# Changelog

## Unreleased

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
