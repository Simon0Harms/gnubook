# Changelog

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
