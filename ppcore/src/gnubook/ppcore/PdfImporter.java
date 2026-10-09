package gnubook.ppcore;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collection;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Collectors;

import org.eclipse.core.runtime.NullProgressMonitor;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;

import name.abuchen.portfolio.datatransfer.Extractor;
import name.abuchen.portfolio.datatransfer.ImportAction;
import name.abuchen.portfolio.datatransfer.ImportAction.Status;
import name.abuchen.portfolio.datatransfer.actions.CheckCurrenciesAction;
import name.abuchen.portfolio.datatransfer.actions.CheckForexGrossValueAction;
import name.abuchen.portfolio.datatransfer.actions.CheckSecurityRelatedValuesAction;
import name.abuchen.portfolio.datatransfer.actions.CheckTransactionDateAction;
import name.abuchen.portfolio.datatransfer.actions.CheckValidTypesAction;
import name.abuchen.portfolio.datatransfer.actions.DetectDuplicatesAction;
import name.abuchen.portfolio.datatransfer.actions.InsertAction;
import name.abuchen.portfolio.datatransfer.pdf.PDFImportAssistant;
import name.abuchen.portfolio.model.Account;
import name.abuchen.portfolio.model.Annotated;
import name.abuchen.portfolio.model.BuySellEntry;
import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.model.Portfolio;
import name.abuchen.portfolio.model.Security;
import name.abuchen.portfolio.model.Transaction;
import name.abuchen.portfolio.model.Transaction.Unit;
import name.abuchen.portfolio.money.Money;

/**
 * PDF import like PP's import wizard, without the wizard: extract all files, check every item with PP's own checks
 * (duplicates, types, currencies, ...) and insert the items whose status is OK. Items with a warning (e.g. a
 * possible duplicate) are only inserted when they are explicitly forced later, as in PP.
 *
 * Target securities account and cash account are chosen per bank (extractor): explicitly given, the choice of the
 * last import from that bank, or the only active one. When there is no unambiguous choice the session waits for one.
 */
final class PdfImporter
{
    private static final Duration KEEP = Duration.ofHours(6);

    /** One extracted item. */
    static final class Entry
    {
        final int index;
        final String extractor;
        final String file;
        final Extractor.Item item;
        final List<Status> status = new ArrayList<>();
        Status.Code maxCode = Status.Code.OK;
        Entry dependency;
        boolean imported;
        String error;

        Entry(int index, String extractor, String file, Extractor.Item item)
        {
            this.index = index;
            this.extractor = extractor;
            this.file = file;
            this.item = item;
        }

        void add(Status s)
        {
            if (s == null)
                return;
            for (Status known : status)
                if (known.getCode() == s.getCode() && java.util.Objects.equals(known.getMessage(), s.getMessage()))
                    return;
            status.add(s);
            if (s.getCode().isHigherSeverityAs(maxCode))
                maxCode = s.getCode();
        }

        boolean importable(Set<Integer> forced)
        {
            if (imported || item.isFailure() || item.isSkipped())
                return false;
            if (dependency != null && !dependency.imported && !dependency.importable(forced))
                return false;
            return maxCode == Status.Code.OK || (maxCode == Status.Code.WARNING
                            && (forced.contains(index) || item.isInvestmentPlanItem()));
        }
    }

    /** Target securities account and cash accounts (per currency) for one bank. */
    static final class Target
    {
        String portfolio;
        final Map<String, String> accounts = new LinkedHashMap<>();
    }

    static final class Session
    {
        final String id = UUID.randomUUID().toString();
        final String clientId;
        final Instant created = Instant.now();
        final List<Entry> entries = new ArrayList<>();
        final List<Map<String, Object>> fileErrors = new ArrayList<>();
        final Map<String, Target> targets = new LinkedHashMap<>();
        final Set<String> missingTargets = new HashSet<>();
        final List<String> newSecurities = new ArrayList<>();

        Session(String clientId)
        {
            this.clientId = clientId;
        }
    }

