package gnubook.ppcore;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import name.abuchen.portfolio.datatransfer.ImportAction.Status;
import name.abuchen.portfolio.model.BuySellEntry;
import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.model.Portfolio;
import name.abuchen.portfolio.model.PortfolioTransaction;

/**
 * Duplicate check for securities account transactions that carry a document number in their note, instead of PP's
 * DetectDuplicatesAction (see https://github.com/Simon0Harms/gnubook/issues/2).
 *
 * PP treats two transactions as the same when day, type, security, shares and amount match. With several identical
 * trades a day (e.g. grid trading) one existing transaction then flags every identical new item. Here:
 * <ul>
 * <li>an existing transaction (or an earlier item of the same import) with the same number: duplicate,</li>
 * <li>existing transactions with another number of the same kind: never a duplicate,</li>
 * <li>existing identical transactions without a number (or with a number of another kind): matched 1:1 – of n
 * identical new items only as many are duplicates as there are such transactions.</li>
 * </ul>
 * Number kinds: "Vorgangs-Nr.: …" (PDF documents of Baader Bank / finanzen invest) and "Beleg …" (finanzen invest
 * Excel list, see gnubook/pp/csvimport.py) are the same number and compared with each other. A future number kind
 * that is not known to match gets a kind of its own: pairs of different kinds fall back to the 1:1 matching, which
 * never books a trade twice.
 *
 * Items whose subject is not a securities account transaction, or that carry no number, keep PP's check. If PP gets
 * a 1:1 matching of its own, the counting part here can go; the number part stays more precise.
 */
final class DocumentDuplicates
{
    private static final Pattern NUMBER = Pattern
                    .compile("(?:(Vorgangs-Nr)\\.?\\s*:?|\\b(Beleg))\\s+([0-9]{6,})"); //$NON-NLS-1$

    record Number(String kind, String value)
    {
    }

    private record Key(LocalDate day, String security, String direction, long shares, long amount, String currency)
    {
    }

    private DocumentDuplicates()
    {
    }

    /** The document number in a note, or null. */
    static Number number(String note)
    {
        if (note == null)
            return null;
        Matcher m = NUMBER.matcher(note);
        if (!m.find())
            return null;
        // "Vorgangs-Nr." of the PDF and "Beleg" (BELEG-D) of the Excel list are the same number (checked with a
        // settlement of 13.10.2026 and the purchase it refers to), so both are one kind
        return new Number(m.group(1) != null || m.group(2) != null ? "baader" : "other", m.group(3)); //$NON-NLS-1$ //$NON-NLS-2$
    }

    /** The securities account transaction of an item (buy/sell or delivery), or null. */
    static PortfolioTransaction transaction(Object subject)
    {
        if (subject instanceof BuySellEntry entry)
            return entry.getPortfolioTransaction();
        if (subject instanceof PortfolioTransaction t)
            return t;
        return null;
    }

    private static String direction(PortfolioTransaction.Type type)
    {
        return switch (type)
        {
            case BUY, DELIVERY_INBOUND -> "in"; //$NON-NLS-1$
            case SELL, DELIVERY_OUTBOUND -> "out"; //$NON-NLS-1$
            default -> null;
        };
    }

    private static Key key(PortfolioTransaction t)
    {
        String direction = t.getType() == null ? null : direction(t.getType());
        if (direction == null || t.getSecurity() == null || t.getDateTime() == null)
            return null;
        return new Key(t.getDateTime().toLocalDate(), t.getSecurity().getUUID(), direction, t.getShares(),
                        t.getAmount(), t.getCurrencyCode());
    }

    /**
     * Statuses for the given new items (index → status, null = no duplicate). Items not handled here (no number, not
     * a securities account transaction) are left out of the result: they need PP's check.
     */
    static Map<Integer, Status> check(Client client, List<Map.Entry<Integer, PortfolioTransaction>> items)
    {
        Map<Key, List<Number>> existing = new HashMap<>();
        Set<Number> known = new HashSet<>();
        for (Portfolio p : client.getPortfolios())
            for (PortfolioTransaction t : p.getTransactions())
            {
                Key k = key(t);
                if (k == null)
                    continue;
                Number n = number(t.getNote());
                existing.computeIfAbsent(k, x -> new ArrayList<>()).add(n);
                if (n != null)
                    known.add(n);
            }

        Map<Integer, Status> result = new HashMap<>();
        Map<Key, Integer> used = new HashMap<>(); // existing transactions without a comparable number already matched
        Set<Number> seen = new HashSet<>();
        for (Map.Entry<Integer, PortfolioTransaction> item : items)
        {
            PortfolioTransaction t = item.getValue();
            Number n = number(t.getNote());
            Key k = key(t);
            if (n == null || k == null)
                continue;
            if (known.contains(n))
            {
                result.put(item.getKey(), new Status(Status.Code.WARNING,
                                "Buchung existiert schon (Beleg-/Vorgangs-Nr. " + n.value() + ")")); //$NON-NLS-1$ //$NON-NLS-2$
                continue;
            }
            if (!seen.add(n))
            {
                result.put(item.getKey(), new Status(Status.Code.WARNING,
                                "Beleg-/Vorgangs-Nr. " + n.value() + " ist zweimal in diesem Import")); //$NON-NLS-1$ //$NON-NLS-2$
                continue;
            }
            long free = existing.getOrDefault(k, List.of()).stream()
                            .filter(other -> other == null || !other.kind().equals(n.kind())).count();
            int taken = used.getOrDefault(k, 0);
            if (taken < free)
            {
                used.put(k, taken + 1);
                result.put(item.getKey(), new Status(Status.Code.WARNING,
                                "Buchung existiert möglicherweise schon (gleiche Buchung ohne Belegnummer)")); //$NON-NLS-1$
            }
            else
            {
                result.put(item.getKey(), null);
            }
        }
        return result;
    }
}
