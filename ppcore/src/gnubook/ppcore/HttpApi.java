package gnubook.ppcore;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Pattern;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import name.abuchen.portfolio.PortfolioLog;
import name.abuchen.portfolio.model.Account;
import name.abuchen.portfolio.model.AccountTransaction;
import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.model.Portfolio;
import name.abuchen.portfolio.model.PortfolioTransaction;
import name.abuchen.portfolio.model.Security;
import name.abuchen.portfolio.online.Factory;
import name.abuchen.portfolio.online.QuoteFeed;

/**
 * JSON API of the service (see ppcore/README.md). Every request needs {@code Authorization: Bearer <token>}.
 */
final class HttpApi
{
    private static final Pattern CLIENT_ID = Pattern.compile("[a-z0-9][a-z0-9_-]{0,63}");
    private static final Pattern UUID_LIKE = Pattern.compile("[A-Za-z0-9-]{1,64}");

    private final Settings settings;
    private final Map<String, ClientHolder> clients = new ConcurrentHashMap<>();
    private final PdfImporter importer = new PdfImporter();
    private final QuoteUpdater quotes = new QuoteUpdater();
    private final String ppVersion;
    private HttpServer server;
    private ExecutorService executor;

    HttpApi(Settings settings, String ppVersion)
    {
        this.settings = settings;
        this.ppVersion = ppVersion;
    }

    void start() throws IOException
    {
        Files.createDirectories(settings.dataDir.resolve("clients"));
        server = HttpServer.create(new InetSocketAddress(settings.bind, settings.port), 32);
        executor = Executors.newFixedThreadPool(6, r -> {
            Thread t = new Thread(r, "ppcore-http");
            t.setDaemon(true);
            // PP finds its price feeds, exchange rate providers etc. with ServiceLoader via the context class loader
            t.setContextClassLoader(Client.class.getClassLoader());
            return t;
        });
        server.setExecutor(executor);
        server.createContext("/", this::handle);
        server.start();
        PortfolioLog.info("pp-core listening on " + settings.bind + ":" + settings.port);
    }

    void stop()
    {
        if (server != null)
            server.stop(1);
        if (executor != null)
            executor.shutdownNow();
    }

    // ------------------------------------------------------------------ dispatch

    private void handle(HttpExchange ex) throws IOException
    {
        try
        {
            if (!authorized(ex))
                throw new ApiException(401, "unauthorized", "missing or wrong token");
            String path = ex.getRequestURI().getRawPath();
            if (!path.startsWith("/api/v1/"))
                throw ApiException.notFound("unknown path");
            String[] seg = path.substring("/api/v1/".length()).split("/");
            for (int i = 0; i < seg.length; i++)
                seg[i] = URLDecoder.decode(seg[i], StandardCharsets.UTF_8);
            String method = ex.getRequestMethod();
            Map<String, String> query = query(ex);
            route(ex, method, seg, query);
        }
        catch (ApiException e)
        {
            sendJson(ex, e.status, Json.obj().with("error", e.code).with("message", e.getMessage()));
        }
        catch (Exception e) // NOSONAR – report everything as JSON
        {
            PortfolioLog.error(e);
            sendJson(ex, 500, Json.obj().with("error", "internal").with("message",
                            e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage()));
        }
        finally
        {
            ex.close();
        }
    }