    /** Context for PP's import actions: resolves the target accounts of one bank. */
    private static final class TargetContext implements ImportAction.Context
    {
        private final Client client;
        private final Target target;

        TargetContext(Client client, Target target)
        {
            this.client = client;
            this.target = target;
        }

        @Override
        public Account getAccount(String currencyCode)
        {
            String uuid = target.accounts.get(currencyCode);
            if (uuid != null)
                return findAccount(client, uuid);
            List<Account> candidates = client.getActiveAccounts().stream()
                            .filter(a -> currencyCode != null && currencyCode.equals(a.getCurrencyCode())).toList();
            return candidates.size() == 1 ? candidates.get(0) : null;
        }

        @Override
        public Portfolio getPortfolio()
        {
            if (target.portfolio != null)
                return findPortfolio(client, target.portfolio);
            List<Portfolio> active = client.getActivePortfolios();
            return active.size() == 1 ? active.get(0) : null;
        }

        @Override
        public Account getSecondaryAccount(String currencyCode)
        {
            return null; // transfers between two of our accounts need a choice in PP itself
        }

        @Override
        public Portfolio getSecondaryPortfolio()
        {
            return null;
        }
    }

    private final Map<String, Session> sessions = new ConcurrentHashMap<>();

    Session session(String clientId, String id)
    {
        cleanup();
        Session s = sessions.get(id);
        if (s == null || !s.clientId.equals(clientId))
            throw ApiException.notFound("import session expired or unknown");
        return s;
    }

    private void cleanup()
    {
        Instant limit = Instant.now().minus(KEEP);
        sessions.values().removeIf(s -> s.created.isBefore(limit));
    }

    /**
     * Extracts the uploaded files. Body: {"files": [{"name": "...", "data": base64}], "portfolio": uuid?,
     * "account": uuid?, "apply": true|false, "autoFeed": true|false}
     */
    Map<String, Object> extract(ClientHolder holder, JsonObject body) throws IOException
    {
        JsonArray files = body.has("files") && body.get("files").isJsonArray() ? body.getAsJsonArray("files") : null;
        if (files == null || files.isEmpty())
            throw ApiException.badRequest("no files");
        Path tmp = Files.createTempDirectory("ppcore-import-");
        Session session = new Session(holder.id);
        try
        {
            List<File> inputs = new ArrayList<>();
            Set<String> used = new HashSet<>();
            for (JsonElement e : files)
            {
                JsonObject f = e.getAsJsonObject();
                String name = safeName(Json.str(f, "name"), used);
                byte[] data;
                try
                {
                    data = Base64.getDecoder().decode(Json.str(f, "data"));
                }
                catch (IllegalArgumentException | NullPointerException ex)
                {
                    throw ApiException.badRequest("file " + name + " is not base64 encoded");
                }
                Path p = tmp.resolve(name);
                Files.write(p, data);
                inputs.add(p.toFile());
            }

            holder.acquire();
            try
            {
                Client client = holder.client();
                Map<File, List<Exception>> errors = new HashMap<>();
                Map<Extractor, List<Extractor.Item>> result = new PDFImportAssistant(client, inputs)
                                .run(new NullProgressMonitor(), errors);
                for (Map.Entry<File, List<Exception>> err : errors.entrySet())
                    for (Exception ex : err.getValue())
                        session.fileErrors.add(Json.obj().with("file", err.getKey().getName()).with("message",
                                        ex.getMessage() == null ? ex.getClass().getSimpleName() : ex.getMessage()));
                Set<String> extractedFiles = new HashSet<>();
                int i = 0;
                for (Map.Entry<Extractor, List<Extractor.Item>> r : result.entrySet())
                {
                    String label = r.getKey().getLabel();
                    for (Extractor.Item item : r.getValue())
                    {
                        String file = sourceFile(item);
                        if (file != null)
                            extractedFiles.add(file);
                        session.entries.add(new Entry(i++, label, file, item));
                    }
                }
                for (File f : inputs)
                    if (!extractedFiles.contains(f.getName())
                                    && session.fileErrors.stream().noneMatch(m -> f.getName().equals(m.get("file"))))
                        session.fileErrors.add(Json.obj().with("file", f.getName()).with("message",
                                        "no transactions recognised (unsupported bank or document type)"));
                setupDependencies(session);
                resolveTargets(holder, client, session, Json.str(body, "portfolio"), Json.str(body, "account"));
                check(client, session);
                sessions.put(session.id, session);
                if (Json.bool(body, "apply", true) && session.missingTargets.isEmpty())
                    apply(holder, client, session, Set.of(), Json.bool(body, "autoFeed", true));
                return describe(holder, client, session);
            }
            finally
            {
                holder.lock.unlock();
            }
        }
        finally
        {
            try (var stream = Files.list(tmp))
            {
                stream.forEach(p -> p.toFile().delete());
            }
            Files.deleteIfExists(tmp);
        }
    }

