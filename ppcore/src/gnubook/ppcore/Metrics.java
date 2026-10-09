package gnubook.ppcore;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.model.CostMethod;
import name.abuchen.portfolio.model.Portfolio;
import name.abuchen.portfolio.model.Security;
import name.abuchen.portfolio.model.TaxesAndFees;
import name.abuchen.portfolio.money.CurrencyConverter;
import name.abuchen.portfolio.money.Money;
import name.abuchen.portfolio.snapshot.AccountSnapshot;
import name.abuchen.portfolio.snapshot.ClientPerformanceSnapshot;
import name.abuchen.portfolio.snapshot.ClientPerformanceSnapshot.CategoryType;
import name.abuchen.portfolio.snapshot.ClientSnapshot;
import name.abuchen.portfolio.snapshot.PerformanceIndex;
import name.abuchen.portfolio.snapshot.PortfolioSnapshot;
import name.abuchen.portfolio.snapshot.SecurityPosition;
import name.abuchen.portfolio.snapshot.filter.PortfolioClientFilter;
import name.abuchen.portfolio.snapshot.security.LazySecurityPerformanceRecord;
import name.abuchen.portfolio.snapshot.security.LazySecurityPerformanceSnapshot;
import name.abuchen.portfolio.util.Interval;

/** Performance figures (PP's "Performance" view) and the statement of assets, computed by PP itself. */
final class Metrics
{
    private static final int MAX_POINTS = 400;

    private Metrics()
    {
    }

    /**
     * Performance between the end of {@code from - 1} and the end of {@code to} (PP's reporting interval), for the
     * whole file or one securities account plus its reference account.
     */
    static Map<String, Object> performance(ClientHolder holder, LocalDate from, LocalDate to, String portfolioUuid)
    {
        Client client = holder.client();
        CurrencyConverter converter = holder.converter();
        Client subject = client;
        if (portfolioUuid != null && !portfolioUuid.isBlank())
        {
            Portfolio p = PdfImporter.findPortfolio(client, portfolioUuid);
            if (p == null)
                throw ApiException.notFound("unknown securities account");
            subject = p.getReferenceAccount() == null ? new PortfolioClientFilter(p).filter(client)
                            : new PortfolioClientFilter(p, p.getReferenceAccount()).filter(client);
        }
        Interval interval = Interval.of(from.minusDays(1), to);
        ClientPerformanceSnapshot snap = new ClientPerformanceSnapshot(subject, converter, interval);
        List<Exception> warnings = new ArrayList<>();
        PerformanceIndex index = PerformanceIndex.forClient(subject, converter, interval, warnings);

        Json.Obj categories = Json.obj();
        for (CategoryType t : CategoryType.values())
        {
            var category = snap.getCategoryByType(t);
            if (category == null)
                continue;
            List<Object> positions = new ArrayList<>();
            for (var pos : category.getPositions())
                positions.add(Json.obj().with("label", pos.getLabel())
                                .with("security", pos.getSecurity() == null ? null : pos.getSecurity().getUUID())
                                .with("value", Json.money(pos.getValue())));
            categories.put(t.name(), Json.obj().with("label", category.getLabel())
                            .with("value", Json.money(category.getValuation())).with("positions", positions));
        }

        LocalDate[] dates = index.getDates();
        double[] accumulated = index.getAccumulatedPercentage();
        long[] totals = index.getTotals();
        long[] invested = index.calculateInvestedCapital();
        List<Object> series = new ArrayList<>();
        int n = dates.length;
        int step = Math.max(1, (int) Math.ceil(n / (double) MAX_POINTS));
        for (int i = 0; i < n; i++)
        {
            if (i % step != 0 && i != n - 1)
                continue;
            series.add(List.of(dates[i].toString(), Json.finite(accumulated[i]), Json.amount(totals[i]),
                            Json.amount(invested[i])));
        }

        var drawdown = index.getDrawdown();
        var volatility = index.getVolatility();
        Money initial = snap.getValue(CategoryType.INITIAL_VALUE);
        Money fin = snap.getValue(CategoryType.FINAL_VALUE);
        List<String> warningTexts = new ArrayList<>();
        for (Exception w : warnings)
            warningTexts.add(w.getMessage());

        return Json.obj().with("currency", converter.getTermCurrency()).with("from", from.toString())
                        .with("to", to.toString()).with("portfolio", portfolioUuid)
                        .with("ttwror", Json.finite(index.getFinalAccumulatedPercentage()))
                        .with("ttwrorAnnualized", Json.finite(index.getFinalAccumulatedAnnualizedPercentage()))
                        .with("irr", Json.finite(snap.getPerformanceIRR()))
                        .with("initialValue", Json.money(initial)).with("finalValue", Json.money(fin))
                        .with("absoluteChange", Json.money(fin.subtract(initial)))
                        .with("delta", Json.money(snap.getAbsoluteDelta()))
                        .with("maxDrawdown", drawdown == null ? null : Json.finite(drawdown.getMaxDrawdown()))
                        .with("volatility", volatility == null ? null : Json.finite(volatility.getStandardDeviation()))
                        .with("semiVolatility", volatility == null ? null : Json.finite(volatility.getSemiDeviation()))
                        .with("categories", categories).with("series", series).with("warnings", warningTexts);
    }

