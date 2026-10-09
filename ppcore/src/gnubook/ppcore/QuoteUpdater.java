package gnubook.ppcore;

import java.io.IOException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import org.eclipse.core.runtime.NullProgressMonitor;

import name.abuchen.portfolio.PortfolioLog;
import name.abuchen.portfolio.model.LatestSecurityPrice;
import name.abuchen.portfolio.model.Security;
import name.abuchen.portfolio.model.SecurityPrice;
import name.abuchen.portfolio.model.SecurityProperty;
import name.abuchen.portfolio.money.ExchangeRateProvider;
import name.abuchen.portfolio.money.ExchangeRateProviderFactory;
import name.abuchen.portfolio.online.Factory;
import name.abuchen.portfolio.online.QuoteFeed;
import name.abuchen.portfolio.online.QuoteFeedData;
import name.abuchen.portfolio.online.QuoteFeedException;
import name.abuchen.portfolio.online.impl.PortfolioPerformanceFeed;

/**
 * Updates historical and latest prices of all securities, like PP's "update prices" job: the downloads run on copies
 * of the securities without holding the file lock, the results are merged into the file afterwards.
 */
final class QuoteUpdater
{
    static final class Job
    {
        final String id = UUID.randomUUID().toString();
        final String clientId;
        final Instant started = Instant.now();
        volatile Instant finished;
        volatile String state = "running";
        volatile String error;
        final List<Map<String, Object>> results = new ArrayList<>();
        int modified;

        Job(String clientId)
        {
            this.clientId = clientId;
        }

        Map<String, Object> describe()
        {
            synchronized (results)
            {
                return Json.obj().with("job", id).with("state", state).with("started", started.toString())
                                .with("finished", finished == null ? null : finished.toString()).with("error", error)
                                .with("modified", modified).with("securities", new ArrayList<>(results));
            }
        }
    }

    private static final class Fetched
    {
        Security security;
        QuoteFeed feed;
        QuoteFeedData historic;
        Optional<LatestSecurityPrice> latest = Optional.empty();
        String historicError;
        String latestError;
        boolean permanent;
        String skipped;
    }

    private final ExecutorService executor = Executors.newFixedThreadPool(2, r -> {
        Thread t = new Thread(r, "ppcore-quotes");
        t.setDaemon(true);
        t.setContextClassLoader(name.abuchen.portfolio.model.Client.class.getClassLoader());
        return t;
    });
    private final Map<String, Job> last = new ConcurrentHashMap<>();
    private final Map<String, Future<?>> running = new ConcurrentHashMap<>();
    private volatile Instant ratesUpdated = Instant.EPOCH;

    Job last(String clientId)
    {
        return last.get(clientId);
    }

    /** Starts an update unless one is running. waitSeconds > 0 blocks until done (or the time is up). */
    synchronized Job start(ClientHolder holder, Set<String> only, int waitSeconds)
    {
        Job job = last.get(holder.id);
        if (job == null || !"running".equals(job.state))
        {
            Job fresh = new Job(holder.id);
            last.put(holder.id, fresh);
            running.put(holder.id, executor.submit(() -> run(holder, fresh, only)));
            job = fresh;
        }
        if (waitSeconds > 0)
        {
            Future<?> f = running.get(holder.id);
            if (f != null)
            {
                try
                {
                    f.get(waitSeconds, TimeUnit.SECONDS);
                }
                catch (TimeoutException e)
                {
                    // still running – the caller gets the intermediate state
                }
                catch (Exception e) // NOSONAR
                {
                    job.error = e.toString();
                }
            }
        }
        return job;
    }

