"""Command line: gnubook <command>."""
from __future__ import annotations

import getpass
import hashlib
import secrets
import sys
from pathlib import Path

import click

from .config import ConfigError, load_config

CONFIG_TEMPLATE = """# gnubook configuration
[book]
# SQLAlchemy URL of the GnuCash book, e.g.
#   postgresql://gnucash:PASSWORD@10.0.0.20:5432/gnucash
#   sqlite:////opt/gnubook/data/book.gnucash
url = "{url}"
timezone = "Europe/Berlin"

[app]
secret_key = "{secret}"
data_dir = "{data_dir}"
username = "{username}"
# gnubook hash-password
password_hash = "{password_hash}"
# true when gnubook is reached via HTTPS (reverse proxy)
session_cookie_secure = false
behind_proxy = false

[api]
# sha256 of the token the FinTS importer uses (gnubook gen-token); empty = API off
token_sha256 = ""
expose_iban = false

[import]
# account for bank lines without a known counter account (full name or GUID)
fallback_account = "Ausgleichskonto-EUR"
# transfers between two imported bank accounts via a transit account (optional)
transit_account = ""
transit_between = []
# limit the accounts offered to the importer (full names); empty = all bank/asset accounts
accounts = []
match_days = 3
transfer_match_days = 7

[backup]
# copy of the book as GnuCash file after every change (empty = off); keep = number of versions
gnucash_file = "/opt/gnubook/data/backup/buch.gnucash"
keep = 10

[import.iban_map]
# "DE00123456780000000000" = "Aktiva:Barvermögen:Girokonto"
"""


@click.group()
@click.option("--config", "config_path", envvar="GNUBOOK_CONFIG", type=click.Path(dir_okay=False),
              help="Konfigurationsdatei (sonst $GNUBOOK_CONFIG, /etc/gnubook/config.toml, ./config.toml)")