    /** Body: {"force": [index...], "portfolio": uuid?, "account": uuid?, "extractor": label?} */
    Map<String, Object> applyLater(ClientHolder holder, Session session, JsonObject body) throws IOException
    {
        holder.acquire();
        try
        {
            Client client = holder.client();
            String portfolio = Json.str(body, "portfolio");
            String account = Json.str(body, "account");
            if (portfolio != null || account != null)
            {
                String only = Json.str(body, "extractor");
                for (String label : new ArrayList<>(session.targets.keySet()))
                    if (only == null || only.equals(label))
                        setTarget(client, session, label, portfolio, account);
                session.missingTargets.clear();
                for (Entry e : session.entries)
                    if (needsTarget(client, session, e))
                        session.missingTargets.add(e.extractor);
            }
            check(client, session);
            Set<Integer> forced = new HashSet<>();
            if (body.has("force") && body.get("force").isJsonArray())
                body.getAsJsonArray("force").forEach(e -> forced.add(e.getAsInt()));
            if (!session.missingTargets.isEmpty())
                throw ApiException.conflict("target_missing", "choose the securities account and cash account first");
            apply(holder, client, session, forced, Json.bool(body, "autoFeed", true));
            return describe(holder, client, session);
        }
        finally
        {
            holder.lock.unlock();
        }
    }

    // ------------------------------------------------------------------ steps

    private static void setupDependencies(Session session)
    {
        Map<Security, Entry> bySecurity = new HashMap<>();
        for (Entry e : session.entries)
            if (e.item instanceof Extractor.SecurityItem)
                bySecurity.put(e.item.getSecurity(), e);
        for (Entry e : session.entries)
            if (!(e.item instanceof Extractor.SecurityItem) && e.item.getSecurity() != null)
                e.dependency = bySecurity.get(e.item.getSecurity());
    }

    private static void resolveTargets(ClientHolder holder, Client client, Session session, String portfolio,
                    String account)
    {
        JsonObject saved = holder.state().has("importTargets") ? holder.state().getAsJsonObject("importTargets")
                        : new JsonObject();
        Set<String> labels = session.entries.stream().map(e -> e.extractor)
                        .collect(Collectors.toCollection(java.util.LinkedHashSet::new));
        for (String label : labels)
        {
            Target t = new Target();
            JsonObject s = saved.has(label) ? saved.getAsJsonObject(label) : null;
            if (s != null)
            {
                String p = Json.str(s, "portfolio");
                if (p != null && findPortfolio(client, p) != null)
                    t.portfolio = p;
                if (s.has("accounts"))
                    for (Map.Entry<String, JsonElement> a : s.getAsJsonObject("accounts").entrySet())
                        if (findAccount(client, a.getValue().getAsString()) != null)
                            t.accounts.put(a.getKey(), a.getValue().getAsString());
            }
            session.targets.put(label, t);
            if (portfolio != null || account != null)
                setTarget(client, session, label, portfolio, account);
        }
        for (Entry e : session.entries)
            if (needsTarget(client, session, e))
                session.missingTargets.add(e.extractor);
    }