    private void route(HttpExchange ex, String method, String[] seg, Map<String, String> q) throws Exception
    {
        if (seg.length == 1 && "health".equals(seg[0]) && "GET".equals(method))
        {
            sendJson(ex, 200, Json.obj().with("status", "ok").with("ppVersion", ppVersion).with("service", "0.1.0")
                            .with("clients", listClients()));
            return;
        }
        if (seg.length == 1 && "feeds".equals(seg[0]) && "GET".equals(method))
        {
            List<Object> feeds = new ArrayList<>();
            for (QuoteFeed f : Factory.getQuoteFeedProvider())
                feeds.add(Json.obj().with("id", f.getId()).with("name", f.getName()));
            sendJson(ex, 200, Json.obj().with("feeds", feeds));
            return;
        }
        if (seg.length < 2 || !"clients".equals(seg[0]))
            throw ApiException.notFound("unknown path");
        ClientHolder holder = holder(seg[1]);
        String sub = seg.length > 2 ? seg[2] : "";

        switch (sub)
        {
            case "" -> {
                require(method, "GET");
                sendJson(ex, 200, summary(holder));
            }
            case "file" -> file(ex, method, holder);
            case "create" -> {
                require(method, "POST");
                JsonObject body = body(ex);
                holder.acquire();
                try
                {
                    holder.create(orDefault(Json.str(body, "currency"), "EUR"),
                                    orDefault(Json.str(body, "portfolio"), "Depot"),
                                    orDefault(Json.str(body, "account"), "Verrechnungskonto"));
                }
                finally
                {
                    holder.lock.unlock();
                }
                sendJson(ex, 201, summary(holder));
            }
            case "demo" -> {
                // the fictional file of gnubook's demo; replaces only a file that was a demo file itself
                require(method, "POST");
                JsonObject body = body(ex);
                LocalDate start = date(orDefault(Json.str(body, "start"), LocalDate.now().minusMonths(13)
                                .withDayOfMonth(1).toString()), "start");
                int months = body.has("months") ? Math.max(1, Math.min(60, body.get("months").getAsInt())) : 14;
                long seed = body.has("seed") ? body.get("seed").getAsLong() : 7L;
                holder.acquire();
                try
                {
                    if (holder.exists() && !holder.state().has("demo"))
                        throw ApiException.conflict("exists", "there is a file already");
                    holder.install(Demo.build(start, months, seed));
                    holder.state().addProperty("demo", start + "/" + months + "/" + seed);
                    holder.saveState();
                }
                finally
                {
                    holder.lock.unlock();
                }
                sendJson(ex, 201, summary(holder));
            }
            case "export" -> {
                require(method, "GET");
                LocalDate since = null;
                String prices = q.getOrDefault("prices", "all");
                if ("none".equals(prices))
                    since = LocalDate.MAX;
                else if (!"all".equals(prices))
                    since = date(prices, "prices");
                holder.acquire();
                try
                {
                    sendJson(ex, 200, Exporter.export(holder, since));
                }
                finally
                {
                    holder.lock.unlock();
                }
            }
            case "import" -> {
                if (seg.length == 3)
                {
                    require(method, "POST");
                    sendJson(ex, 200, importer.extract(holder, body(ex)));
                }
                else if (seg.length == 4 && "GET".equals(method))
                {
                    var session = importer.session(holder.id, seg[3]);
                    sendJson(ex, 200, importer.applyLater(holder, session, new JsonObject()));
                }
                else if (seg.length == 5 && "apply".equals(seg[4]))
                {
                    require(method, "POST");
                    var session = importer.session(holder.id, seg[3]);
                    sendJson(ex, 200, importer.applyLater(holder, session, body(ex)));
                }
                else
                    throw ApiException.notFound("unknown path");
            }
            case "quotes" -> {
                if ("GET".equals(method))
                {
                    var job = quotes.last(holder.id);
                    sendJson(ex, 200, job == null ? Json.obj().with("state", "none") : job.describe());
                    return;
                }
                require(method, "POST");
                JsonObject body = body(ex);
                Set<String> only = new HashSet<>();
                if (body.has("securities") && body.get("securities").isJsonArray())
                    body.getAsJsonArray("securities").forEach(e -> only.add(e.getAsString()));
                holder.acquire(); // fail early when there is no file
                try
                {
                    holder.client();
                }
                finally
                {
                    holder.lock.unlock();
                }
                int wait = Math.min(1800, Math.max(0, Integer.parseInt(q.getOrDefault("wait", "0"))));
                var job = quotes.start(holder, only, wait);
                sendJson(ex, "running".equals(job.state) ? 202 : 200, job.describe());
            }
            case "performance" -> {
                require(method, "GET");
                LocalDate to = q.containsKey("to") ? date(q.get("to"), "to") : LocalDate.now();
                LocalDate from = q.containsKey("from") ? date(q.get("from"), "from") : to.minusYears(1).plusDays(1);
                if (from.isAfter(to))
                    throw ApiException.badRequest("from is after to");
                holder.acquire();
                try
                {
                    sendJson(ex, 200, Metrics.performance(holder, from, to, q.get("portfolio")));
                }
                finally
                {
                    holder.lock.unlock();
                }
            }
            case "holdings" -> {
                require(method, "GET");
                LocalDate day = q.containsKey("date") ? date(q.get("date"), "date") : LocalDate.now();
                holder.acquire();
                try
                {
                    sendJson(ex, 200, Metrics.holdings(holder, day));
                }
                finally
                {
                    holder.lock.unlock();
                }
            }
            case "search" -> {
                require(method, "GET");
                String term = q.getOrDefault("q", "").trim();
                if (term.length() < 2)
                    throw ApiException.badRequest("search term too short");
                sendJson(ex, 200, Json.obj().with("results", FeedFinder.search(term)));
            }
            case "securities" -> {
                if (seg.length != 4 || !UUID_LIKE.matcher(seg[3]).matches())
                    throw ApiException.notFound("unknown path");
                require(method, "PATCH");
                sendJson(ex, 200, updateSecurity(holder, seg[3], body(ex)));
            }
            case "transactions" -> {
                if (seg.length != 4 || !UUID_LIKE.matcher(seg[3]).matches())
                    throw ApiException.notFound("unknown path");
                require(method, "DELETE");
                sendJson(ex, 200, deleteTransaction(holder, seg[3]));
            }
            default -> throw ApiException.notFound("unknown path");
        }
    }

