package gnubook.ppcore;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;

import name.abuchen.portfolio.PortfolioLog;
import name.abuchen.portfolio.model.Security;
import name.abuchen.portfolio.online.Factory;
import name.abuchen.portfolio.online.QuoteFeed;
import name.abuchen.portfolio.online.SecuritySearchProvider;
import name.abuchen.portfolio.online.SecuritySearchProvider.ResultItem;
import name.abuchen.portfolio.online.impl.PortfolioPerformanceFeed;
import name.abuchen.portfolio.online.impl.YahooFinanceQuoteFeed;
import name.abuchen.portfolio.online.impl.YahooSearchProvider;

/**
 * Search for instruments (for the "choose price source" dialog in gnubook) and a best-effort automatic choice of a
 * price source for securities created by a PDF import. PP's own data service needs a login that a server cannot do,
 * so the automatic choice uses Yahoo Finance by ISIN.
 */
final class FeedFinder
{
    private FeedFinder()
    {
    }

    static List<Object> search(String query)
    {
        List<Object> out = new ArrayList<>();
        for (SecuritySearchProvider provider : Factory.getSearchProvider())
        {
            List<ResultItem> results;
            try
            {
                results = provider.search(query);
            }
            catch (Exception e) // NOSONAR – a failing provider must not break the others
            {
                PortfolioLog.warning(provider.getName() + ": " + e.getMessage());
                continue;
            }
            for (ResultItem r : results)
            {
                String feed = r.getFeedId();
                boolean needsLogin = PortfolioPerformanceFeed.ID.equals(feed);
                out.add(Json.obj().with("provider", provider.getName()).with("name", r.getName())
                                .with("symbol", r.getSymbol()).with("isin", r.getIsin()).with("wkn", r.getWkn())
                                .with("type", r.getType()).with("exchange", r.getExchange())
                                .with("currency", r.getCurrencyCode()).with("feed", feed)
                                .with("source", r.getSource()).with("needsLogin", needsLogin));
                if (out.size() >= 60)
                    return out;
            }
        }
        return out;
    }

    /** Sets a Yahoo Finance price source for securities without one (by ISIN, else by name). */
    static void configure(List<Security> securities)
    {
        for (Security s : securities)
        {
            if (s.getFeed() != null && !QuoteFeed.MANUAL.equals(s.getFeed()))
                continue;
            String query = s.getIsin() != null && !s.getIsin().isBlank() ? s.getIsin() : s.getWkn();
            if (query == null || query.isBlank())
                continue;
            try
            {
                List<ResultItem> results = Factory.getSearchProvider(YahooSearchProvider.class).search(query);
                ResultItem best = results.stream().filter(r -> r.getSymbol() != null && !r.getSymbol().isBlank())
                                .min(Comparator.comparingInt(r -> rank(r, s.getCurrencyCode()))).orElse(null);
                if (best != null)
                {
                    s.setFeed(YahooFinanceQuoteFeed.ID);
                    s.setTickerSymbol(best.getSymbol());
                    s.getEphemeralData().touchFeedConfigurationChanged();
                }
            }
            catch (Exception e) // NOSONAR
            {
                PortfolioLog.warning("no price source found for " + s.getName() + ": " + e.getMessage());
            }
        }
    }

    /** Lower is better: Xetra for EUR, US listings for USD, otherwise the first hit. */
    private static int rank(ResultItem r, String currency)
    {
        String symbol = r.getSymbol().toUpperCase(Locale.ROOT);
        if ("EUR".equals(currency))
        {
            if (symbol.endsWith(".DE"))
                return 0;
            if (symbol.endsWith(".F"))
                return 1;
            if (symbol.matches(".*\\.(AS|PA|MI|VI|MC|BR|LS|HE)$"))
                return 2;
            return 5;
        }
        if ("USD".equals(currency))
            return symbol.contains(".") ? 5 : 0;
        if ("GBP".equals(currency))
            return symbol.endsWith(".L") ? 0 : 5;
        if ("CHF".equals(currency))
            return symbol.endsWith(".SW") ? 0 : 5;
        return 3;
    }
}