    private static void setTarget(Client client, Session session, String label, String portfolio, String account)
    {
        Target t = session.targets.computeIfAbsent(label, k -> new Target());
        if (portfolio != null)
        {
            if (findPortfolio(client, portfolio) == null)
                throw ApiException.badRequest("unknown securities account " + portfolio);
            t.portfolio = portfolio;
        }
        if (account != null)
        {
            Account a = findAccount(client, account);
            if (a == null)
                throw ApiException.badRequest("unknown cash account " + account);
            t.accounts.put(a.getCurrencyCode(), a.getUUID());
        }
    }

    /** True when an item of this entry would need a target that cannot be chosen automatically. */
    private static boolean needsTarget(Client client, Session session, Entry e)
    {
        if (e.item instanceof Extractor.SecurityItem || e.item.isFailure() || e.item.isSkipped())
            return false;
        TargetContext ctx = new TargetContext(client, session.targets.get(e.extractor));
        Annotated subject = e.item.getSubject();
        if (subject instanceof BuySellEntry entry)
            return (e.item.getPortfolioPrimary() == null && ctx.getPortfolio() == null)
                            || (e.item.getAccountPrimary() == null
                                            && ctx.getAccount(entry.getAccountTransaction().getCurrencyCode()) == null);
        if (subject instanceof name.abuchen.portfolio.model.PortfolioTransaction)
            return e.item.getPortfolioPrimary() == null && ctx.getPortfolio() == null;
        if (subject instanceof name.abuchen.portfolio.model.AccountTransaction at)
            return e.item.getAccountPrimary() == null && ctx.getAccount(at.getCurrencyCode()) == null;
        return false;
    }

    private static void check(Client client, Session session)
    {
        List<ImportAction> actions = List.of(new CheckTransactionDateAction(), new CheckValidTypesAction(),
                        new CheckSecurityRelatedValuesAction(), new DetectDuplicatesAction(client),
                        new CheckCurrenciesAction(), new CheckForexGrossValueAction());
        for (Entry e : session.entries)
        {
            if (e.imported)
                continue;
            e.status.clear();
            e.maxCode = Status.Code.OK;
            if (e.item.isFailure())
            {
                e.add(new Status(Status.Code.ERROR, e.item.getFailureMessage()));
                continue;
            }
            if (e.item.isSkipped())
            {
                e.add(new Status(Status.Code.SKIP, ((Extractor.SkippedItem) e.item).getSkipReason()));
                continue;
            }
            if (session.missingTargets.contains(e.extractor) && needsTarget(client, session, e))
            {
                e.add(new Status(Status.Code.ERROR, "target account missing"));
                continue;
            }
            TargetContext ctx = new TargetContext(client, session.targets.get(e.extractor));
            for (ImportAction action : actions)
            {
                try
                {
                    e.add(e.item.apply(action, ctx));
                }
                catch (RuntimeException ex)
                {
                    e.add(new Status(Status.Code.ERROR, ex.getMessage() == null ? ex.toString() : ex.getMessage()));
                }
            }
        }
    }