@click.pass_context
def main(ctx, config_path):
    """gnubook – Web-Frontend für ein GnuCash-SQL-Buch."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


def _cfg(ctx):
    try:
        return load_config(ctx.obj.get("config_path"))
    except ConfigError as exc:
        raise click.ClickException(str(exc))


@main.command("hash-password")
def hash_password():
    """Passwort-Hash für [app] password_hash erzeugen."""
    from werkzeug.security import generate_password_hash

    pw = getpass.getpass("Neues Passwort: ")
    if len(pw) < 10:
        raise click.ClickException("Bitte mindestens 10 Zeichen verwenden.")
    if getpass.getpass("Wiederholen: ") != pw:
        raise click.ClickException("Die Eingaben stimmen nicht überein.")
    click.echo(generate_password_hash(pw))


@main.command("gen-token")
def gen_token():
    """API-Token für den FinTS-Importer erzeugen."""
    token = secrets.token_urlsafe(40)
    click.echo("Token für den FinTS-Importer (firefly_access_token) – nur jetzt sichtbar:")
    click.echo(f"  {token}")
    click.echo("In die gnubook-Konfiguration unter [api] eintragen:")
    click.echo(f'  token_sha256 = "{hashlib.sha256(token.encode()).hexdigest()}"')


@main.command("init-config")
@click.argument("path", type=click.Path(dir_okay=False))
@click.option("--url", default="", help="Buch-URL")
@click.option("--data-dir", default="/opt/gnubook/data")
@click.option("--username", default="admin")
def init_config(path, url, data_dir, username):
    """Konfigurationsdatei mit zufälligem secret_key anlegen."""
    p = Path(path)
    if p.exists():
        raise click.ClickException(f"{p} existiert bereits.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(CONFIG_TEMPLATE.format(url=url, secret=secrets.token_urlsafe(48), data_dir=data_dir,
                                        username=username, password_hash=""), encoding="utf-8")
    p.chmod(0o600)
    click.echo(f"{p} angelegt. Jetzt Buch-URL prüfen und `gnubook hash-password` ausführen.")


@main.command("check")
@click.pass_context
def check(ctx):
    """Konfiguration und Datenbankverbindung prüfen."""
    from .book import Book
    from .config import validate_for_web

    cfg = _cfg(ctx)
    click.echo(f"Konfiguration: {cfg.source or '(nur Umgebungsvariablen)'}")
    problems = validate_for_web(cfg)
    for p in problems:
        click.echo(f"  FEHLER: {p}")
    if not cfg.book.url:
        sys.exit(1)
    book = Book(cfg.book.url, cfg.book.timezone, cfg.book.account_separator)
    info = book.schema_info()
    click.echo(f"GnuCash-Buch: Version {info['gnucash']}, Schema {'unterstützt' if info['supported'] else 'UNBEKANNT'}")
    idx = book.load_accounts()
    click.echo(f"Konten: {len(idx.by_guid)}")
    locks = book.lock_holders()
    click.echo("Sperre: " + (", ".join(f"{h} (PID {p})" for h, p in locks) if locks else "keine"))
    sys.exit(1 if problems or not info["supported"] else 0)


@main.command("check-balances")
@click.option("--account", "accounts", multiple=True, help="nur dieses Konto (voller Name oder GUID), mehrfach möglich")
@click.option("--show-all", is_flag=True, help="auch stimmige Prüfpunkte ausgeben")
@click.option("--accept-open", is_flag=True, help="alle offenen Abweichungen als bekannt akzeptieren")
@click.option("--note", default="per CLI akzeptiert", help="Notiz für --accept-open")
@click.pass_context
def check_balances(ctx, accounts, show_all, accept_open, note):
    """Bank-Saldo-Prüfpunkte nachrechnen (Exit-Code 1 bei offenen Abweichungen)."""
    from . import checkpoints as cps
    from .appdb import AppDB
    from .book import Book
    from .money import fmt

    cfg = _cfg(ctx)
    book = Book(cfg.book.url, cfg.book.timezone, cfg.book.account_separator)
    appdb = AppDB(cfg.data_path / "gnubook.sqlite")
    index = book.load_accounts()
    only = None
    if accounts:
        only = set()
        for ref in accounts:
            acc = index.find(ref)
            if acc is None:
                raise click.ClickException(f"Konto nicht gefunden: {ref}")
            only.add(acc.guid)
    with book.connect() as conn:
        checks = cps.evaluate(conn, book, index, only, appdb.acceptances())
    open_total = 0
    for ag, check in sorted(checks.items(), key=lambda kv: index.get(kv[0]).full_name):
        acc = index.get(ag)
        c = check.counts
        click.echo(f"{acc.full_name}: {len(check.checkpoints)} Saldo-Angaben geprüft, {c['ok']} stimmen, "
                   f"{c['open']} abweichend, {c['accepted']} akzeptiert")
        for cp in check.checkpoints:
            if cp.status == "ok" and not show_all:
                continue
            label = {"ok": "ok   ", "open": "offen", "accepted": "akz. "}[cp.status]
            line = (f"  {label}: {cp.stand_date.isoformat()} (Abschluss {cp.tx_day.isoformat()})  "
                    f"Differenz STAND {fmt(cp.diff_stand)}")
            if cp.bank_end is not None:
                line += f", ENDSALDO {fmt(cp.diff_end)}"
            click.echo(line)
            if cp.status == "open" and accept_open:
                appdb.accept(ag, cp.tx_guid, cp.diff_stand, cp.diff_end, note)
        open_total += 0 if accept_open else c["open"]
    if not checks:
        click.echo("Keine Saldo-Angaben im Buch gefunden.")
    if accept_open:
        click.echo("Offene Abweichungen wurden akzeptiert.")
    sys.exit(1 if open_total else 0)


@main.command("backup")
@click.option("--dir", "directory", default=None, help="Zielverzeichnis (Standard: <data_dir>/../backup/book)")
@click.option("--keep", default=30, show_default=True, help="so viele Sicherungen behalten")
@click.option("--prefix", default="gnucash", show_default=True)
@click.pass_context
def backup(ctx, directory, keep, prefix):
    """Buch als .gnucash-Datei (SQLite) sichern – mit GnuCash Desktop direkt öffnbar."""
    from datetime import datetime

    from .backup import export_gnucash_file, rotate
    from .book import Book

    cfg = _cfg(ctx)
    book = Book(cfg.book.url, cfg.book.timezone, cfg.book.account_separator)
    out_dir = Path(directory) if directory else Path(cfg.app.data_dir).resolve().parent / "backup" / "book"
    target = out_dir / f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}.gnucash"
    export_gnucash_file(book, target)
    rotate(out_dir, prefix, keep)
    click.echo(f"Gesichert: {target}")


@main.command("demo-book")
@click.argument("path", type=click.Path(dir_okay=False))
def demo_book(path):
    """Fiktives Demo-Buch (SQLite) zum Ausprobieren anlegen."""
    from .demo import create_demo_book

    if Path(path).exists():
        raise click.ClickException(f"{path} existiert bereits.")
    click.echo(f"Demo-Buch angelegt: {create_demo_book(path)}")


@main.command("serve")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8080, type=int)
@click.pass_context
def serve(ctx, host, port):
    """Entwicklungsserver starten (im Betrieb gunicorn verwenden)."""
    from . import create_app

    app = create_app(_cfg(ctx))
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
