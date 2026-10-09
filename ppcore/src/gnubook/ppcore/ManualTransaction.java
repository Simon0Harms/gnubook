package gnubook.ppcore;

import java.io.IOException;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.time.LocalTime;
import java.util.Map;

import com.google.gson.JsonObject;

import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.model.Portfolio;
import name.abuchen.portfolio.model.PortfolioTransaction;
import name.abuchen.portfolio.model.Security;
import name.abuchen.portfolio.model.SecurityPrice;
import name.abuchen.portfolio.model.Transaction.Unit;
import name.abuchen.portfolio.money.ExchangeRate;
import name.abuchen.portfolio.money.Money;
import name.abuchen.portfolio.money.Values;

/**
 * Manual inbound and outbound deliveries (Einlieferung / Auslieferung) – what PP's desktop dialog of the same name
 * does, for documents PP's PDF importers do not read (for example Baader's / Scalable's "Depotauslieferung").
 *
 * <p>Body of {@code POST /clients/{id}/transactions}:
 * {@code type} (DELIVERY_INBOUND | DELIVERY_OUTBOUND), {@code portfolio} (UUID; optional with one active
 * portfolio), {@code security} (UUID) or {@code isin} (an existing security, or a new one for an inbound delivery
 * with {@code name} and optional {@code currency}), {@code date} (YYYY-MM-DD), {@code shares}, optional
 * {@code amount} (value in the portfolio's currency; empty = shares × price on that day), {@code fees},
 * {@code taxes}, {@code note}, {@code force} (allow delivering more shares than held).
 *
 * <p>As in PP, the amount of an inbound delivery includes fees and taxes, the amount of an outbound delivery is
 * the value less fees and taxes.
 */
final class ManualTransaction
{
    private ManualTransaction()
    {
    }

    static Map<String, Object> create(ClientHolder holder, JsonObject body) throws IOException
    {
        holder.acquire();
        try
        {
            Client client = holder.client();
            PortfolioTransaction.Type type = type(Json.str(body, "type"));
            Portfolio portfolio = portfolio(client, Json.str(body, "portfolio"));
            String currency = portfolio.getReferenceAccount() != null
                            ? portfolio.getReferenceAccount().getCurrencyCode()
                            : client.getBaseCurrency();
            LocalDate day = date(Json.str(body, "date"));
            if (day.isAfter(LocalDate.now()))
                throw ApiException.badRequest("date is in the future");
            long shares = shares(body);
            Security security = security(client, body, type, currency);
            boolean outbound = type == PortfolioTransaction.Type.DELIVERY_OUTBOUND;

            long held = held(portfolio, security, day);
            if (outbound && shares > held && !Json.bool(body, "force", false))
                throw ApiException.conflict("not_enough_shares", "only " + Json.shares(held)
                                + " shares of " + security.getName() + " in " + portfolio.getName() + " on " + day);

            long fees = money(body, "fees", 0L);
            long taxes = money(body, "taxes", 0L);
            String secCurrency = security.getCurrencyCode() == null ? currency : security.getCurrencyCode();
            BigDecimal rate = BigDecimal.ONE;
            if (!secCurrency.equals(currency))
            {
                ExchangeRate r = holder.converter().with(currency).getRate(day, secCurrency);
                if (r == null || r.getValue() == null || r.getValue().signum() <= 0)
                    throw ApiException.badRequest("no exchange rate " + secCurrency + "/" + currency + " for " + day);
                rate = r.getValue();
            }

            // gross value: given amount without fees/taxes, else shares × price of that day
            long gross;
            Long amount = body.has("amount") && !body.get("amount").isJsonNull()
                            && Json.str(body, "amount") != null && !Json.str(body, "amount").isBlank()
                                            ? money(body, "amount", 0L)
                                            : null;
            if (amount != null)
                gross = outbound ? amount + fees + taxes : amount - fees - taxes;
            else
            {
                SecurityPrice price = security.getSecurityPrice(day);
                if (price == null || price.getValue() <= 0)
                    throw ApiException.badRequest("no price of " + security.getName() + " for " + day
                                    + " – enter the value");
                BigDecimal value = BigDecimal.valueOf(shares).multiply(BigDecimal.valueOf(price.getValue()))
                                .divide(BigDecimal.valueOf(Values.Share.factor()))
                                .divide(BigDecimal.valueOf(Values.Quote.factor()))
                                .multiply(BigDecimal.valueOf(Values.Amount.factor())).multiply(rate);
                gross = value.setScale(0, RoundingMode.HALF_UP).longValueExact();
                amount = outbound ? gross - fees - taxes : gross + fees + taxes;
            }
            if (gross < 0 || amount < 0)
                throw ApiException.badRequest("fees and taxes are larger than the value");

            PortfolioTransaction t = new PortfolioTransaction();
            t.setType(type);
            t.setDateTime(day.atTime(LocalTime.MIDNIGHT));
            t.setSecurity(security);
            t.setShares(shares);
            t.setCurrencyCode(currency);
            t.setAmount(amount);
            String note = Json.str(body, "note");
            if (note != null && !note.isBlank())
                t.setNote(note.trim());
            if (!secCurrency.equals(currency))
            {
                long forex = BigDecimal.valueOf(gross).divide(rate, 0, RoundingMode.HALF_UP).longValueExact();
                t.addUnit(new Unit(Unit.Type.GROSS_VALUE, Money.of(currency, gross), Money.of(secCurrency, forex),
                                rate));
            }
            if (fees != 0)
                t.addUnit(new Unit(Unit.Type.FEE, Money.of(currency, fees)));
            if (taxes != 0)
                t.addUnit(new Unit(Unit.Type.TAX, Money.of(currency, taxes)));
            portfolio.addTransaction(t);
            client.markDirty();
            holder.save();
            return Json.obj().with("uuid", t.getUUID()).with("type", type.name()).with("portfolio", portfolio.getUUID())
                            .with("security", security.getUUID()).with("date", day.toString())
                            .with("shares", Json.shares(shares)).with("amount", Json.amount(amount))
                            .with("currency", currency).with("held", Json.shares(held))
                            .with("revision", holder.revision());
        }
        finally
        {
            holder.lock.unlock();
        }
    }