    // ------------------------------------------------------------------ handlers

    private void file(HttpExchange ex, String method, ClientHolder holder) throws IOException
    {
        if ("GET".equals(method))
        {
            holder.acquire();
            byte[] data;
            String name;
            try
            {
                holder.client(); // 404 when missing
                data = Files.readAllBytes(holder.file());
                name = holder.file().getFileName().toString();
            }
            finally
            {
                holder.lock.unlock();
            }
            ex.getResponseHeaders().add("Content-Type", "application/octet-stream");
            ex.getResponseHeaders().add("Content-Disposition", "attachment; filename=\"" + name + "\"");
            ex.getResponseHeaders().add("X-Revision", holder.revision());
            ex.sendResponseHeaders(200, data.length);
            try (OutputStream out = ex.getResponseBody())
            {
                out.write(data);
            }
            return;
        }
        require(method, "PUT");
        byte[] content = read(ex);
        if (content.length == 0)
            throw ApiException.badRequest("empty file");
        String name = ex.getRequestHeaders().getFirst("X-Filename");
        holder.acquire();
        try
        {
            holder.replace(content, name);
            holder.forgetDemo();
        }
        finally
        {
            holder.lock.unlock();
        }
        sendJson(ex, 200, summary(holder));
    }

    private Map<String, Object> summary(ClientHolder holder)
    {
        holder.acquire();
        try
        {
            Json.Obj o = Json.obj().with("id", holder.id).with("exists", holder.exists());
            if (holder.exists())
            {
                Client c = holder.client();
                long txs = c.getAccounts().stream().mapToLong(a -> a.getTransactions().size()).sum()
                                + c.getPortfolios().stream().mapToLong(p -> p.getTransactions().size()).sum();
                o.put("file", holder.file().getFileName().toString());
                o.put("size", holder.file().toFile().length());
                o.put("modified", java.time.Instant.ofEpochMilli(holder.file().toFile().lastModified()).toString());
                o.put("revision", holder.revision());
                o.put("baseCurrency", c.getBaseCurrency());
                o.put("securities", c.getSecurities().size());
                o.put("accounts", c.getAccounts().size());
                o.put("portfolios", c.getPortfolios().size());
                o.put("transactions", txs);
                var first = java.util.stream.Stream
                                .concat(c.getAccounts().stream().flatMap(a -> a.getTransactions().stream()),
                                                c.getPortfolios().stream().flatMap(p -> p.getTransactions().stream()))
                                .map(t -> t.getDateTime().toLocalDate()).min(java.util.Comparator.naturalOrder());
                o.put("firstDate", first.map(LocalDate::toString).orElse(null));
                List<Object> ports = new ArrayList<>();
                for (Portfolio p : c.getPortfolios())
                    ports.add(Json.obj().with("uuid", p.getUUID()).with("name", p.getName()).with("retired",
                                    p.isRetired()));
                o.put("portfolioList", ports);
                List<Object> accs = new ArrayList<>();
                for (Account a : c.getAccounts())
                    accs.add(Json.obj().with("uuid", a.getUUID()).with("name", a.getName())
                                    .with("currency", a.getCurrencyCode()).with("retired", a.isRetired()));
                o.put("accountList", accs);
            }
            JsonObject st = holder.state();
            o.put("originalName", Json.str(st, "originalName"));
            o.put("lastPriceUpdate", Json.str(st, "lastPriceUpdate"));
            o.put("demo", Json.str(st, "demo"));
            o.put("importTargets", st.has("importTargets") ? Json.GSON.fromJson(st.get("importTargets"), Map.class)
                            : Map.of());
            return o;
        }
        finally
        {
            holder.lock.unlock();
        }
    }

