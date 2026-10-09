package gnubook.ppcore;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.util.Random;

import name.abuchen.portfolio.model.Account;
import name.abuchen.portfolio.model.AccountTransaction;
import name.abuchen.portfolio.model.BuySellEntry;
import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.model.Portfolio;
import name.abuchen.portfolio.model.PortfolioTransaction;
import name.abuchen.portfolio.model.Security;
import name.abuchen.portfolio.model.SecurityPrice;
import name.abuchen.portfolio.model.Transaction.Unit;
import name.abuchen.portfolio.money.Money;
import name.abuchen.portfolio.money.Values;
import name.abuchen.portfolio.online.QuoteFeed;

/**
 * A fictional PP file for gnubook's demo: the securities of the demo book (an ETF savings plan and a share bought
 * once and partly sold, with fees, taxes and a dividend), daily prices and the clearing account. All names are made
 * up, the securities have no ISIN and no price source. Built in code, so it is always valid for the running PP
 * version and contains nothing from elsewhere.
 */
final class Demo
{
    private static final String EUR = "EUR";

    private Demo()
    {
    }

    static Client build(LocalDate start, int months, long seed)
    {
        Random rnd = new Random(seed);
        Client client = new Client();
        client.setBaseCurrency(EUR);

        Account account = new Account();
        account.setName("Verrechnungskonto Musterbank");
        account.setCurrencyCode(EUR);
        client.addAccount(account);
        Portfolio depot = new Portfolio();
        depot.setName("Depot Musterbank");
        depot.setReferenceAccount(account);
        client.addPortfolio(depot);

        Security etf = security(client, "Musterwelt Aktien ETF", "WELT", "MW0001");
        Security share = security(client, "Muster Industrie AG", "MUST", "MI0001");

        LocalDate end = start.plusMonths(months).minusDays(1);
        LocalDate today = LocalDate.now();
        if (end.isAfter(today))
            end = today;

        // daily prices (working days), a random walk with a slight upward drift
        double pEtf = 92.40;
        double pShare = 31.80;
        for (LocalDate d = start; !d.isAfter(end); d = d.plusDays(1))
        {
            if (d.getDayOfWeek().getValue() > 5)
                continue;
            pEtf = Math.max(1, pEtf * (1 + (rnd.nextInt(41) - 19) / 2000.0));
            pShare = Math.max(1, pShare * (1 + (rnd.nextInt(61) - 29) / 1500.0));
            etf.addPrice(new SecurityPrice(d, Math.round(pEtf * Values.Quote.factor())));
            share.addPrice(new SecurityPrice(d, Math.round(pShare * Values.Quote.factor())));
        }

        deposit(account, start, 5000_00);
        for (int m = 0; m < months; m++)
        {
            LocalDate d = start.plusMonths(m).withDayOfMonth(15);
            if (d.isAfter(end))
                break;
            d = workday(d);
            // savings plan: 150 EUR incl. 1.50 EUR fee
            long price = price(etf, d);
            long net = 148_50;
            long shares = Math.round((double) net / 100 * Values.Share.factor() * Values.Quote.factor() / price);
            buy(depot, account, etf, d, shares, 150_00, 1_50, 0, "Sparplan");
            if (m == 0)
                deposit(account, d.plusDays(3), 150_00L * months);
            if (m == 1)
            {
                long gross = Math.round(40.0 * price(share, d) / Values.Quote.factor() * 100);
                buy(depot, account, share, d, 40L * Values.Share.factor(), gross + 4_90, 4_90, 0, null);
            }
            if (m == 5 || m == 11)
            {
                // dividend 0.85 EUR per share, 26.375 % tax on it
                long gross = 40 * 85;
                long tax = Math.round(gross * 0.26375);
                dividend(account, share, d.plusDays(6), gross - tax, tax);
            }
            if (m == months - 4)
            {
                long gross = Math.round(15.0 * price(share, d) / Values.Quote.factor() * 100);
                long tax = Math.max(0, Math.round((gross - 15 * price(share, start.plusMonths(1).withDayOfMonth(15))
                                / Values.Quote.factor() * 100) * 0.26375));
                sell(depot, account, share, d, 15L * Values.Share.factor(), gross - 4_90 - tax, 4_90, tax);
            }
        }
        // a small account fee once a year
        LocalDate fee = workday(start.plusMonths(Math.min(months, 12) - 1).withDayOfMonth(28));
        if (!fee.isAfter(end))
            account.addTransaction(new AccountTransaction(fee.atTime(LocalTime.MIDNIGHT), EUR, 12_00, null,
                            AccountTransaction.Type.FEES));
        return client;
    }

    private static Security security(Client client, String name, String ticker, String wkn)
    {
        Security s = new Security(name, EUR);
        s.setTickerSymbol(ticker);
        s.setWkn(wkn);
        s.setFeed(QuoteFeed.MANUAL);
        s.setNote("Fiktives Wertpapier der gnubook-Demo");
        client.addSecurity(s);
        return s;
    }

    private static LocalDate workday(LocalDate d)
    {
        while (d.getDayOfWeek().getValue() > 5)
            d = d.plusDays(1);
        return d;
    }

    private static long price(Security s, LocalDate d)
    {
        SecurityPrice p = s.getSecurityPrice(d);
        return p == null || p.getValue() <= 0 ? Values.Quote.factor() : p.getValue();
    }

    private static void deposit(Account account, LocalDate d, long cents)
    {
        account.addTransaction(new AccountTransaction(workday(d).atTime(LocalTime.MIDNIGHT), EUR, cents, null,
                        AccountTransaction.Type.DEPOSIT));
    }

    private static void buy(Portfolio p, Account a, Security s, LocalDate d, long shares, long amount, long fee,
                    long tax, String note)
    {
        entry(p, a, s, d, PortfolioTransaction.Type.BUY, shares, amount, fee, tax, note);
    }

    private static void sell(Portfolio p, Account a, Security s, LocalDate d, long shares, long amount, long fee,
                    long tax)
    {
        entry(p, a, s, d, PortfolioTransaction.Type.SELL, shares, amount, fee, tax, null);
    }

    private static void entry(Portfolio p, Account a, Security s, LocalDate d, PortfolioTransaction.Type type,
                    long shares, long amount, long fee, long tax, String note)
    {
        BuySellEntry e = new BuySellEntry(p, a);
        e.setType(type);
        e.setDate(LocalDateTime.of(d, LocalTime.MIDNIGHT));
        e.setSecurity(s);
        e.setShares(shares);
        e.setMonetaryAmount(Money.of(EUR, amount));
        if (fee > 0)
            e.getPortfolioTransaction().addUnit(new Unit(Unit.Type.FEE, Money.of(EUR, fee)));
        if (tax > 0)
            e.getPortfolioTransaction().addUnit(new Unit(Unit.Type.TAX, Money.of(EUR, tax)));
        if (note != null)
            e.setNote(note);
        e.insert();
    }

    private static void dividend(Account a, Security s, LocalDate d, long net, long tax)
    {
        AccountTransaction t = new AccountTransaction(workday(d).atTime(LocalTime.MIDNIGHT), EUR, net, s,
                        AccountTransaction.Type.DIVIDENDS);
        if (tax > 0)
            t.addUnit(new Unit(Unit.Type.TAX, Money.of(EUR, tax)));
        a.addTransaction(t);
    }
}