    private void run(ClientHolder holder, Job job, Set<String> only)
    {
        try
        {
            updateExchangeRates(holder);

            List<Fetched> work = new ArrayList<>();
            holder.acquire();
            try
            {
                var client = holder.client();
                List<Security> candidates = client.getSecurities().stream()
                                .filter(s -> only == null || only.isEmpty() || only.contains(s.getUUID()))
                                .filter(s -> !s.isRetired() && !s.isExchangeRate()).toList();
                var needLogin = Factory.getQuoteFeed(PortfolioPerformanceFeed.class)
                                .requireAuthentication(candidates);
                for (Security s : candidates)
                {
                    Fetched f = new Fetched();
                    f.security = s;
                    QuoteFeed feed = Factory.getQuoteFeedProvider(s.getFeed());
                    if (feed == null || QuoteFeed.MANUAL.equals(feed.getId()))
                        f.skipped = "no price source configured";
                    else if (needLogin.contains(s))
                        f.skipped = "PP's own price source needs a login – choose another price source";
                    f.feed = feed;
                    work.add(f);
                }
            }
            finally
            {
                holder.lock.unlock();
            }

            // download without holding the lock (on copies, so the file stays consistent)
            for (Fetched f : work)
            {
                if (f.skipped != null)
                    continue;
                Security copy;
                holder.acquire();
                try
                {
                    copy = f.security.deepCopy();
                }
                finally
                {
                    holder.lock.unlock();
                }
                try
                {
                    f.historic = f.feed.getHistoricalQuotes(copy, false);
                }
                catch (QuoteFeedException e)
                {
                    f.historicError = message(e);
                    f.permanent = e.getClass().getSimpleName().contains("Configuration");
                }
                catch (RuntimeException e)
                {
                    f.historicError = message(e);
                }
                String latestFeedId = copy.getLatestFeed() == null ? copy.getFeed() : copy.getLatestFeed();
                QuoteFeed latestFeed = Factory.getQuoteFeedProvider(latestFeedId);
                if (latestFeed != null && !QuoteFeed.MANUAL.equals(latestFeed.getId()))
                {
                    try
                    {
                        Security forLatest = copy;
                        var latestTicker = copy.getPropertyValue(SecurityProperty.Type.FEED,
                                        QuoteFeed.TICKER_SYMBOL_LATEST);
                        if (latestTicker.isPresent())
                        {
                            forLatest = copy.deepCopy();
                            forLatest.setTickerSymbol(latestTicker.get());
                        }
                        f.latest = latestFeed.getLatestQuote(forLatest);
                    }
                    catch (QuoteFeedException | RuntimeException e)
                    {
                        f.latestError = message(e);
                    }
                }
            }

            // merge
            int modified = 0;
            holder.acquire();
            try
            {
                var client = holder.client();
                Map<String, Security> byUuid = new HashMap<>();
                client.getSecurities().forEach(s -> byUuid.put(s.getUUID(), s));
                for (Fetched f : work)
                {
                    Security s = byUuid.get(f.security.getUUID());
                    Json.Obj r = Json.obj().with("uuid", f.security.getUUID()).with("name", f.security.getName())
                                    .with("feed", f.security.getFeed()).with("ticker", f.security.getTickerSymbol());
                    if (s == null)
                    {
                        r.put("status", "skipped");
                        r.put("message", "security was deleted meanwhile");
                    }
                    else if (f.skipped != null)
                    {
                        r.put("status", "skipped");
                        r.put("message", f.skipped);
                    }
                    else
                    {
                        int before = s.getPrices().size();
                        boolean changed = false;
                        List<String> errors = new ArrayList<>();
                        if (f.historic != null)
                        {
                            changed |= applyHistoric(s, f.feed, f.historic);
                            for (Exception e : f.historic.getErrors())
                                errors.add(message(e));
                        }
                        if (f.historicError != null)
                            errors.add(f.historicError);
                        if (f.permanent)
                            s.getEphemeralData().setHasPermanentError();
                        if (f.latest.isPresent())
                            changed |= s.setLatest(f.latest.get());
                        if (f.latestError != null)
                            errors.add(f.latestError);
                        s.getEphemeralData().touchFeedLastUpdate();
                        if (changed)
                            modified++;
                        r.put("status", !errors.isEmpty() && !changed ? "error" : changed ? "updated" : "unchanged");
                        r.put("message", errors.isEmpty() ? null : String.join("; ", errors));
                        r.put("newPrices", Math.max(0, s.getPrices().size() - before));
                    }
                    if (s != null)
                    {
                        List<SecurityPrice> prices = s.getPrices();
                        r.put("lastPrice", prices.isEmpty() ? null : prices.get(prices.size() - 1).getDate().toString());
                        LatestSecurityPrice latest = s.getLatest();
                        r.put("latest", latest == null ? null
                                        : Json.obj().with("date", latest.getDate().toString())
                                                        .with("value", Json.quote(latest.getValue())));
                    }
                    synchronized (job.results)
                    {
                        job.results.add(r);
                    }
                }
                if (modified > 0)
                {
                    client.markDirty();
                    holder.save();
                }
                job.modified = modified;
                holder.state().addProperty("lastPriceUpdate", Instant.now().toString());
                holder.saveState();
            }
            finally
            {
                holder.lock.unlock();
            }
            job.state = "done";
        }
        catch (Exception e) // NOSONAR
        {
            PortfolioLog.error(e);
            job.error = message(e);
            job.state = "failed";
        }
        finally
        {
            job.finished = Instant.now();
            running.remove(holder.id);
        }
    }

