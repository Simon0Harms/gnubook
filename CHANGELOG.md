# Changelog

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