    private void apply(ClientHolder holder, Client client, Session session, Set<Integer> forced, boolean autoFeed)
                    throws IOException
    {
        List<Entry> todo = session.entries.stream().filter(e -> e.importable(forced))
                        .sorted(Comparator.comparing((Entry e) -> e.item instanceof Extractor.SecurityItem ? 0 : 1)
                                        .thenComparing(e -> e.item.getDate() == null ? java.time.LocalDateTime.MIN
                                                        : e.item.getDate()))
                        .toList();
        if (todo.isEmpty())
            return;
        Set<Security> before = new HashSet<>(client.getSecurities());
        for (Entry e : todo)
        {
            TargetContext ctx = new TargetContext(client, session.targets.get(e.extractor));
            InsertAction action = new InsertAction(client);
            action.setInvestmentPlanItem(e.item.isInvestmentPlanItem());
            try
            {
                Status s = e.item.apply(action, ctx);
                if (s != null && s.getCode() == Status.Code.ERROR)
                    e.error = s.getMessage();
                else
                    e.imported = true;
            }
            catch (RuntimeException ex)
            {
                e.error = ex.getMessage() == null ? ex.toString() : ex.getMessage();
            }
        }
        List<Security> added = client.getSecurities().stream().filter(s -> !before.contains(s)).toList();
        for (Security s : added)
            session.newSecurities.add(s.getUUID());
        client.markDirty();
        rememberTargets(holder, client, session);
        if (autoFeed && !added.isEmpty())
            FeedFinder.configure(added);
        holder.save();
    }

    private static void rememberTargets(ClientHolder holder, Client client, Session session) throws IOException
    {
        JsonObject state = holder.state();
        JsonObject saved = state.has("importTargets") ? state.getAsJsonObject("importTargets") : new JsonObject();
        for (Map.Entry<String, Target> t : session.targets.entrySet())
        {
            boolean used = session.entries.stream().anyMatch(e -> e.imported && e.extractor.equals(t.getKey()));
            if (!used)
                continue;
            TargetContext ctx = new TargetContext(client, t.getValue());
            JsonObject o = new JsonObject();
            Portfolio p = ctx.getPortfolio();
            if (p != null)
                o.addProperty("portfolio", p.getUUID());
            JsonObject accounts = new JsonObject();
            for (Entry e : session.entries)
            {
                if (!e.imported || !e.extractor.equals(t.getKey()))
                    continue;
                String currency = cashCurrency(e.item);
                Account a = currency == null ? null : ctx.getAccount(currency);
                if (a != null)
                    accounts.addProperty(currency, a.getUUID());
            }
            o.add("accounts", accounts);
            saved.add(t.getKey(), o);
        }
        state.add("importTargets", saved);
        holder.saveState();
    }

    private static String cashCurrency(Extractor.Item item)
    {
        Annotated subject = item.getSubject();
        if (subject instanceof BuySellEntry entry)
            return entry.getAccountTransaction().getCurrencyCode();
        if (subject instanceof name.abuchen.portfolio.model.AccountTransaction at)
            return at.getCurrencyCode();
        return null;
    }

    // ------------------------------------------------------------------ output