    /** Statement of assets on {@code date}: positions with PP's FIFO figures, cash accounts, securities accounts. */
    static Map<String, Object> holdings(ClientHolder holder, LocalDate date)
    {
        Client client = holder.client();
        CurrencyConverter converter = holder.converter();
        ClientSnapshot snapshot = ClientSnapshot.create(client, converter, date);

        LocalDate first = client.getPortfolios().stream().flatMap(p -> p.getTransactions().stream())
                        .map(t -> t.getDateTime().toLocalDate()).min(Comparator.naturalOrder()).orElse(date);
        LazySecurityPerformanceSnapshot perf = LazySecurityPerformanceSnapshot.create(client, converter,
                        Interval.of(first.minusDays(1), date));
        Map<Security, LazySecurityPerformanceRecord> records = new HashMap<>();
        for (var r : perf.getRecords())
            records.put(r.getSecurity(), r);

        List<Object> positions = new ArrayList<>();
        Money total = Money.of(converter.getTermCurrency(), 0);
        for (SecurityPosition pos : snapshot.getJointPortfolio().getPositions())
        {
            Security s = pos.getSecurity();
            if (s == null)
                continue;
            Money value = converter.convert(date, pos.calculateValue());
            total = total.add(value);
            Json.Obj o = Json.obj().with("security", s.getUUID()).with("name", s.getName()).with("isin", s.getIsin())
                            .with("wkn", s.getWkn()).with("ticker", s.getTickerSymbol())
                            .with("currency", s.getCurrencyCode()).with("shares", Json.shares(pos.getShares()))
                            .with("price", Json.quote(pos.getPrice().getValue()))
                            .with("priceDate", pos.getPrice().getDate() == null ? null : pos.getPrice().getDate().toString())
                            .with("value", Json.money(value));
            var r = records.get(s);
            if (r != null)
            {
                o.put("cost", Json.money(r.getCost(CostMethod.FIFO, TaxesAndFees.INCLUDED)));
                o.put("costNet", Json.money(r.getCost(CostMethod.FIFO, TaxesAndFees.NOT_INCLUDED)));
                o.put("unrealizedGain", Json.money(r.getCapitalGainsOnHoldings(CostMethod.FIFO)));
                o.put("unrealizedGainPercent", Json.finiteOrNull(r.getCapitalGainsOnHoldingsPercent(CostMethod.FIFO)));
                var realized = r.getRealizedCapitalGains(CostMethod.FIFO);
                o.put("realizedGain", realized == null ? null : Json.money(realized.getCapitalGains()));
                o.put("dividends", Json.money(r.getSumOfDividends()));
                o.put("fees", Json.money(r.getFees()));
                o.put("taxes", Json.money(r.getTaxes()));
                o.put("irr", Json.finiteOrNull(r.getIrr()));
                o.put("ttwror", Json.finiteOrNull(r.getTrueTimeWeightedRateOfReturn()));
                o.put("delta", Json.money(r.getDelta()));
            }
            positions.add(o);
        }
        positions.sort(Comparator.comparing((Object o) -> {
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) o;
            @SuppressWarnings("unchecked")
            Map<String, Object> v = (Map<String, Object>) m.get("value");
            return v == null ? java.math.BigDecimal.ZERO : new java.math.BigDecimal((String) v.get("amount"));
        }).reversed());

        List<Object> accounts = new ArrayList<>();
        Money cash = Money.of(converter.getTermCurrency(), 0);
        for (AccountSnapshot a : snapshot.getAccounts())
        {
            if (a.getAccount().isRetired() && a.getUnconvertedFunds().isZero())
                continue;
            cash = cash.add(a.getFunds());
            accounts.add(Json.obj().with("uuid", a.getAccount().getUUID()).with("name", a.getAccount().getName())
                            .with("balance", Json.money(a.getUnconvertedFunds())).with("value", Json.money(a.getFunds())));
        }

        List<Object> portfolios = new ArrayList<>();
        for (PortfolioSnapshot ps : snapshot.getPortfolios())
        {
            List<Object> pp = new ArrayList<>();
            for (SecurityPosition pos : ps.getPositions())
                if (pos.getSecurity() != null)
                    pp.add(Json.obj().with("security", pos.getSecurity().getUUID())
                                    .with("shares", Json.shares(pos.getShares()))
                                    .with("value", Json.money(converter.convert(date, pos.calculateValue()))));
            portfolios.add(Json.obj().with("uuid", ps.getPortfolio().getUUID()).with("name", ps.getPortfolio().getName())
                            .with("value", Json.money(ps.getValue())).with("positions", pp));
        }

        return Json.obj().with("date", date.toString()).with("currency", converter.getTermCurrency())
                        .with("positions", positions).with("securitiesValue", Json.money(total))
                        .with("accounts", accounts).with("cash", Json.money(cash))
                        .with("total", Json.money(total.add(cash))).with("portfolios", portfolios);
    }
}
