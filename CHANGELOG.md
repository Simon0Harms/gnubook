# Changelog
## Unreleased
- Belegeingang: after a complete run *Belege ab* moves up to the day the next run lists back to
- Belegeingang: moving *Belege ab* back or emptying it lists the postbox again only when documents can be missing
  before the old day (it was after the oldest document fetched so far)
- gnubook-update: the backup before an update leaves out the PDFs of the Belegeingang (`data/postbox/book-*`)
- Belegeingang: the local PDF is deleted once it is in Nextcloud and nothing is left to do for PP (on by default)
- Portfolio Performance: the PDF import also reads lists of securities account transactions – CSV with the
  columns of PP's CSV import, or the Excel list finanzen.net zero sends on request – with PP's CSV importer and the
  same checks as PDFs (possible duplicates are not imported without a decision). pp-core: `"csv"` in
  `POST /clients/{id}/import`
- Belegeingang: logs in again when the session runs out during a long run (downloads failed with HTTP 401)
- Belegeingang: the oldest documents are downloaded first
- Belegeingang: progress of a running run on the page (reloading itself) and in the journal
- Belegeingang: *Abbrechen* stops a running run from the web interface
- Belegeingang: no click limit by default (`[postbox] max_clicks = 0`), runs may take up to 12 hours; after a click
  only the new entries are read
- Belegeingang: *Belege ab* may stay empty – then every document down to the oldest one is fetched
- Belegeingang: an own Nextcloud share link for the documents (instead of the Nextcloud of the GnuCash copy),
  and sub folders per year, week or day
