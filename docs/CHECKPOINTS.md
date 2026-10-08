# Balance checkpoints

Many banks put the statement balance into the booking text of the monthly or quarterly closing line. gnubook
reads these balances and compares them with the book after every change.

## Recognised formats

| Text in the booking | Meaning |
|---|---|
| `STAND29.05.2026 1.239,51H` (also `Stand 29.05.2026 1.239,51 H`) | balance at the end of 29.05.2026, `H` = credit (positive), `S` = debit (negative) |
| `**ENDSALDO** 1.234,56H` | balance after the closing line itself (fees included) |
| `Kontostand am 31.03.2026 47,11 +` (also `-47,11` or `47,11 -`) | balance at the end of the day |

Words that only contain these letters are ignored, for example `WIDERSTANDSFÄHIG` or `STANDARDLASTSCHRIFT`.
Invalid dates such as `31.02.` are ignored too. A checkpoint belongs to the bank, asset or liability account
the closing booking is posted to.

## How the book side is computed

- **STAND**: all bookings of the account up to and including the given day, without the closing booking
  itself.
- **ENDSALDO**: STAND plus the amount of the closing booking on the account, for example the fees.
- **Difference** = book − bank.

The column *neu seit davor* shows how much of the difference appeared since the previous checkpoint. It tells
you in which period to look for the missing or wrong booking.

A typical cause of differences are transfers between two banks around a month end: the banks book them on
different days, but a GnuCash transaction has only one date. See the transit account option in
[FINTS.md](FINTS.md).

## Accepting known differences

Some old differences cannot be fixed any more. Accept them on the *Saldo-Prüfpunkte* page (with a note) or
all at once:

```bash
gnubook check-balances --accept-open --note "Altlasten vor 2025"
```

gnubook stores the difference it accepted. If it changes later, for example because a booking in that month
was edited, the checkpoint is reported as open again.

## Checking from cron

`gnubook check-balances` prints all deviations and ends with exit code 1 if there are open ones:

```
Aktiva:Barvermögen:Girokonto Musterbank: 14 Saldo-Angaben geprüft, 13 stimmen, 1 abweichend, 0 akzeptiert
  offen: 2026-04-30 (Abschluss 2026-05-01)  Differenz STAND 23,40, ENDSALDO 23,40
```

A cron job that sends mail only when something is open looks like this (`/etc/cron.d/gnubook-balances`; cron
needs a working mail setup):

```
MAILTO=you@example.org
30 7 * * * gnubook out=$(/opt/gnubook/venv/bin/gnubook --config /opt/gnubook/config.toml check-balances) || echo "$out"
```

## Bank profiles and own formats

Which lines count as balance lines depends on the book's **bank profile** (*Bücher → Bankprofil und Import*):

| Profile | Balance lines | Import booking text | Own accounts by IBAN |
|---|---|---|---|
| `de` (default) | `STAND…`, `**ENDSALDO**`, `Kontostand am …` | `<purpose>; <name>`, memo `Konto <IBAN>` | also via the bank code and account number GnuCash stored for online banking |
| `generic` | only the patterns configured below | `<name> – <purpose>`, memo `IBAN <IBAN>` | `iban_map` and account codes only |

Add more formats for every profile in `config.toml`. A pattern is a regular expression with the named groups
`day`, `month`, `year` and `amount`, and optionally `sign`. The `sign` group may be `-` or the debit marker.
`end` is an optional second expression for the balance after the closing line. `keyword` is a lower-case word
every such line contains.

```toml
[[checkpoints.patterns]]
stand = 'Balance on (?P<day>\d{2})/(?P<month>\d{2})/(?P<year>\d{4}):? (?P<sign>-?)(?P<amount>[\d,]+\.\d{2})'
keyword = "balance"
decimal = "."
thousands = ","
```

Profiles for other countries go into `gnubook/banks/` as a subclass of `BankProfile`. `de.py` is the example.

