package gnubook.ppcore;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import name.abuchen.portfolio.model.Account;
import name.abuchen.portfolio.model.AccountTransaction;
import name.abuchen.portfolio.model.AccountTransferEntry;
import name.abuchen.portfolio.model.BuySellEntry;
import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.model.CrossEntry;
import name.abuchen.portfolio.model.LatestSecurityPrice;
import name.abuchen.portfolio.model.Portfolio;
import name.abuchen.portfolio.model.PortfolioTransaction;
import name.abuchen.portfolio.model.PortfolioTransferEntry;
import name.abuchen.portfolio.model.Security;
import name.abuchen.portfolio.model.SecurityPrice;
import name.abuchen.portfolio.model.Transaction;
import name.abuchen.portfolio.model.TransactionOwner;
import name.abuchen.portfolio.money.ExchangeRate;
import name.abuchen.portfolio.money.ExchangeRateTimeSeries;
import name.abuchen.portfolio.money.Values;

/**
 * Everything gnubook needs to book the PP file into GnuCash: securities with prices, cash accounts, securities
 * accounts and all transactions (with their units and cross entries). Amounts are decimal strings.
 */
final class Exporter
{
    private Exporter()
    {
    }

    /**
     * @param pricesSince
     *            only prices on or after this day (null = all, LocalDate.MAX = none)
     */
    static Map<String, Object> export(ClientHolder holder, LocalDate pricesSince)
    {
        Client client = holder.client();
        String base = client.getBaseCurrency();

        List<Object> securities = new ArrayList<>();
        for (Security s : client.getSecurities())
        {
            Json.Obj o = Json.obj().with("uuid", s.getUUID()).with("name", s.getName())
                            .with("currency", s.getCurrencyCode()).with("targetCurrency", s.getTargetCurrencyCode())
                            .with("isin", blankToNull(s.getIsin())).with("wkn", blankToNull(s.getWkn()))
                            .with("ticker", blankToNull(s.getTickerSymbol())).with("feed", s.getFeed())
                            .with("feedUrl", s.getFeedURL()).with("latestFeed", s.getLatestFeed())
                            .with("latestFeedUrl", s.getLatestFeedURL()).with("retired", s.isRetired())
                            .with("exchangeRate", s.isExchangeRate()).with("note", s.getNote())
                            .with("calendar", s.getCalendar());
            LatestSecurityPrice latest = s.getLatest();
            o.put("latest", latest == null || latest.getValue() <= 0 ? null
                            : Json.obj().with("date", Json.date(latest.getDate()))
                                            .with("value", Json.quote(latest.getValue())));
            List<SecurityPrice> all = s.getPrices();
            o.put("priceCount", all.size());
            o.put("firstPrice", all.isEmpty() ? null : Json.date(all.get(0).getDate()));
            o.put("lastPrice", all.isEmpty() ? null : Json.date(all.get(all.size() - 1).getDate()));

            // prices, also converted into the base currency (ECB rates, like PP does) for GnuCash's price DB
            List<Object> prices = new ArrayList<>();
            boolean foreign = s.getCurrencyCode() != null && !s.getCurrencyCode().equals(base);
            ExchangeRateTimeSeries series = foreign ? holder.series(s.getCurrencyCode()) : null;
            if (pricesSince == null || !pricesSince.equals(LocalDate.MAX))
            {
                for (SecurityPrice p : s.getPricesIncludingLatest())
                {
                    if (pricesSince != null && p.getDate().isBefore(pricesSince))
                        continue;
                    if (p.getValue() <= 0)
                        continue;
                    String inBase = "";
                    if (!foreign)
                    {
                        inBase = Json.quote(p.getValue());
                    }
                    else if (series != null)
                    {
                        Optional<ExchangeRate> rate = series.lookupRate(p.getDate());
                        if (rate.isPresent() && rate.get().getValue() != null && rate.get().getValue().signum() > 0)
                            inBase = Json.decimal(rate.get().getValue().multiply(BigDecimal.valueOf(p.getValue()))
                                            .setScale(0, RoundingMode.HALF_UP).longValueExact(),
                                            Values.Quote.precision());
                    }
                    prices.add(List.of(Json.date(p.getDate()), Json.quote(p.getValue()), inBase));
                }
            }
            o.put("prices", prices);
            securities.add(o);
        }

        List<Object> accounts = new ArrayList<>();
        List<Object> transactions = new ArrayList<>();
        for (Account a : client.getAccounts())
        {
            accounts.add(Json.obj().with("uuid", a.getUUID()).with("name", a.getName())
                            .with("currency", a.getCurrencyCode()).with("retired", a.isRetired())
                            .with("note", a.getNote()));
            for (AccountTransaction t : a.getTransactions())
            {
                Json.Obj o = transaction(t, "account", a.getUUID());
                o.put("type", t.getType().name());
                o.put("exDate", t.getExDate() == null ? null : Json.dateTime(t.getExDate()));
                transactions.add(o);
            }
        }

        List<Object> portfolios = new ArrayList<>();
        for (Portfolio p : client.getPortfolios())
        {
            portfolios.add(Json.obj().with("uuid", p.getUUID()).with("name", p.getName())
                            .with("referenceAccount",
                                            p.getReferenceAccount() == null ? null : p.getReferenceAccount().getUUID())
                            .with("retired", p.isRetired()).with("note", p.getNote()));
            for (PortfolioTransaction t : p.getTransactions())
            {
                Json.Obj o = transaction(t, "portfolio", p.getUUID());
                o.put("type", t.getType().name());
                transactions.add(o);
            }
        }

        return Json.obj()
                        .with("client", Json.obj().with("id", holder.id).with("baseCurrency", base)
                                        .with("revision", holder.revision())
                                        .with("file", holder.file() == null ? null
                                                        : holder.file().getFileName().toString())
                                        .with("version", client.getFileVersionAfterRead()))
                        .with("securities", securities).with("accounts", accounts).with("portfolios", portfolios)
                        .with("transactions", transactions);
    }