    private static Map<String, Object> describe(ClientHolder holder, Client client, Session session)
    {
        List<Object> items = new ArrayList<>();
        for (Entry e : session.entries)
        {
            Json.Obj o = Json.obj().with("index", e.index).with("extractor", e.extractor).with("file", e.file)
                            .with("kind", e.item.getClass().getSimpleName())
                            .with("type", safe(e.item::getTypeInformation))
                            .with("date", e.item.getDate() == null ? null : e.item.getDate().toString());
            Security sec = e.item.getSecurity();
            o.put("security", sec == null ? null
                            : Json.obj().with("name", sec.getName()).with("isin", sec.getIsin())
                                            .with("wkn", sec.getWkn()).with("uuid", sec.getUUID()));
            Money amount = null;
            try
            {
                amount = e.item.getAmount();
            }
            catch (RuntimeException ignore)
            {
                // some items have no amount
            }
            o.put("amount", Json.money(amount));
            long shares = 0;
            try
            {
                shares = e.item.getShares();
            }
            catch (RuntimeException ignore)
            {
                // no shares
            }
            o.put("shares", shares == 0 ? null : Json.shares(shares));
            Transaction tx = mainTransaction(e.item);
            if (tx != null)
            {
                o.put("fees", Json.money(tx.getUnitSum(Unit.Type.FEE)));
                o.put("taxes", Json.money(tx.getUnitSum(Unit.Type.TAX)));
                o.put("uuid", e.imported ? tx.getUUID() : null);
            }
            o.put("status", e.maxCode.name());
            List<Object> messages = new ArrayList<>();
            for (Status s : e.status)
                if (s.getMessage() != null && !s.getMessage().isBlank())
                    messages.add(Json.obj().with("code", s.getCode().name()).with("message", s.getMessage()));
            o.put("messages", messages);
            o.put("imported", e.imported);
            o.put("error", e.error);
            items.add(o);
        }
        List<Object> targets = new ArrayList<>();
        for (Map.Entry<String, Target> t : session.targets.entrySet())
        {
            TargetContext ctx = new TargetContext(client, t.getValue());
            Portfolio p = ctx.getPortfolio();
            Map<String, Object> accs = new LinkedHashMap<>();
            for (Entry e : session.entries)
            {
                if (!e.extractor.equals(t.getKey()))
                    continue;
                String currency = cashCurrency(e.item);
                if (currency != null && !accs.containsKey(currency))
                {
                    Account a = ctx.getAccount(currency);
                    accs.put(currency, a == null ? null : Json.obj().with("uuid", a.getUUID()).with("name", a.getName()));
                }
            }
            targets.add(Json.obj().with("extractor", t.getKey())
                            .with("portfolio", p == null ? null
                                            : Json.obj().with("uuid", p.getUUID()).with("name", p.getName()))
                            .with("accounts", accs).with("missing", session.missingTargets.contains(t.getKey())));
        }
        long imported = session.entries.stream().filter(e -> e.imported).count();
        return Json.obj().with("session", session.id).with("revision", holder.revision()).with("items", items)
                        .with("fileErrors", session.fileErrors).with("targets", targets)
                        .with("needsTarget", !session.missingTargets.isEmpty()).with("imported", imported)
                        .with("newSecurities", session.newSecurities)
                        .with("portfolios", choices(client.getActivePortfolios()))
                        .with("accounts", accountChoices(client.getActiveAccounts()));
    }

    private static List<Object> choices(Collection<Portfolio> portfolios)
    {
        List<Object> out = new ArrayList<>();
        for (Portfolio p : portfolios)
            out.add(Json.obj().with("uuid", p.getUUID()).with("name", p.getName()));
        return out;
    }

    private static List<Object> accountChoices(Collection<Account> accounts)
    {
        List<Object> out = new ArrayList<>();
        for (Account a : accounts)
            out.add(Json.obj().with("uuid", a.getUUID()).with("name", a.getName()).with("currency",
                            a.getCurrencyCode()));
        return out;
    }

    private static Transaction mainTransaction(Extractor.Item item)
    {
        Annotated subject = item.getSubject();
        if (subject instanceof BuySellEntry entry)
            return entry.getPortfolioTransaction();
        if (subject instanceof Transaction t)
            return t;
        return null;
    }

    private static String sourceFile(Extractor.Item item)
    {
        try
        {
            String s = item.getSource();
            return s == null || s.isBlank() ? null : s;
        }
        catch (RuntimeException e)
        {
            return null;
        }
    }

    private static String safe(java.util.function.Supplier<String> s)
    {
        try
        {
            return s.get();
        }
        catch (RuntimeException e)
        {
            return null;
        }
    }

    private static String safeName(String name, Set<String> used)
    {
        String base = name == null ? "document.pdf" : Path.of(name).getFileName().toString();
        base = base.replaceAll("[^A-Za-z0-9._ -]", "_");
        if (base.isBlank() || base.startsWith("."))
            base = "document" + base;
        String candidate = base;
        int n = 1;
        while (!used.add(candidate))
            candidate = (n++) + "-" + base;
        return candidate;
    }

    static Account findAccount(Client client, String uuid)
    {
        return client.getAccounts().stream().filter(a -> a.getUUID().equals(uuid)).findFirst().orElse(null);
    }

    static Portfolio findPortfolio(Client client, String uuid)
    {
        return client.getPortfolios().stream().filter(p -> p.getUUID().equals(uuid)).findFirst().orElse(null);
    }
}