    private Map<String, Object> updateSecurity(ClientHolder holder, String uuid, JsonObject body) throws IOException
    {
        holder.acquire();
        try
        {
            Client client = holder.client();
            Security s = client.getSecurities().stream().filter(x -> x.getUUID().equals(uuid)).findFirst()
                            .orElseThrow(() -> ApiException.notFound("unknown security"));
            boolean feedChanged = false;
            if (body.has("feed"))
            {
                String feed = Json.str(body, "feed");
                if (feed != null && !feed.isBlank() && !QuoteFeed.MANUAL.equals(feed)
                                && Factory.getQuoteFeedProvider(feed) == null)
                    throw ApiException.badRequest("unknown price source " + feed);
                s.setFeed(feed == null || feed.isBlank() ? QuoteFeed.MANUAL : feed);
                feedChanged = true;
            }
            if (body.has("ticker"))
            {
                s.setTickerSymbol(blankToNull(Json.str(body, "ticker")));
                feedChanged = true;
            }
            if (body.has("feedUrl"))
            {
                s.setFeedURL(blankToNull(Json.str(body, "feedUrl")));
                feedChanged = true;
            }
            if (body.has("latestFeed"))
            {
                String feed = blankToNull(Json.str(body, "latestFeed"));
                if (feed != null && Factory.getQuoteFeedProvider(feed) == null)
                    throw ApiException.badRequest("unknown price source " + feed);
                s.setLatestFeed(feed);
                feedChanged = true;
            }
            if (body.has("latestFeedUrl"))
            {
                s.setLatestFeedURL(blankToNull(Json.str(body, "latestFeedUrl")));
                feedChanged = true;
            }
            if (body.has("name") && Json.str(body, "name") != null && !Json.str(body, "name").isBlank())
                s.setName(Json.str(body, "name").trim());
            if (body.has("isin"))
                s.setIsin(blankToNull(Json.str(body, "isin")));
            if (body.has("wkn"))
                s.setWkn(blankToNull(Json.str(body, "wkn")));
            if (body.has("retired"))
                s.setRetired(Json.bool(body, "retired", false));
            if (feedChanged)
                s.getEphemeralData().touchFeedConfigurationChanged();
            client.markDirty();
            holder.save();
            return Json.obj().with("uuid", s.getUUID()).with("name", s.getName()).with("feed", s.getFeed())
                            .with("ticker", s.getTickerSymbol()).with("feedUrl", s.getFeedURL())
                            .with("latestFeed", s.getLatestFeed()).with("revision", holder.revision());
        }
        finally
        {
            holder.lock.unlock();
        }
    }

    private Map<String, Object> deleteTransaction(ClientHolder holder, String uuid) throws IOException
    {
        holder.acquire();
        try
        {
            Client client = holder.client();
            for (Account a : client.getAccounts())
                for (AccountTransaction t : new ArrayList<>(a.getTransactions()))
                    if (t.getUUID().equals(uuid))
                    {
                        a.deleteTransaction(t, client);
                        client.markDirty();
                        holder.save();
                        return Json.obj().with("deleted", uuid).with("revision", holder.revision());
                    }
            for (Portfolio p : client.getPortfolios())
                for (PortfolioTransaction t : new ArrayList<>(p.getTransactions()))
                    if (t.getUUID().equals(uuid))
                    {
                        p.deleteTransaction(t, client);
                        client.markDirty();
                        holder.save();
                        return Json.obj().with("deleted", uuid).with("revision", holder.revision());
                    }
            throw ApiException.notFound("unknown transaction");
        }
        finally
        {
            holder.lock.unlock();
        }
    }

