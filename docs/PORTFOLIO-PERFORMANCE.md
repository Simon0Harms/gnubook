# Portfolio Performance in gnubook

gnubook can run [Portfolio Performance](https://www.portfolio-performance.info/) (PP) on the server and book
its securities transactions and prices into the GnuCash book. PP stays the place where securities are managed:
PDF statements of the bank are read by PP's own importers, prices come from PP's price sources, and the
performance figures (TTWROR, IRR, FIFO cost) are calculated by PP itself. gnubook shows all of this in its web
interface and keeps the GnuCash book in step with the PP file.

PP's desktop user interface is not used. PP's core runs without a user interface as a small local service,
**pp-core**, next to gnubook in the same container.

```
Browser ──► gnubook (port 8080) ──HTTP + token──► pp-core (127.0.0.1:8091) ──► PP file of each book
                │                                     │                      (data/ppcore/clients/book-<id>/)
                │                                     └──► prices: Yahoo and PP's other sources, ECB rates
                └──► GnuCash book (PostgreSQL/SQLite): bookings, securities, prices
```

## What you get

A menu entry *Portfolio Performance* with these pages:

| Page | Content |
|---|---|
| *Kennzahlen* | TTWROR (cumulative and per year), IRR, absolute change, max. drawdown, volatility for a period (this year, 12 months, 3/5 years, all, custom) and for all or one securities account; charts of the performance and of the value against the invested capital; PP's calculation table (initial value, capital gains, earnings, fees, taxes, final value) |
| *Vermögensaufstellung* | holdings on any date: shares, price, value, FIFO cost, unrealised and realised gains, dividends, IRR and TTWROR per security; cash accounts |
| *Buchungen* | all PP transactions with their state in the GnuCash book (booked, edited in GnuCash, conflict, deleted in GnuCash, detached, not booked and why); conflicts are resolved here; transactions can be deleted in PP |
| *PDF-Import* | upload bank statements (contract notes, dividends, taxes) as PDF; PP's importers read them; possible duplicates and warnings are shown and only imported when you allow it |
| *Wertpapiere und Kurse* | securities with their price source, last price and number of prices; search for a price source (Yahoo by ISIN), update prices |
| *Einstellungen* | upload, download or create the PP file; the GnuCash accounts used; the bank-import rule; start date and options; manual run and dry run; history of the runs |

Demo users never see these pages.

## Installation

pp-core is optional. Install gnubook first (see [INSTALL.md](INSTALL.md)), then, as root in the gnubook
container:

```bash
bash /opt/gnubook/src/deploy/install-pp.sh
```

The script

- installs `openjdk-21-jdk-headless` (and `curl`, `gpg`) unless a JDK 21 or newer is already there;
- downloads the Portfolio Performance release for Linux this gnubook version was tested with (0.88.0) from
  PP's GitHub releases and checks its signature against the release key of PP's author
  (fingerprint `E46E 6F8F F02E 4C83 5690 8458 9239 277F 560C 95AC`, fetched from keys.openpgp.org);
- compiles pp-core against exactly this PP release and writes its OSGi configuration (below `/opt/gnubook/pp`);
- writes `/opt/gnubook/ppcore.properties` with a random token, and `[pp] url` and `token` into
  `/opt/gnubook/config.toml`;
- installs and starts the services `gnubook-ppcore` (pp-core) and `gnubook-pp.timer` (hourly price update and
  booking), and restarts gnubook.

Options: `install-pp.sh latest` installs the newest PP release instead, `install-pp.sh 0.88.0` a specific one.
Without access to keys.openpgp.org, give PP's public key as file with `GNUBOOK_PP_KEYFILE=/path/key.asc`. The
installer can also be run together with gnubook's: `GNUBOOK_PP=1` for `install.sh`.

If the distribution has no `openjdk-21-jdk-headless` package (Debian 12's default Java is OpenJDK 17), install
a JDK 21 yourself (for example Eclipse Temurin 21) and run the script with `PPCORE_JAVA_HOME=/path/to/jdk`.

**Resources.** The PP release takes about 80 MB on disk. pp-core is a Java process: in tests with small PP files
it used about 230–330 MB of memory (resident), gnubook itself about 120 MB. The Java heap is limited to 768 MB
(`PPCORE_JAVA_OPTS` in `/opt/gnubook/ppcore.env`), so pp-core can grow to roughly 1 GB with large files. Plan for
1–1.5 GB of memory for the container; with less, lower the heap (e.g. `PPCORE_JAVA_OPTS=-Xmx512m`) and restart
`gnubook-ppcore`.

## First steps

1. *Portfolio Performance → Einstellungen*: upload your PP file (`.xml`, `.portfolio` or `.zip`, not
   password-protected), or create a new, empty one. Every book has its own PP file.
2. Check the accounts under *Konten im Buch*. Empty fields mean the default names below; missing accounts
   are created on the first run:

   | Role | Default (German chart) | Type |
   |---|---|---|
   | securities root (one placeholder per PP securities account below it) | `Aktiva:Wertpapiere` | Asset |
   | clearing account | `Aktiva:Wertpapier-Verrechnung` | Asset |
   | dividends | `Erträge:Dividenden` | Income |
   | interest | `Erträge:Zinsen` | Income |
   | realised gains | `Erträge:Kursgewinne` | Income |
   | fees | `Aufwendungen:Wertpapiergebühren` | Expense |
   | taxes | `Aufwendungen:Kapitalertragsteuer` | Expense |
   | interest charges | `Aufwendungen:Sollzinsen` | Expense |
   | deliveries and opening positions | `Eigenkapital:Wertpapier-Einlieferungen` (or a top-level equity account like `Anfangsbestand`) | Equity |

   A book with an English chart gets English names (`Assets:Investments`, `Income:Dividends`, …).
3. Under *Verrechnungskonten der Depots im Buch*, choose the bank accounts whose bank-import lines belong to
   the securities business (see the next section).
4. Optionally set a start date (*Buchungen ab*, see below).
5. Run a *Probelauf* (dry run): it shows how many bookings would be created, changed and deleted.
6. Switch on *Buchungen und Kurse aus Portfolio Performance automatisch ins Buch übernehmen* and save. The
   first run starts in the background; *Jetzt übernehmen* runs it at once, *Letzte Läufe* lists the results.

## The clearing account and the bank import

PP records the cash side of every securities transaction in a PP cash account ("Verrechnungskonto"). The real
bank account behind it is usually imported into GnuCash by the FinTS bank import as well. Booking both would
count every purchase twice. gnubook therefore uses a **clearing account** (`Aktiva:Wertpapier-Verrechnung`):

- The PP side books the cash part of buys, sales, dividends, interest, fees and taxes against the clearing
  account.
- The bank import books the lines of the bank accounts chosen under *Verrechnungskonten der Depots im Buch*
  against the same clearing account, instead of finding a counter account as usual. Transfers from and to
  your own other accounts are still booked as transfers.

Example: a purchase of 10 shares for 1,001.50 € including 1.50 € fees.

| | Bank account | Clearing account | Securities | Fees |
|---|---:|---:|---:|---:|
| bank import: debit of the bank | −1,001.50 | +1,001.50 | | |
| PP: purchase | | −1,001.50 | +1,000.00 (10 shares) | +1.50 |
| **balance** | −1,001.50 | **0.00** | 1,000.00 | 1.50 |

When both sides have arrived, the clearing account is back to zero. A balance other than zero shows a
transaction that exists only on one side, for example a statement that was not imported into PP yet. The
*Kennzahlen* page shows the current balance.

PP's deposits, withdrawals and transfers between PP cash accounts are **not** booked (the bank import has
them). Instead of the clearing account, a PP cash account can also be mapped to a GnuCash account directly
(*Geldseite je PP-Konto*), for example when there is no bank import for it.

## How the transactions are booked

| PP transaction | GnuCash splits |
|---|---|
| buy | securities account: gross value (shares); clearing: −amount paid; fees, taxes: their accounts |
| sale | securities account: −gross proceeds (−shares); clearing: +amount received; fees, taxes; realised gain: a split without shares on the securities account and the gain on the income account |
| dividend | clearing: net amount; dividends: −gross; taxes, fees: their accounts; a zero split on the securities account links the dividend to the security |
| interest, interest charge, fees, taxes, refunds | clearing account against the matching income or expense account |
| transfer between securities accounts | from one securities account to the other at FIFO cost |
| inbound / outbound delivery | securities account against the deliveries account (at FIFO cost when outbound) |
| deposit, withdrawal, transfer between cash accounts | not booked (bank import) |

Details:

- Every PP securities account becomes a placeholder below the securities root, and every security in it an
  account of type *Aktie* or *Fonds* (ETFs and funds are recognised by their name) with the security as
  commodity. The commodity is created in the namespace `Portfolio Performance` (setting *Typ (Namensraum)
  neuer Wertpapiere*), with
  the ticker (else WKN, ISIN) as symbol and the ISIN as code; an existing commodity with the same ISIN is
  used instead.
- Realised gains are calculated **FIFO per securities account** on the values before fees, like PP. Purchase
  dates are kept across transfers. Optionally (*Kaufgebühren in den Einstand*) purchase fees become part of
  the cost and sale fees reduce the proceeds. Without *Realisierte Kursgewinne buchen* the sale only reduces
  the securities account by the proceeds.
- The description is German (`Kauf …`, `Verkauf …`, `Dividende …`, `Gebühren …`, `Depotübertrag …`,
  `Einlieferung …`, `Anfangsbestand …`); the notes contain PP's note, the source document (`Beleg: …`) and
  `Portfolio Performance`.
- Transactions in a currency other than the book's currency (for example of a USD cash account) are not
  booked; they are listed with the reason. Securities quoted in foreign currencies are fine when they are
  bought from an account in the book's currency.

## Start date

Without a start date all PP transactions are booked. With a start date (for example the first day of the year
from which the GnuCash book should contain securities), earlier transactions are not booked. Instead, the
holdings of the day before become one **opening booking** per security, at FIFO cost, against the deliveries
account. Later sales calculate their gains from the real purchase prices before the start date.

## Prices

The prices of every booked security are written into GnuCash's price database (source `user:price`, in the
book's currency): daily for the last 400 days (*Tageskurse der letzten … Tage*), one per month before, from a
week before the first booking on. Prices that were entered in GnuCash itself are left alone. The option *Kurse
in die GnuCash-Preisdatenbank übernehmen* switches this off.

Prices are updated in PP by pp-core: on the page *Wertpapiere und Kurse*, or every
`[pp] quotes_interval_hours` (default 12) by the hourly timer. The ECB exchange rates PP uses for securities in
other currencies are updated every 6 hours.

The price source of a security is chosen on its page. *Kursquelle suchen* asks Yahoo Finance and PP's other
search services (by name, ISIN, WKN or symbol). For securities that a PDF import creates, pp-core looks for a
Yahoo symbol by ISIN itself (option *Kursquelle für neue Wertpapiere suchen* on the import page). PP's own price service (*Portfolio Performance* as source) needs a login in
PP's desktop application and does not work in pp-core.

## Keeping the book in step

- Every booking has a stable key (PP's transaction UUID). gnubook remembers which GnuCash transaction belongs
  to it and what it booked. The transaction in GnuCash also carries the key (slot `gnubook-pp`), so the link
  is found again even if gnubook's own data is lost.
- After every change in PP made through gnubook (upload, PDF import, deletion, prices), gnubook books the
  changes in the background. The hourly timer (`gnubook pp-update`) does the same.
- A transaction changed in PP is changed in GnuCash, a transaction deleted in PP is deleted in GnuCash – but
  only as long as nobody changed it in GnuCash. A changed **description, date, amount or account** in GnuCash
  marks the booking as *in GnuCash bearbeitet*; it stays as it is until PP changes the same transaction, which
  is then a **conflict**. Notes and reconciliation are no change. Reconciled bookings are never changed by
  gnubook.
- Conflicts are resolved on the page *Buchungen*: *PP übernehmen* overwrites the GnuCash transaction with PP's
  version, *Buch behalten* keeps the GnuCash version and detaches the booking from PP, *wieder abgleichen*
  links it again.
- A booking deleted in GnuCash is not created again (unless you choose *PP übernehmen*).
- gnubook writes only while GnuCash Desktop has the book closed. While it is open, the run waits and is
  repeated every 5 minutes and with the next timer run.
- Only one run per book takes place at a time, also across the web application, the timer and the command
  line.

Command line:

| Command | Purpose |
|---|---|
| `gnubook pp-status` | pp-core reachable? PP file, last price update and last run of every book |
| `gnubook pp-sync [--book B] [--dry-run] [--force]` | book the changes now (`--dry-run`: only show what would change; `--force`: run even if nothing changed in PP) |
| `gnubook pp-update [--book B] [--quotes/--no-quotes]` | update prices when due, then book changes (what the timer runs) |

## Editing the PP file in PP's desktop application

The PP file can be downloaded under *Einstellungen*, edited in PP's desktop application and uploaded again.
The upload replaces the file (the previous one is kept as backup in pp-core's data directory), and the next run
books the differences. Changes made in gnubook in the meantime (PDF imports, prices) are lost with the upload,
so do not work on both at the same time.

## Updates

- `gnubook-update` (gnubook itself) rebuilds pp-core when its code changed.
- `gnubook-pp-update` installs the newest PP release, `gnubook-pp-update 0.88.0` a specific one.

A new build is compiled against the PP release first: if PP changed something pp-core depends on, the build
fails and the running version stays. A build that compiles but does not answer after starting is not kept:
the previous build is started again. The last builds are kept in `/opt/gnubook/pp/builds`.

## Files and services

| Path | Content |
|---|---|
| `/opt/gnubook/data/ppcore/clients/book-<id>/` | the PP file of each book, earlier versions (`backups/`, one per save, the last 20) and pp-core's state |
| `/opt/gnubook/data/ppcore/workspace` | PP's workspace (ECB exchange rates, log) |
| `/opt/gnubook/ppcore.properties` | pp-core settings: `bind`, `port`, `token`, `data_dir`, `keep_backups`, `max_upload_mb` (mode 640) |
| `/opt/gnubook/ppcore.env` | Java binary and JVM options (`PPCORE_JAVA_OPTS`, e.g. `-Xmx512m`) |
| `/opt/gnubook/pp/dist/<version>/` | the PP release as downloaded |
| `/opt/gnubook/pp/builds/`, `current` | pp-core builds; `current` is the active one |
| `gnubook-ppcore.service` | pp-core (`journalctl -u gnubook-ppcore`) |
| `gnubook-pp.timer`, `gnubook-pp.service` | hourly `gnubook pp-update` |

`config.toml`:

```toml
[pp]
url = "http://127.0.0.1:8091"
token = "…"                  # the same as in ppcore.properties
timeout = 60                 # seconds for calls to pp-core (PDF import and prices wait longer)
quotes_interval_hours = 12   # pp-update loads prices at most this often
```

The PP files are part of `data/` and therefore of the backups made by `gnubook-update`. pp-core listens on
`127.0.0.1` only and needs the token for every call.

## Limitations

- PP's desktop user interface is not available; what gnubook's pages do not cover (taxonomies, investment
  plans, watchlists, dashboards, editing single transactions) needs PP's desktop application with the
  downloaded file (see above). As far as I know, PP creates the transactions of investment plans in its
  desktop application, so pp-core does not execute investment plans; import the statements instead.
- Password-protected PP files are not supported.
- Cash accounts in other currencies than the book's are not booked.
- PP's own price service needs a login and does not work in pp-core.
- One PP file per book.

## Troubleshooting

- *pp-core nicht erreichbar*: `systemctl status gnubook-ppcore`, `journalctl -u gnubook-ppcore -n 100`.
  `[pp] token` in `config.toml` must be the same as `token` in `ppcore.properties`.
- Prices are not updated: the page *Wertpapiere und Kurse* shows the error per security. The container needs
  internet access to the price sources (e.g. `query1.finance.yahoo.com`).
- The clearing account does not return to zero: look for transactions that exist only in PP or only in the
  bank import (wrong bank account chosen, statement not imported into PP).
- A run fails with an account error: the accounts in *Einstellungen* must not be placeholders and must be in
  the book's currency.

## License

pp-core links Portfolio Performance's bundles, which are licensed under the Eclipse Public License 1.0. The
pp-core source code in `ppcore/` is part of gnubook (GPL-3.0-or-later) with an additional permission to
combine it with Portfolio Performance, see [ppcore/README.md](../ppcore/README.md). Portfolio Performance itself
is downloaded from its official releases and not distributed with gnubook.