    private static Json.Obj transaction(Transaction t, String kind, String owner)
    {
        Json.Obj o = Json.obj().with("uuid", t.getUUID()).with("kind", kind).with("owner", owner)
                        .with("date", Json.dateTime(t.getDateTime())).with("currency", t.getCurrencyCode())
                        .with("amount", Json.amount(t.getAmount()))
                        .with("shares", t.getShares() == 0 ? null : Json.shares(t.getShares()))
                        .with("security", t.getSecurity() == null ? null : t.getSecurity().getUUID())
                        .with("note", t.getNote()).with("source", t.getSource())
                        .with("updatedAt", t.getUpdatedAt() == null ? null : t.getUpdatedAt().toString());
        List<Object> units = new ArrayList<>();
        t.getUnits().forEach(u -> {
            Json.Obj uo = Json.obj().with("type", u.getType().name()).with("amount", Json.amount(u.getAmount().getAmount()))
                            .with("currency", u.getAmount().getCurrencyCode());
            if (u.getForex() != null)
            {
                uo.put("forexAmount", Json.amount(u.getForex().getAmount()));
                uo.put("forexCurrency", u.getForex().getCurrencyCode());
                uo.put("rate", u.getExchangeRate() == null ? null : u.getExchangeRate().toPlainString());
            }
            units.add(uo);
        });
        o.put("units", units);
        CrossEntry cross = t.getCrossEntry();
        if (cross != null)
        {
            Transaction other = cross.getCrossTransaction(t);
            TransactionOwner<?> otherOwner = cross.getCrossOwner(t);
            String type = cross instanceof BuySellEntry ? "buysell"
                            : cross instanceof AccountTransferEntry ? "account-transfer"
                                            : cross instanceof PortfolioTransferEntry ? "portfolio-transfer"
                                                            : cross.getClass().getSimpleName();
            o.put("cross", Json.obj().with("type", type).with("uuid", other == null ? null : other.getUUID())
                            .with("owner", ownerUuid(otherOwner)));
        }
        else
        {
            o.put("cross", null);
        }
        return o;
    }

    private static String ownerUuid(TransactionOwner<?> owner)
    {
        if (owner instanceof Account a)
            return a.getUUID();
        if (owner instanceof Portfolio p)
            return p.getUUID();
        return null;
    }

    private static String blankToNull(String s)
    {
        return s == null || s.isBlank() ? null : s.trim();
    }
}
