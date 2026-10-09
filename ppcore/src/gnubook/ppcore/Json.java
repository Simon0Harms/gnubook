package gnubook.ppcore;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.Map;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;

import name.abuchen.portfolio.money.Money;
import name.abuchen.portfolio.money.Values;

/** JSON helpers. Amounts are written as decimal strings so nothing is lost on the way to Python. */
final class Json
{
    static final Gson GSON = new GsonBuilder().disableHtmlEscaping().serializeNulls().create();

    private Json()
    {
    }

    /** Insertion-ordered map with a fluent put. */
    static final class Obj extends LinkedHashMap<String, Object>
    {
        private static final long serialVersionUID = 1L;

        Obj with(String key, Object value)
        {
            put(key, value);
            return this;
        }
    }

    static Obj obj()
    {
        return new Obj();
    }

    static String amount(long value)
    {
        return decimal(value, Values.Amount.precision());
    }

    static String shares(long value)
    {
        return decimal(value, Values.Share.precision());
    }

    static String quote(long value)
    {
        return decimal(value, Values.Quote.precision());
    }

    static String decimal(long unscaled, int scale)
    {
        BigDecimal d = BigDecimal.valueOf(unscaled, scale).stripTrailingZeros();
        if (d.scale() < 0)
            d = d.setScale(0);
        return d.toPlainString();
    }

    static Map<String, Object> money(Money m)
    {
        if (m == null)
            return null;
        return obj().with("amount", amount(m.getAmount())).with("currency", m.getCurrencyCode());
    }

    static String date(LocalDate d)
    {
        return d == null ? null : d.toString();
    }

    static String dateTime(LocalDateTime d)
    {
        return d == null ? null : d.toString();
    }

    static double finite(double v)
    {
        return Double.isFinite(v) ? v : 0d;
    }

    static Double finiteOrNull(Double v)
    {
        return v == null || !Double.isFinite(v) ? null : v;
    }

    static String str(JsonObject o, String key)
    {
        JsonElement e = o == null ? null : o.get(key);
        return e == null || e.isJsonNull() ? null : e.getAsString();
    }

    static boolean bool(JsonObject o, String key, boolean fallback)
    {
        JsonElement e = o == null ? null : o.get(key);
        return e == null || e.isJsonNull() ? fallback : e.getAsBoolean();
    }
}
