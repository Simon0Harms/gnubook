package gnubook.ppcore;

import java.io.IOException;
import java.io.Reader;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantLock;

import org.eclipse.core.runtime.NullProgressMonitor;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import name.abuchen.portfolio.model.Account;
import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.model.ClientFactory;
import name.abuchen.portfolio.model.Portfolio;
import name.abuchen.portfolio.money.CurrencyConverter;
import name.abuchen.portfolio.money.CurrencyConverterImpl;
import name.abuchen.portfolio.money.ExchangeRateProviderFactory;
import name.abuchen.portfolio.money.ExchangeRateTimeSeries;

/**
 * One Portfolio Performance file ("client") of the service. All access goes through {@link #lock}: the PP model is
 * not thread-safe.
 *
 * <pre>
 * data_dir/clients/&lt;id&gt;/portfolio.&lt;xml|portfolio|zip&gt;   the file
 * data_dir/clients/&lt;id&gt;/backups/                         earlier versions (one per save)
 * data_dir/clients/&lt;id&gt;/state.json                       import targets per bank, last price update
 * </pre>
 */
final class ClientHolder
{
    private static final List<String> EXTENSIONS = List.of("xml", "portfolio", "zip");
    private static final DateTimeFormatter STAMP = DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss-SSS");

    final String id;
    final Path dir;
    final ReentrantLock lock = new ReentrantLock(true);
    private final int keepBackups;

    private Client client;
    private Path loadedFile;
    private long loadedMtime;
    private String revision;
    private JsonObject state;
    private ExchangeRateProviderFactory rates;

    ClientHolder(String id, Path dir, int keepBackups)
    {
        this.id = id;
        this.dir = dir;
        this.keepBackups = keepBackups;
    }

    /** Acquires the lock, or fails with 409 when another long operation holds it for too long. */
    void acquire()
    {
        try
        {
            if (!lock.tryLock(120, TimeUnit.SECONDS))
                throw ApiException.conflict("busy", "the file is busy (price update or import running), try again");
        }
        catch (InterruptedException e)
        {
            Thread.currentThread().interrupt();
            throw ApiException.conflict("busy", "interrupted");
        }
    }

    // ------------------------------------------------------------------ file

    Path file()
    {
        for (String ext : EXTENSIONS)
        {
            Path p = dir.resolve("portfolio." + ext);
            if (Files.isRegularFile(p))
                return p;
        }
        return null;
    }

    boolean exists()
    {
        return file() != null;
    }

    /** The loaded client; reloads when the file changed on disk. Caller holds the lock. */
    Client client()
    {
        Path f = file();
        if (f == null)
            throw ApiException.notFound("no Portfolio Performance file for '" + id + "' yet – upload one first");
        try
        {
            long mtime = Files.getLastModifiedTime(f).toMillis();
            if (client == null || !f.equals(loadedFile) || mtime != loadedMtime)
            {
                if (ClientFactory.isEncrypted(f.toFile()))
                    throw ApiException.badRequest("the file is password protected – save it without password in PP");
                client = ClientFactory.load(f.toFile(), null, new NullProgressMonitor());
                loadedFile = f;
                loadedMtime = mtime;
                revision = sha256(f);
                rates = null;
            }
            return client;
        }
        catch (IOException e)
        {
            throw new ApiException(500, "load_failed", "cannot read " + f.getFileName() + ": " + e.getMessage());
        }
    }

    String revision()
    {
        return revision;
    }

    CurrencyConverter converter()
    {
        Client c = client();
        return new CurrencyConverterImpl(rates(), c.getBaseCurrency());
    }

    private ExchangeRateProviderFactory rates()
    {
        Client c = client();
        if (rates == null)
            rates = new ExchangeRateProviderFactory(c);
        return rates;
    }

    /** Exchange rates from {@code currency} into the base currency (lookups are empty where no rate is known). */
    ExchangeRateTimeSeries series(String currency)
    {
        try
        {
            return rates().getTimeSeries(currency, client().getBaseCurrency());
        }
        catch (RuntimeException e)
        {
            return null;
        }
    }

    /** Forget cached exchange rates (after the ECB rates were updated). */
    void resetRates()
    {
        rates = null;
    }

    /** Writes the client back to its file (atomically, keeps the previous version as backup). */
    void save() throws IOException
    {
        if (client == null || loadedFile == null)
            return;
        backup(loadedFile);
        Path tmp = dir.resolve(".saving-" + System.nanoTime() + "." + extension(loadedFile));
        try
        {
            ClientFactory.save(client, tmp.toFile());
            move(tmp, loadedFile);
        }
        finally
        {
            Files.deleteIfExists(tmp);
        }
        loadedMtime = Files.getLastModifiedTime(loadedFile).toMillis();
        revision = sha256(loadedFile);
    }