    private static PortfolioTransaction.Type type(String v)
    {
        if ("DELIVERY_INBOUND".equals(v))
            return PortfolioTransaction.Type.DELIVERY_INBOUND;
        if ("DELIVERY_OUTBOUND".equals(v))
            return PortfolioTransaction.Type.DELIVERY_OUTBOUND;
        throw ApiException.badRequest("type: expected DELIVERY_INBOUND or DELIVERY_OUTBOUND");
    }

    private static Portfolio portfolio(Client client, String uuid)
    {
        if (uuid == null || uuid.isBlank())
        {
            var active = client.getPortfolios().stream().filter(p -> !p.isRetired()).toList();
            if (active.size() != 1)
                throw ApiException.badRequest("portfolio: choose one");
            return active.get(0);
        }
        return client.getPortfolios().stream().filter(p -> p.getUUID().equals(uuid)).findFirst()
                        .orElseThrow(() -> ApiException.badRequest("unknown portfolio"));
    }

    private static Security security(Client client, JsonObject body, PortfolioTransaction.Type type, String currency)
    {
        String uuid = Json.str(body, "security");
        if (uuid != null && !uuid.isBlank())
            return client.getSecurities().stream().filter(s -> s.getUUID().equals(uuid)).findFirst()
                            .orElseThrow(() -> ApiException.badRequest("unknown security"));
        String isin = Json.str(body, "isin");
        if (isin == null || isin.isBlank())
            throw ApiException.badRequest("security or isin required");
        String wanted = isin.trim().toUpperCase();
        if (!wanted.matches("[A-Z]{2}[A-Z0-9]{9}[0-9]"))
            throw ApiException.badRequest("isin: invalid");
        var found = client.getSecurities().stream().filter(s -> wanted.equals(s.getIsin())).findFirst();
        if (found.isPresent())
            return found.get();
        if (type != PortfolioTransaction.Type.DELIVERY_INBOUND)
            throw ApiException.badRequest("no security with ISIN " + wanted + " in the file");
        String name = Json.str(body, "name");
        if (name == null || name.isBlank())
            throw ApiException.badRequest("name required for a new security");
        String cur = Json.str(body, "currency");
        Security s = new Security(name.trim(), cur == null || cur.isBlank() ? currency : cur.trim().toUpperCase());
        s.setIsin(wanted);
        client.addSecurity(s);
        return s;
    }

    /** Shares of the security in the portfolio at the end of the day. */
    private static long held(Portfolio portfolio, Security security, LocalDate day)
    {
        long n = 0;
        for (PortfolioTransaction t : portfolio.getTransactions())
            if (t.getSecurity() == security && !t.getDateTime().toLocalDate().isAfter(day))
                n += t.getType().isPurchase() ? t.getShares() : -t.getShares();
        return n;
    }

    private static LocalDate date(String v)
    {
        try
        {
            return LocalDate.parse(v);
        }
        catch (RuntimeException e)
        {
            throw ApiException.badRequest("date: expected YYYY-MM-DD");
        }
    }

    private static long shares(JsonObject body)
    {
        BigDecimal v = decimal(body, "shares");
        if (v == null || v.signum() <= 0)
            throw ApiException.badRequest("shares: expected a positive number");
        return v.multiply(BigDecimal.valueOf(Values.Share.factor())).setScale(0, RoundingMode.HALF_UP)
                        .longValueExact();
    }

    private static long money(JsonObject body, String key, long fallback)
    {
        BigDecimal v = decimal(body, key);
        if (v == null)
            return fallback;
        if (v.signum() < 0)
            throw ApiException.badRequest(key + ": must not be negative");
        return v.multiply(BigDecimal.valueOf(Values.Amount.factor())).setScale(0, RoundingMode.HALF_UP)
                        .longValueExact();
    }

    private static BigDecimal decimal(JsonObject body, String key)
    {
        String v = Json.str(body, key);
        if (v == null || v.isBlank())
            return null;
        try
        {
            return new BigDecimal(v.trim());
        }
        catch (NumberFormatException e)
        {
            throw ApiException.badRequest(key + ": not a number");
        }
    }
}
