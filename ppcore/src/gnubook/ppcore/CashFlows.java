package gnubook.ppcore;

import java.io.IOException;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.time.LocalTime;
import java.util.ArrayList;
import java.util.EnumSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;

import name.abuchen.portfolio.model.Account;
import name.abuchen.portfolio.model.AccountTransaction;
import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.money.Values;

/**
 * Cash account transactions mirrored from the GnuCash book: deposits and removals (and interest, fees, taxes)
 * that were entered in GnuCash on the account of a PP cash account.
 *
 * <p>Body of {@code POST /clients/{id}/cashflows}: {@code create} – a list of {@code account} (UUID),
 * {@code type} (DEPOSIT, REMOVAL, INTEREST, INTEREST_CHARGE, FEES, FEES_REFUND, TAXES, TAX_REFUND),
 * {@code date} (YYYY-MM-DD), {@code amount} (positive decimal in the account's currency), optional {@code note}
 * and {@code source} (gnubook's key, so the transaction is found again); {@code delete} – UUIDs of cash account
 * transactions of these types without cross entry. Everything is checked first and written with one save, so a
 * refused request leaves the file untouched.
 */
final class CashFlows
{
    private static final Set<AccountTransaction.Type> TYPES = EnumSet.of(AccountTransaction.Type.DEPOSIT,
                    AccountTransaction.Type.REMOVAL, AccountTransaction.Type.INTEREST,
                    AccountTransaction.Type.INTEREST_CHARGE, AccountTransaction.Type.FEES,
                    AccountTransaction.Type.FEES_REFUND, AccountTransaction.Type.TAXES,
                    AccountTransaction.Type.TAX_REFUND);

    private CashFlows()
    {
    }

    private record Pending(Account account, AccountTransaction tx)
    {
    }

    private record Doomed(Account account, AccountTransaction tx)
    {
    }

    static Map<String, Object> apply(ClientHolder holder, JsonObject body) throws IOException
    {
        holder.acquire();
        try
        {
            Client client = holder.client();
            List<Pending> creates = new ArrayList<>();
            JsonArray create = body.has("create") && body.get("create").isJsonArray()
                            ? body.getAsJsonArray("create")
                            : new JsonArray();
            int i = 0;
            for (JsonElement e : create)
            {
                if (!e.isJsonObject())
                    throw ApiException.badRequest("create[" + i + "]: object expected");
                creates.add(build(client, e.getAsJsonObject(), "create[" + i + "]"));
                i++;
            }
            List<Doomed> deletes = new ArrayList<>();
            JsonArray delete = body.has("delete") && body.get("delete").isJsonArray()
                            ? body.getAsJsonArray("delete")
                            : new JsonArray();
            for (JsonElement e : delete)
                deletes.add(find(client, e.getAsString()));

            for (Doomed d : deletes)
                d.account().deleteTransaction(d.tx(), client);
            List<Object> created = new ArrayList<>();
            for (Pending p : creates)
            {
                p.account().addTransaction(p.tx());
                created.add(Json.obj().with("uuid", p.tx().getUUID()).with("source", p.tx().getSource()));
            }
            if (!creates.isEmpty() || !deletes.isEmpty())
            {
                client.markDirty();
                holder.save();
            }
            return Json.obj().with("created", created).with("deleted", deletes.size())
                            .with("revision", holder.revision());
        }
        finally
        {
            holder.lock.unlock();
        }
    }

    private static Pending build(Client client, JsonObject o, String where)
    {
        String uuid = Json.str(o, "account");
        Account account = client.getAccounts().stream().filter(a -> a.getUUID().equals(uuid)).findFirst()
                        .orElseThrow(() -> ApiException.badRequest(where + ": unknown account"));
        AccountTransaction.Type type;
        try
        {
            type = AccountTransaction.Type.valueOf(String.valueOf(Json.str(o, "type")));
        }
        catch (IllegalArgumentException e)
        {
            type = null;
        }
        if (type == null || !TYPES.contains(type))
            throw ApiException.badRequest(where + ": type not allowed");
        LocalDate day;
        try
        {
            day = LocalDate.parse(Json.str(o, "date"));
        }
        catch (RuntimeException e)
        {
            throw ApiException.badRequest(where + ": date: expected YYYY-MM-DD");
        }
        BigDecimal value;
        try
        {
            value = new BigDecimal(String.valueOf(Json.str(o, "amount")).trim());
        }
        catch (NumberFormatException e)
        {
            throw ApiException.badRequest(where + ": amount: not a number");
        }
        if (value.signum() <= 0)
            throw ApiException.badRequest(where + ": amount: must be positive");
        long amount = value.multiply(BigDecimal.valueOf(Values.Amount.factor())).setScale(0, RoundingMode.HALF_UP)
                        .longValueExact();

        AccountTransaction t = new AccountTransaction();
        t.setType(type);
        t.setDateTime(day.atTime(LocalTime.MIDNIGHT));
        t.setCurrencyCode(account.getCurrencyCode());
        t.setAmount(amount);
        String note = Json.str(o, "note");
        if (note != null && !note.isBlank())
            t.setNote(note.trim());
        String source = Json.str(o, "source");
        if (source != null && !source.isBlank())
            t.setSource(source.trim());
        return new Pending(account, t);
    }

    private static Doomed find(Client client, String uuid)
    {
        for (Account a : client.getAccounts())
            for (AccountTransaction t : a.getTransactions())
                if (t.getUUID().equals(uuid))
                {
                    if (!TYPES.contains(t.getType()) || t.getCrossEntry() != null)
                        throw ApiException.conflict("not_a_cash_flow",
                                        "transaction " + uuid + " is no deposit, removal, interest, fee or tax");
                    return new Doomed(a, t);
                }
        throw ApiException.notFound("unknown transaction " + uuid);
    }
}
