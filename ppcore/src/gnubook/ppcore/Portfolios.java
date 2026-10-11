package gnubook.ppcore;

import java.io.IOException;
import java.util.Currency;
import java.util.Map;

import com.google.gson.JsonObject;

import name.abuchen.portfolio.model.Account;
import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.model.Portfolio;

/**
 * A new securities account (Depot) in an existing file, with its cash account (Verrechnungskonto) – what PP
 * Desktop's "Neues Depot" / "Neues Konto" do.
 *
 * <p>Body of {@code POST /clients/{id}/portfolios}: {@code name} (the Depot), and either {@code account} (UUID of
 * an existing cash account as reference account) or {@code accountName} with optional {@code currency} (default:
 * the file's base currency) for a new cash account. Names must not be taken by an active portfolio or account.
 */
final class Portfolios
{
    private Portfolios()
    {
    }

    static Map<String, Object> create(ClientHolder holder, JsonObject body) throws IOException
    {
        holder.acquire();
        try
        {
            Client client = holder.client();
            String name = trimmed(Json.str(body, "name"));
            if (name == null)
                throw ApiException.badRequest("name: required");
            if (client.getPortfolios().stream().anyMatch(p -> !p.isRetired() && p.getName().equalsIgnoreCase(name)))
                throw ApiException.conflict("exists", "there is a portfolio " + name + " already");

            String accountUuid = trimmed(Json.str(body, "account"));
            Account account;
            boolean created = false;
            if (accountUuid != null)
            {
                account = client.getAccounts().stream().filter(a -> a.getUUID().equals(accountUuid)).findFirst()
                                .orElseThrow(() -> ApiException.badRequest("unknown account"));
            }
            else
            {
                String accountName = trimmed(Json.str(body, "accountName"));
                if (accountName == null)
                    throw ApiException.badRequest("account or accountName required");
                if (client.getAccounts().stream()
                                .anyMatch(a -> !a.isRetired() && a.getName().equalsIgnoreCase(accountName)))
                    throw ApiException.conflict("exists", "there is an account " + accountName + " already");
                String currency = trimmed(Json.str(body, "currency"));
                currency = currency == null ? client.getBaseCurrency() : currency.toUpperCase();
                try
                {
                    Currency.getInstance(currency);
                }
                catch (IllegalArgumentException e)
                {
                    throw ApiException.badRequest("currency: unknown " + currency);
                }
                account = new Account();
                account.setName(accountName);
                account.setCurrencyCode(currency);
                created = true;
            }

            Portfolio portfolio = new Portfolio();
            portfolio.setName(name);
            portfolio.setReferenceAccount(account);
            if (created)
                client.addAccount(account);
            client.addPortfolio(portfolio);
            client.markDirty();
            holder.save();
            return Json.obj().with("portfolio", portfolio.getUUID()).with("name", name)
                            .with("account", account.getUUID()).with("accountName", account.getName())
                            .with("currency", account.getCurrencyCode()).with("accountCreated", created)
                            .with("revision", holder.revision());
        }
        finally
        {
            holder.lock.unlock();
        }
    }

    private static String trimmed(String v)
    {
        return v == null || v.isBlank() ? null : v.trim();
    }
}