    // ------------------------------------------------------------------ helpers

    private ClientHolder holder(String id)
    {
        if (!CLIENT_ID.matcher(id).matches())
            throw ApiException.badRequest("invalid client id");
        return clients.computeIfAbsent(id,
                        k -> new ClientHolder(k, settings.dataDir.resolve("clients").resolve(k), settings.keepBackups));
    }

    private List<Object> listClients() throws IOException
    {
        List<Object> out = new ArrayList<>();
        var dir = settings.dataDir.resolve("clients");
        if (Files.isDirectory(dir))
            try (var ds = Files.newDirectoryStream(dir))
            {
                for (var p : ds)
                    if (Files.isDirectory(p) && CLIENT_ID.matcher(p.getFileName().toString()).matches())
                        out.add(p.getFileName().toString());
            }
        return out;
    }

    private boolean authorized(HttpExchange ex)
    {
        String header = ex.getRequestHeaders().getFirst("Authorization");
        if (header == null || !header.startsWith("Bearer "))
            return false;
        byte[] sent = header.substring(7).trim().getBytes(StandardCharsets.UTF_8);
        return MessageDigest.isEqual(sent, settings.token.getBytes(StandardCharsets.UTF_8));
    }

    private static void require(String method, String expected)
    {
        if (!expected.equals(method))
            throw new ApiException(405, "method_not_allowed", "use " + expected);
    }

    private byte[] read(HttpExchange ex) throws IOException
    {
        long limit = settings.maxUploadMb * 1024L * 1024L;
        try (InputStream in = ex.getRequestBody(); ByteArrayOutputStream out = new ByteArrayOutputStream())
        {
            byte[] buf = new byte[65536];
            long total = 0;
            int n;
            while ((n = in.read(buf)) > 0)
            {
                total += n;
                if (total > limit)
                    throw new ApiException(413, "too_large", "request larger than " + settings.maxUploadMb + " MB");
                out.write(buf, 0, n);
            }
            return out.toByteArray();
        }
    }

    private JsonObject body(HttpExchange ex) throws IOException
    {
        byte[] data = read(ex);
        if (data.length == 0)
            return new JsonObject();
        try
        {
            return JsonParser.parseString(new String(data, StandardCharsets.UTF_8)).getAsJsonObject();
        }
        catch (RuntimeException e)
        {
            throw ApiException.badRequest("body is not a JSON object");
        }
    }

    private static Map<String, String> query(HttpExchange ex)
    {
        Map<String, String> out = new HashMap<>();
        String raw = ex.getRequestURI().getRawQuery();
        if (raw == null || raw.isEmpty())
            return out;
        for (String part : raw.split("&"))
        {
            int eq = part.indexOf('=');
            String k = URLDecoder.decode(eq < 0 ? part : part.substring(0, eq), StandardCharsets.UTF_8);
            String v = eq < 0 ? "" : URLDecoder.decode(part.substring(eq + 1), StandardCharsets.UTF_8);
            out.put(k, v);
        }
        return out;
    }

    private static LocalDate date(String value, String name)
    {
        try
        {
            return LocalDate.parse(value);
        }
        catch (DateTimeParseException | NullPointerException e)
        {
            throw ApiException.badRequest(name + ": expected YYYY-MM-DD");
        }
    }

    private static String orDefault(String v, String fallback)
    {
        return v == null || v.isBlank() ? fallback : v.trim();
    }

    private static String blankToNull(String v)
    {
        return v == null || v.isBlank() ? null : v.trim();
    }

    private static void sendJson(HttpExchange ex, int status, Object body) throws IOException
    {
        byte[] data = Json.GSON.toJson(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream out = ex.getResponseBody())
        {
            out.write(data);
        }
    }
}
