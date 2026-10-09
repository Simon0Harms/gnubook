package gnubook.ppcore;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Properties;

/**
 * Settings of the service, read from a properties file (path in the system property {@code ppcore.config}, the
 * environment variable {@code PPCORE_CONFIG} or {@code /opt/gnubook/ppcore.properties}). Environment variables
 * {@code PPCORE_<KEY>} override single values, e.g. {@code PPCORE_TOKEN}.
 */
final class Settings
{
    final String bind;
    final int port;
    final String token;
    final Path dataDir;
    final int keepBackups;
    final int maxUploadMb;

    private Settings(Properties p)
    {
        this.bind = value(p, "bind", "127.0.0.1");
        this.port = Integer.parseInt(value(p, "port", "8091"));
        this.token = value(p, "token", "");
        this.dataDir = Path.of(value(p, "data_dir", "/opt/gnubook/data/ppcore"));
        this.keepBackups = Integer.parseInt(value(p, "keep_backups", "20"));
        this.maxUploadMb = Integer.parseInt(value(p, "max_upload_mb", "64"));
    }

    private static String value(Properties p, String key, String fallback)
    {
        String env = System.getenv("PPCORE_" + key.toUpperCase());
        if (env != null && !env.isBlank())
            return env.trim();
        String v = p.getProperty(key);
        return v == null || v.isBlank() ? fallback : v.trim();
    }

    static Settings load() throws IOException
    {
        String path = System.getProperty("ppcore.config");
        if (path == null || path.isBlank())
            path = System.getenv("PPCORE_CONFIG");
        if (path == null || path.isBlank())
            path = "/opt/gnubook/ppcore.properties";
        Properties p = new Properties();
        Path file = Path.of(path);
        if (Files.isRegularFile(file))
        {
            try (InputStream in = Files.newInputStream(file))
            {
                p.load(in);
            }
        }
        Settings s = new Settings(p);
        if (s.token.length() < 24)
            throw new IOException("token missing or shorter than 24 characters (" + path + " or PPCORE_TOKEN)");
        return s;
    }
}
