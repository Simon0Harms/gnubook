"""Belegeingang: documents fetched from a broker's online postbox (docs/POSTBOX.md).

A headless browser (Playwright) logs in with the stored credentials, lists the postbox, clicks "Mehr anzeigen"
until it reaches documents it already has, and downloads the new ones. gnubook keeps every PDF in data_dir,
imports it into Portfolio Performance (pp-core, with PP's own importers) and copies it into a Nextcloud folder.

Modules:
    plan      which documents to fetch and where to stop listing (no I/O)
    settings  per-source settings (stored as JSON with the source in system.sqlite)
    store     documents and runs in the book's own database
    jobs      run requests from the web app, picked up by gnubook-postbox-request.path
    fzero     the finanzen.net zero web app (Playwright)
    service   one run: fetch, store, import into PP, upload into Nextcloud
"""
from __future__ import annotations

# kind -> name shown in the web interface
SOURCES = {"finanzen-zero": "finanzen.net zero"}
DEFAULT_SOURCE = "finanzen-zero"