    /** Replaces the file with an uploaded one after checking that PP can read it. */
    void replace(byte[] content, String originalName) throws IOException
    {
        Files.createDirectories(dir);
        String ext = extensionOf(originalName);
        Path tmp = dir.resolve(".upload-" + System.nanoTime() + "." + ext);
        try
        {
            Files.write(tmp, content);
            if (ClientFactory.isEncrypted(tmp.toFile()))
                throw ApiException.badRequest(
                                "the file is password protected – in Portfolio Performance use 'Save as' without password");
            Client loaded;
            try
            {
                loaded = ClientFactory.load(tmp.toFile(), null, new NullProgressMonitor());
            }
            catch (IOException | RuntimeException e)
            {
                throw ApiException.badRequest("not a Portfolio Performance file: " + e.getMessage());
            }
            Path old = file();
            if (old != null)
                backup(old);
            Path target = dir.resolve("portfolio." + ext);
            move(tmp, target);
            for (String other : EXTENSIONS)
                if (!other.equals(ext))
                    Files.deleteIfExists(dir.resolve("portfolio." + other));
            client = loaded;
            loadedFile = target;
            loadedMtime = Files.getLastModifiedTime(target).toMillis();
            revision = sha256(target);
            rates = null;
            JsonObject st = state();
            st.addProperty("originalName", originalName == null ? "" : originalName);
            saveState();
        }
        finally
        {
            Files.deleteIfExists(tmp);
        }
    }

    /** Creates a new, empty file with one securities account (Depot) and one cash account. */
    void create(String currency, String portfolioName, String accountName) throws IOException
    {
        if (exists())
            throw ApiException.conflict("exists", "there is a file already");
        Files.createDirectories(dir);
        Client c = new Client();
        c.setBaseCurrency(currency);
        Account account = new Account();
        account.setName(accountName);
        account.setCurrencyCode(currency);
        c.addAccount(account);
        Portfolio portfolio = new Portfolio();
        portfolio.setName(portfolioName);
        portfolio.setReferenceAccount(account);
        c.addPortfolio(portfolio);
        Path target = dir.resolve("portfolio.xml");
        ClientFactory.save(c, target.toFile());
        client = c;
        loadedFile = target;
        loadedMtime = Files.getLastModifiedTime(target).toMillis();
        revision = sha256(target);
        rates = null;
    }

    private void backup(Path f) throws IOException
    {
        if (keepBackups <= 0 || !Files.isRegularFile(f))
            return;
        Path dirB = dir.resolve("backups");
        Files.createDirectories(dirB);
        Files.copy(f, dirB.resolve("portfolio-" + LocalDateTime.now().format(STAMP) + "." + extension(f)),
                        StandardCopyOption.REPLACE_EXISTING);
        List<Path> all = new ArrayList<>();
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(dirB, "portfolio-*"))
        {
            ds.forEach(all::add);
        }
        all.sort((a, b) -> b.getFileName().toString().compareTo(a.getFileName().toString()));
        for (int i = keepBackups; i < all.size(); i++)
            Files.deleteIfExists(all.get(i));
    }

    private static void move(Path from, Path to) throws IOException
    {
        try
        {
            Files.move(from, to, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        }
        catch (AtomicMoveNotSupportedException e)
        {
            Files.move(from, to, StandardCopyOption.REPLACE_EXISTING);
        }
    }

    private static String extension(Path p)
    {
        return extensionOf(p.getFileName().toString());
    }

    static String extensionOf(String name)
    {
        if (name != null)
        {
            int dot = name.lastIndexOf('.');
            if (dot >= 0)
            {
                String ext = name.substring(dot + 1).toLowerCase(Locale.ROOT);
                if (EXTENSIONS.contains(ext))
                    return ext;
            }
        }
        return "xml";
    }

    static String sha256(Path f) throws IOException
    {
        try
        {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(Files.readAllBytes(f)));
        }
        catch (NoSuchAlgorithmException e)
        {
            throw new IOException(e);
        }
    }

    // ------------------------------------------------------------------ state.json

    JsonObject state()
    {
        if (state == null)
        {
            Path f = dir.resolve("state.json");
            state = new JsonObject();
            if (Files.isRegularFile(f))
            {
                try (Reader r = Files.newBufferedReader(f, StandardCharsets.UTF_8))
                {
                    state = JsonParser.parseReader(r).getAsJsonObject();
                }
                catch (IOException | RuntimeException e)
                {
                    state = new JsonObject();
                }
            }
        }
        return state;
    }

    void saveState() throws IOException
    {
        Files.createDirectories(dir);
        Path tmp = dir.resolve(".state-" + System.nanoTime() + ".json");
        try (Writer w = Files.newBufferedWriter(tmp, StandardCharsets.UTF_8))
        {
            Json.GSON.toJson(state(), w);
        }
        move(tmp, dir.resolve("state.json"));
    }
}