    /** Same rules as PP's HistoricalTask (merge, replace, replace when the data source changed). */
    private static boolean applyHistoric(Security security, QuoteFeed feed, QuoteFeedData data)
    {
        var policy = feed.getHistoricalUpdatePolicy(security);
        if (policy == QuoteFeed.HistoricalUpdatePolicy.REPLACE)
            return replace(security, data, null);
        if (policy == QuoteFeed.HistoricalUpdatePolicy.REPLACE_IF_SOURCE_CHANGED)
        {
            var identity = feed.getHistoricalDataIdentity(security);
            if (identity.isEmpty())
                return security.addAllPrices(data.getPrices());
            String stored = security.getPropertyValue(SecurityProperty.Type.FEED, QuoteFeed.HISTORICAL_DATA_IDENTITY)
                            .orElse(null);
            if (identity.get().equals(stored))
                return security.addAllPrices(data.getPrices());
            return replace(security, data, identity.get());
        }
        return security.addAllPrices(data.getPrices());
    }

    private static boolean replace(Security security, QuoteFeedData data, String identity)
    {
        if (!data.getErrors().isEmpty() || data.getPrices().isEmpty())
            return false;
        boolean had = !security.getPrices().isEmpty();
        security.removeAllPrices();
        boolean dirty = security.addAllPrices(data.getPrices()) || had;
        if (security.setPropertyValue(SecurityProperty.Type.FEED, QuoteFeed.HISTORICAL_DATA_IDENTITY, identity))
            dirty = true;
        return dirty;
    }

    /** ECB exchange rates (stored in the workspace, at most every 6 hours). */
    private void updateExchangeRates(ClientHolder holder)
    {
        if (Instant.now().isBefore(ratesUpdated.plusSeconds(6 * 3600)))
            return;
        for (ExchangeRateProvider p : ExchangeRateProviderFactory.getProviders())
        {
            try
            {
                p.update(new NullProgressMonitor());
                p.save(new NullProgressMonitor());
            }
            catch (IOException | RuntimeException e)
            {
                PortfolioLog.warning("exchange rates " + p.getName() + ": " + e.getMessage());
            }
        }
        ratesUpdated = Instant.now();
        holder.resetRates();
    }

    static void loadExchangeRates()
    {
        for (ExchangeRateProvider p : ExchangeRateProviderFactory.getProviders())
        {
            try
            {
                p.load(new NullProgressMonitor());
            }
            catch (IOException | RuntimeException e)
            {
                PortfolioLog.warning("exchange rates " + p.getName() + ": " + e.getMessage());
            }
        }
    }

    private static String message(Throwable e)
    {
        String m = e.getMessage();
        return m == null || m.isBlank() ? e.getClass().getSimpleName() : m;
    }
}