- Belegeingang (optional, `deploy/install-postbox.sh`, docs/POSTBOX.md): documents of a finanzen.net zero account are
  fetched from its online postbox by a headless browser (Playwright, Firefox or Chromium; no X server or VNC). It logs
  in with the stored user name and password (encrypted like the Nextcloud passwords; generic recognition of the login
  form, cookie banner, two-step login; optional CSS selectors), clicks *Mehr anzeigen* until a whole block of the list
  is older than the newest completely listed day minus an overlap, and downloads every document with an unknown
  number inside the page. Every PDF is kept in `data/postbox/`, imported into Portfolio Performance with PP's
  importers (OK items at once; possible duplicates and a missing securities account wait for a decision on the PDF
  import page, which the documents page links to) and copied into a Nextcloud folder of the user (optionally one
  folder per year). Runs: `gnubook-postbox.timer` (Mon–Fri 23:20) and *Jetzt abrufen*/*Probelauf* on the new page
  *Belegeingang* (a request file picked up by `gnubook-postbox-request.path` – the web app never starts a browser);
  `gnubook postbox-fetch`, `postbox-status`. Failed runs keep a screenshot and the page's HTML (password removed); a
  run that does not reach the stop day, and a failed download, keep the list position so the next run lists that
  range again. After a failed login the timer pauses until the settings are saved again. A cookie banner is only
  answered before the login and inside a consent container; dialogs of the app are never clicked.
  Browser tests against a stand-in of the web app (CI with Firefox and Chromium)
- Nextcloud: `Client.upload_bytes` can skip creating the folder and set the content type
- Fixed (pp-core): `GET /clients/{id}/import/{session}` imported the remaining OK items and failed with 409
  `target_missing` while a bank's securities account was still to be chosen – so the PDF import page showed an
  error instead of the choice. The GET now only checks and shows the session; importing is `…/apply` alone. The smoke
  test covers choosing the securities account and the duplicate warning on a second import

- Portfolio Performance: *Einstellungen* can add a securities account (*Depot hinzufügen*) to the existing PP
  file, with a new cash account (name, currency) or an existing one as reference account – no detour through
  PP Desktop. The Depotwechsel form links to it. pp-core: `POST /clients/{id}/portfolios`
- Portfolio Performance: *Ein-/Auslieferung* can also enter a *Depotwechsel* (securities transfer between two
  securities accounts of the PP file, like PP Desktop's dialog): existing security, sending and receiving
  account in the same currency, shares, optional value (shares × price of that day), note; no fees or taxes;
  more shares than held only after confirmation. Booked in GnuCash as before for PP transfers: one *Depotübertrag*
  at FIFO cost, purchase dates kept. pp-core: `POST /clients/{id}/transactions` with `"type": "TRANSFER"` and
  `"target"`
- *Bücher*: new card *PostgreSQL-Rollen* lists the login roles gnubook's admin role may manage, with their
  databases and the books using them. Unused roles can be deleted (type the name to confirm), optionally together
  with the databases they own (no backup). Roles of connected books, superusers and roles without ADMIN
  privilege (PG16+) are not offered
- Portfolio Performance: *Wertpapiere und Kurse* can be filtered by portfolio (Depot); shows the securities
  with transactions in that portfolio. The filter is kept when toggling *auch aufgelöste …*
- Portfolio Performance: new page *Ein-/Auslieferung* to enter inbound and outbound deliveries by hand, for
  documents PP's PDF importers do not read (e.g. Baader Bank / Scalable Capital *Depotauslieferung*). Existing
  security or a new one by ISIN, value optional (shares × price of that day), fees, taxes, note;
  more shares out than held only after confirmation. pp-core: `POST /clients/{id}/transactions`. The PDF
  import links to the page when a document is not recognised
- Fixed (Portfolio Performance): when the GnuCash database is replaced by another book (e.g. GnuCash Desktop
  *Save As* over it), the next run no longer marks all PP bookings as *deleted in GnuCash*. gnubook remembers the
  book's GUID; on a different one it forgets the old links (bookings, securities, accounts, prices), finds its
  transactions again by their slot or books them anew, and runs at once instead of waiting for the next day
- PDF import (Portfolio Performance): several documents can be uploaded packed as ZIP or TAR (.tar.gz/.tgz,
  .tar.bz2, .tar.xz); the PDF and TXT files inside are imported, other files ignored. Limits against archive
  bombs (500 documents, 200 MB unpacked per upload); encrypted ZIPs and nested archives are not unpacked
- Portfolio Performance (optional, `deploy/install-pp.sh`, docs/PORTFOLIO-PERFORMANCE.md): PP runs on the server
  without its desktop interface as service *pp-core* (PP's own bundles started by Equinox with a small extra
  bundle and JSON API, one PP file per book). New pages *Portfolio Performance*: performance figures and charts
  calculated by PP (TTWROR, IRR, drawdown, volatility), holdings on any date, all transactions with their state
  in the book, PDF import with PP's importers, securities and price sources, settings. PP transactions are booked
  into the GnuCash book and kept in step (stock/fund accounts and commodities, FIFO realised gains, dividends,
  fees, taxes, transfers, deliveries, opening positions from a start date, prices into the price database);
  bookings changed in GnuCash are not overwritten, conflicts are resolved in the web interface. The cash side
  goes to a clearing account; the bank import books the lines of the chosen bank accounts against the same
  account. `gnubook pp-status`, `pp-sync`, `pp-update` and the hourly timer `gnubook-pp.timer`; installer with
  signature check of the PP release, rebuild on `gnubook-update`, `gnubook-pp-update` with rollback; CI builds
  and starts pp-core with the tested PP release
- Nextcloud copy: the Portfolio Performance file is uploaded next to the `.gnucash` copy (`<name>-PP.xml`)
  after every change of the PP file
- Demo: the shared demo user sees the Portfolio Performance pages read-only with a made-up PP file that pp-core
  builds for the demo period (`POST /clients/{id}/demo`)
- Fixed: the PostgreSQL run of the net worth price test opened the book as SQLite file
- Portfolio report (*Depot*): holdings with average-cost basis, unrealized/realized gain and 12-month price
  change, allocation donut, market value vs. cost basis over time, price history per security; the demo book
  has an ETF savings plan and a share that is partly sold; the shared demo book is rebuilt at once when the
  demo data changes (`DEMO_VERSION`), not only at the next month
- Demo from the login page (`[app] demo`, on by default): *Demo ansehen* logs in as a shared, read-only
  demo user. Its book is the synthetic demo book, rebuilt monthly so the data reaches the current month.
  Every change is refused unless `[app] demo_writable = true`; never API tokens, Nextcloud or password
  change
- Net worth report (*Nettovermögen*): monthly assets, liabilities and net worth as a line chart (server-side
  SVG), historical prices for securities and foreign currencies, change over the period, composition by
  account group, monthly table; linked from the dashboard

## 0.4.0 – bank profiles

- Country/bank-specific code moved to `gnubook/banks/` (`de`: STAND/ENDSALDO/Kontostand, AqBanking-style
  booking text, BLZ + Kontonummer; `generic`); the core only calls the profile hooks
- Bank profile and import settings per book (*Bücher → Bankprofil und Import*), overriding `[import]`
- Own balance-line patterns in `[[checkpoints.patterns]]` for every profile
- German and English user interface; test that every UI string has a translation

## 0.3.0 – gnubook creates the databases

- `[postgres] admin_url` (role with CREATEROLE + CREATEDB): new books get their own PostgreSQL role and
  database with a random password, filled empty, with a simple German chart of accounts, or from an uploaded
  GnuCash SQLite file; credentials for GnuCash Desktop are shown once
- "Neuer Benutzer" can create the user's own book in the same step; `gnubook book-create`
- deleting a book gnubook created can drop database and role (name confirmation, last `.gnucash` copy first)

## 0.2.0 – several users and books

- Users with their own passwords; books (one GnuCash database each), related n:m; book switcher in the header
- Admin pages for users and books (connection is tested before saving); users change their own password
- API tokens per user and book, created under Einstellungen or with `gnubook token-create`
- gnubook's own data per book (`data/books/<id>.sqlite`), users/books/tokens in `data/system.sqlite`
- `.gnucash` copy after every change, configured per book
- CLI: `user-add`, `user-list`, `user-password`, `book-add`, `book-list`, `token-create`, `--book` for
  `check-balances` and `backup`
- A 0.1 single-user configuration is taken over automatically on first start
## 0.1.0 – first version
- Dashboard, account tree, registers with running balance, search
- Transaction editor with free splits, quick-fill, arithmetic in amount fields; edit, copy, delete
- Bank balance checkpoints (STAND / ENDSALDO / "Kontostand am") with acceptance of known differences and
  `gnubook check-balances`
- Firefly-III-compatible import API for bnw/firefly-iii-fints-importer: duplicate detection, linking to
  existing bookings, own-account transfers (optionally via a transit account), counter account from history,
  GnuCash's Bayesian import map or a fallback account; review page
- GnuCash lock handling (`gnclock`), optimistic concurrency, schema version check, audit log
- Installer, updater and systemd unit for Debian/Ubuntu LXC; demo book; tests for SQLite, PostgreSQL and
  read-back with the GnuCash engine
