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
# Users and books (GnuCash databases) are managed in the web UI (Benutzer / Bücher) or with
#   gnubook user-add NAME --admin
#   gnubook book-add NAME postgresql://USER:PASSWORD@HOST:5432/DB --user NAME
# The [book] url / [app] username + password_hash / [api] token_sha256 settings of version 0.1 are taken
# over once on first start (as book "Hauptbuch" and an admin user) and are not needed for new setups.
[book]
url = "{url}"
timezone = "Europe/Berlin"

[app]
secret_key = "{secret}"
data_dir = "{data_dir}"
# true when gnubook is reached via HTTPS (reverse proxy)
session_cookie_secure = false
behind_proxy = false

[api]
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

[postgres]
# optional: role with CREATEROLE + CREATEDB (no superuser) so gnubook can create a database per book
# admin_url = "postgresql://gnubook_admin:PASSWORD@192.168.1.30:5432/postgres"
# client_host = "192.168.1.30"   # how GnuCash Desktop reaches PostgreSQL

[backup]
# number of .gnucash versions kept per book (the file itself is set per book under "Bücher")
keep = 10

[nextcloud]
# users can copy the .gnucash backup into their own Nextcloud (Einstellungen → Nextcloud)
enabled = true
allow_http = false
# stored Nextcloud passwords are encrypted with a key derived from this value (default: [app] secret_key).
# Changing it makes them unreadable – users then connect their Nextcloud again.
# encryption_key = ""

# Portfolio Performance (securities): deploy/install-pp.sh installs pp-core and adds this section
# [pp]
# url = "http://127.0.0.1:8091"
# token = "same as token in /opt/gnubook/ppcore.properties"
# quotes_interval_hours = 12

# Belegeingang (documents from the finanzen.net zero postbox): deploy/install-postbox.sh adds this section
# [postbox]
# enabled = true
# browser = "firefox"
# browsers_path = "/opt/gnubook/pw-browsers"

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
def init_config(path, url, data_dir):
    """Konfigurationsdatei mit zufälligem secret_key anlegen."""
    p = Path(path)
    if p.exists():
        raise click.ClickException(f"{p} existiert bereits.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(CONFIG_TEMPLATE.format(url=url, secret=secrets.token_urlsafe(48), data_dir=data_dir,
                                        ), encoding="utf-8")
    p.chmod(0o600)
    click.echo(f"{p} angelegt. Weiter: gnubook user-add NAME --admin und gnubook book-add NAME URL --user NAME")


def _registry(ctx):
    from .system import Registry

    return Registry(_cfg(ctx))


def _book_ctx(reg, ref):
    """Book by id or name; without ref the only book."""
    books = reg.system.books()
    if ref is None:
        if len(books) != 1:
            raise click.ClickException("Mehrere Bücher – bitte --book NAME|ID angeben: "
                                       + ", ".join(f"{b['id']}={b['name']}" for b in books) if books
                                       else "Noch kein Buch verbunden (gnubook book-add).")
        return reg.context(books[0]["id"])
    for b in books:
        if str(b["id"]) == str(ref) or b["name"].casefold() == str(ref).casefold():
            return reg.context(b["id"])
    raise click.ClickException(f"Buch nicht gefunden: {ref}")


book_option = click.option("--book", "book_ref", default=None, help="Buch (Name oder ID); nötig bei mehreren Büchern")


@main.command("check")
@click.pass_context
def check(ctx):
    """Konfiguration, Benutzer und Verbindung zu allen Büchern prüfen."""
    from .config import validate_for_web
    from .system import mask_url

    cfg = _cfg(ctx)
    click.echo(f"Konfiguration: {cfg.source or '(nur Umgebungsvariablen)'}")
    problems = validate_for_web(cfg)
    for p in problems:
        click.echo(f"  FEHLER: {p}")
    reg = _registry(ctx)
    users = reg.system.users()
    click.echo(f"Benutzer: {len(users)} ({sum(1 for u in users if u['is_admin'])} Admin)")
    if not users:
        problems.append("kein Benutzer")
        click.echo("  FEHLER: noch kein Benutzer – gnubook user-add NAME --admin")
    for b in reg.system.books():
        try:
            bc = reg.context(b["id"])
            info = bc.book.schema_info()
            locks = bc.book.lock_holders()
            n = len(bc.book.load_accounts().by_guid)
            ok = info["supported"]
            click.echo(f"Buch {b['id']} „{b['name']}“: {mask_url(b['url'])} – GnuCash {info['gnucash']}, "
                       f"Schema {'unterstützt' if ok else 'UNBEKANNT'}, {n} Konten, Sperre: "
                       + (", ".join(f"{h} (PID {p})" for h, p in locks) if locks else "keine"))
            if not ok:
                problems.append(b["name"])
        except Exception as exc:  # noqa: BLE001
            problems.append(b["name"])
            click.echo(f"Buch {b['id']} „{b['name']}“: FEHLER {exc}")
    reg.dispose()
    sys.exit(1 if problems else 0)


@main.command("user-add")
@click.argument("username")
@click.option("--admin", is_flag=True, help="Administrator (verwaltet Benutzer und Bücher)")
@click.option("--book", "books", multiple=True, help="Buch (Name oder ID), mehrfach möglich")
@click.pass_context
def user_add(ctx, username, admin, books):
    """Benutzer anlegen (Passwort wird abgefragt)."""
    from .system import UserError

    reg = _registry(ctx)
    pw = getpass.getpass("Passwort: ")
    if getpass.getpass("Wiederholen: ") != pw:
        raise click.ClickException("Die Eingaben stimmen nicht überein.")
    try:
        uid = reg.system.add_user(username, pw, admin)
    except UserError as exc:
        raise click.ClickException(str(exc))
    for ref in books:
        reg.system.grant(uid, _book_ctx(reg, ref).id)
    click.echo(f"Benutzer {username} angelegt.")


@main.command("user-list")
@click.pass_context
def user_list(ctx):
    """Benutzer und ihre Bücher anzeigen."""
    reg = _registry(ctx)
    for u in reg.system.users():
        books = ", ".join(b["name"] for b in reg.system.user_books(u["id"])) or "–"
        flags = ("Admin " if u["is_admin"] else "") + ("" if u["active"] else "gesperrt")
        click.echo(f"{u['id']:>3} {u['username']:<20} {flags:<15} {books}")


@main.command("user-password")
@click.argument("username")
@click.pass_context
def user_password(ctx, username):
    """Passwort eines Benutzers neu setzen."""
    from .system import UserError

    reg = _registry(ctx)
    u = reg.system.user_by_name(username)
    if u is None:
        raise click.ClickException(f"Benutzer nicht gefunden: {username}")
    pw = getpass.getpass("Neues Passwort: ")
    if getpass.getpass("Wiederholen: ") != pw:
        raise click.ClickException("Die Eingaben stimmen nicht überein.")
    try:
        reg.system.set_password(u["id"], pw)
    except UserError as exc:
        raise click.ClickException(str(exc))
    click.echo("Passwort gesetzt.")


@main.command("book-add")
@click.argument("name")
@click.argument("url")
@click.option("--timezone", default="Europe/Berlin", show_default=True)
@click.option("--user", "users", multiple=True, help="Benutzer, die das Buch nutzen dürfen")
@click.option("--no-backup", is_flag=True, help="keine .gnucash-Sicherung nach jeder Änderung")
@click.pass_context
def book_add(ctx, name, url, timezone, users, no_backup):
    """Vorhandenes GnuCash-Buch (Datenbank-URL) verbinden."""
    from .book import Book

    reg = _registry(ctx)
    b = Book(url, timezone)
    info = b.schema_info()
    n = len(b.load_accounts().by_guid)
    b.dispose()
    if not info["supported"]:
        raise click.ClickException(f"GnuCash-Version {info['gnucash']} wird nicht unterstützt.")
    bid = reg.system.add_book(name, url, timezone, "" if no_backup else reg.default_backup_file(name))
    for un in users:
        u = reg.system.user_by_name(un)
        if u is None:
            raise click.ClickException(f"Benutzer nicht gefunden: {un}")
        reg.system.grant(u["id"], bid)
    click.echo(f"Buch {bid} „{name}“ verbunden ({n} Konten).")


@main.command("book-create")
@click.argument("name")
@click.option("--content", type=click.Choice(["simple", "empty", "file"]), default="simple", show_default=True,
              help="simple = einfacher Kontenrahmen, empty = leer, file = Inhalt aus --file")
@click.option("--file", "path", type=click.Path(exists=True, dir_okay=False), help="GnuCash-Datei im SQLite-Format")
@click.option("--user", "users", multiple=True, help="Benutzer, die das Buch nutzen dürfen")
@click.option("--no-backup", is_flag=True)
@click.pass_context
def book_create(ctx, name, content, path, users, no_backup):
    """Neue PostgreSQL-Datenbank + Rolle + GnuCash-Buch anlegen (braucht [postgres] admin_url)."""
    from .system import UserError

    reg = _registry(ctx)
    ids = []
    for un in users:
        u = reg.system.user_by_name(un)
        if u is None:
            raise click.ClickException(f"Benutzer nicht gefunden: {un}")
        ids.append(u["id"])
    if content == "file" and not path:
        raise click.ClickException("--file fehlt")
    try:
        bid, cr = reg.create_book(name, "upload" if content == "file" else content, path, ids, not no_backup)
    except UserError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Buch {bid} „{name}“ angelegt. Zugang für GnuCash Desktop (Datenformat postgres):")
    click.echo(f"  Host {cr['host']}:{cr['port']}  Datenbank {cr['database']}  Benutzer {cr['user']}  "
               f"Passwort {cr['password']}")


@main.command("book-list")
@click.pass_context
def book_list(ctx):
    """Verbundene Bücher anzeigen."""
    from .system import mask_url

    reg = _registry(ctx)
    for b in reg.system.books():
        users = ", ".join(u["username"] for u in reg.system.users() if u["id"] in reg.system.book_users(b["id"]))
        click.echo(f"{b['id']:>3} {b['name']:<20} {mask_url(b['url'])}  Benutzer: {users or '–'}")


@main.command("token-create")
@click.argument("username")
@book_option
@click.option("--label", default="FinTS-Importer", show_default=True)
@click.pass_context
def token_create(ctx, username, book_ref, label):
    """API-Token für den FinTS-Importer (ein Benutzer, ein Buch)."""
    reg = _registry(ctx)
    u = reg.system.user_by_name(username)
    if u is None:
        raise click.ClickException(f"Benutzer nicht gefunden: {username}")
    bc = _book_ctx(reg, book_ref)
    if not reg.system.may_use(u["id"], bc.id):
        raise click.ClickException(f"{username} darf das Buch „{bc.name}“ nicht nutzen.")
    click.echo("Token für den FinTS-Importer (firefly_access_token) – nur jetzt sichtbar:")
    click.echo(f"  {reg.system.create_token(u['id'], bc.id, label)}")


@main.command("check-balances")
@book_option
@click.option("--account", "accounts", multiple=True, help="nur dieses Konto (voller Name oder GUID), mehrfach möglich")
@click.option("--show-all", is_flag=True, help="auch stimmige Prüfpunkte ausgeben")
@click.option("--accept-open", is_flag=True, help="alle offenen Abweichungen als bekannt akzeptieren")
@click.option("--note", default="per CLI akzeptiert", help="Notiz für --accept-open")
@click.pass_context
def check_balances(ctx, book_ref, accounts, show_all, accept_open, note):
    """Bank-Saldo-Prüfpunkte nachrechnen (Exit-Code 1 bei offenen Abweichungen)."""
    from . import checkpoints as cps
    from .money import fmt

    reg = _registry(ctx)
    bc = _book_ctx(reg, book_ref)
    book, appdb = bc.book, bc.appdb
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
@book_option
@click.option("--dir", "directory", default=None, help="Zielverzeichnis (Standard: <data_dir>/backup/manual)")
@click.option("--keep", default=30, show_default=True, help="so viele Sicherungen behalten")
@click.option("--prefix", default=None, help="Dateiname-Präfix (Standard: Buchname)")
@click.pass_context
def backup(ctx, book_ref, directory, keep, prefix):
    """Buch als .gnucash-Datei (SQLite) sichern – mit GnuCash Desktop direkt öffnbar."""
    from datetime import datetime

    from .backup import export_gnucash_file, rotate
    from .system import slug

    reg = _registry(ctx)
    bc = _book_ctx(reg, book_ref)
    prefix = prefix or slug(bc.name)
    out_dir = Path(directory) if directory else Path(reg.cfg.app.data_dir) / "backup" / "manual"
    target = out_dir / f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}.gnucash"
    export_gnucash_file(bc.book, target)
    rotate(out_dir, prefix, keep)
    click.echo(f"Gesichert: {target}")


def _pp_books(reg, book_ref):
    """Book contexts with the Portfolio Performance link switched on (all, or the given one)."""
    if not reg.cfg.pp.url:
        raise click.ClickException("Portfolio Performance ist nicht eingerichtet: [pp] url fehlt in config.toml.")
    if book_ref is not None:
        ctxs = [_book_ctx(reg, book_ref)]
    else:
        ctxs = [reg.context(b["id"]) for b in reg.system.books()]
    return [c for c in ctxs if c is not None and c.pp is not None and c.pp.enabled]


@main.command("pp-sync")
@book_option
@click.option("--force", is_flag=True, help="auch ohne Änderung in PP alles prüfen")
@click.option("--dry-run", is_flag=True, help="nur anzeigen, was sich ändern würde")
@click.pass_context
def pp_sync(ctx, book_ref, force, dry_run):
    """Portfolio-Performance-Datei ins GnuCash-Buch übernehmen (Buchungen und Kurse)."""
    from .book import WriteLockError
    from .pp.client import PPCoreError
    from .pp.sync import SyncError

    reg = _registry(ctx)
    failed = 0
    for bc in _pp_books(reg, book_ref):
        try:
            if not force and not dry_run and not bc.pp.needs_sync():
                click.echo(f"{bc.name}: unverändert")
                continue
            r = bc.pp.sync(actor="cli", dry_run=dry_run, wait=600)
            click.echo(f"{bc.name}: {r.summary()}")
            for e in r.errors:
                click.echo(f"  Fehler: {e}")
            if r.wrote:
                bc.backup.flush()  # .gnucash copy / Nextcloud, as after a change in the web app
        except WriteLockError as exc:
            click.echo(f"{bc.name}: {exc}")
            failed += 1
        except (PPCoreError, SyncError) as exc:
            click.echo(f"{bc.name}: {exc}", err=True)
            failed += 1
    reg.dispose()
    sys.exit(1 if failed else 0)


@main.command("pp-update")
@book_option
@click.option("--quotes/--no-quotes", default=None,
              help="Kurse jetzt aktualisieren (Standard: wenn älter als [pp] quotes_interval_hours)")
@click.pass_context
def pp_update(ctx, book_ref, quotes):
    """Kurse in Portfolio Performance aktualisieren und alles ins Buch übernehmen (für den Timer)."""
    from datetime import datetime, timezone

    from .book import WriteLockError
    from .pp.client import PPCoreError
    from .pp.service import SyncBusy
    from .pp.sync import SyncError

    reg = _registry(ctx)
    failed = 0
    for bc in _pp_books(reg, book_ref):
        try:
            due = quotes
            if due is None:
                last = (bc.pp.client.summary(bc.pp.cid) or {}).get("lastPriceUpdate")
                age = None
                if last:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00")))
                due = age is None or age.total_seconds() >= reg.cfg.pp.quotes_interval_hours * 3600
            if due:
                job = bc.pp.client.quotes_start(bc.pp.cid, wait=900)
                n = len(job.get("securities") or [])
                errors = [s for s in job.get("securities") or [] if s.get("status") == "error"]
                click.echo(f"{bc.name}: Kurse für {n} Wertpapiere geprüft, {job.get('modified', 0)} aktualisiert"
                           + (f", {len(errors)} Fehler" if errors else ""))
                for s in errors:
                    click.echo(f"  {s.get('name')}: {s.get('message')}")
                if job.get("modified"):
                    bc.pp.request_copy()  # new prices are in the PP file: copy it into Nextcloud
            if bc.pp.needs_sync():
                r = bc.pp.sync(actor="timer", wait=600)
                click.echo(f"{bc.name}: {r.summary()}")
            else:
                click.echo(f"{bc.name}: Buch ist aktuell")
            bc.backup.flush()
        except (WriteLockError, SyncBusy) as exc:
            click.echo(f"{bc.name}: {exc} – nächster Versuch beim nächsten Lauf")
        except (PPCoreError, SyncError) as exc:
            click.echo(f"{bc.name}: {exc}", err=True)
            failed += 1
    reg.dispose()
    sys.exit(1 if failed else 0)


@main.command("pp-status")
@book_option
@click.pass_context
def pp_status(ctx, book_ref):
    """Stand der Portfolio-Performance-Anbindung anzeigen."""
    from .pp.client import PPCoreError

    reg = _registry(ctx)
    if not reg.cfg.pp.url:
        raise click.ClickException("[pp] url fehlt in config.toml.")
    from .pp.client import PPCoreClient

    client = PPCoreClient(reg.cfg.pp.url, reg.cfg.pp.token, reg.cfg.pp.timeout)
    try:
        h = client.health()
        click.echo(f"pp-core erreichbar: Portfolio Performance {h.get('ppVersion')}")
    except PPCoreError as exc:
        raise click.ClickException(str(exc))
    books = [reg.context(b["id"]) for b in reg.system.books()] if book_ref is None else [_book_ctx(reg, book_ref)]
    for bc in books:
        st = bc.pp.settings()
        try:
            summary = client.summary(bc.pp.cid)
        except PPCoreError as exc:
            summary = {"error": str(exc)}
        line = f"{bc.name}: Übernahme {'an' if st.enabled else 'aus'}"
        if summary.get("exists"):
            line += (f", PP-Datei {summary.get('originalName') or summary.get('file')} "
                     f"({summary.get('transactions')} Buchungen, Kurse {summary.get('lastPriceUpdate') or '–'})")
        else:
            line += ", noch keine PP-Datei"
        click.echo(line)
        runs = bc.appdb.pp_runs(1)
        if runs:
            click.echo(f"  letzter Lauf {runs[0]['finished_at']}: {runs[0]['status']} – {runs[0]['summary']}")
    reg.dispose()


def _postbox_jobs(reg, book_ref, source, scheduled, from_requests, mode):
    """[(book id, source kind, mode, actor)] for postbox-fetch."""
    from .postbox import SOURCES
    from .postbox import jobs as pjobs
    from .postbox.settings import SourceSettings

    if from_requests:
        reqs = pjobs.take(reg.cfg.app.data_dir)
        actors = {}
        for r in reqs:
            actors.setdefault((int(r["book"]), r.get("source") or ""), r.get("actor") or "web")
        out = []
        for (book_id, kind), modes in pjobs.merge(reqs).items():
            kinds = [kind] if kind else [s["kind"] for s in reg.system.postbox_sources(book_id)]
            out += [(book_id, k, m, actors[(book_id, kind)]) for m in modes for k in kinds if k in SOURCES]
        return out
    book_ids = None
    if book_ref is not None:
        book_ids = {_book_ctx(reg, book_ref).id}
    out = []
    for s in reg.system.postbox_sources():
        if book_ids is not None and s["book_id"] not in book_ids:
            continue
        if source and s["kind"] != source:
            continue
        if scheduled and not SourceSettings.from_json(s["settings"]).enabled:
            continue
        out.append((s["book_id"], s["kind"], mode, "timer" if scheduled else "cli"))
    return out


REQUEST_WAIT = 2 * 3600  # seconds; gnubook-postbox-request.service allows 3 hours


@main.command("postbox-fetch")
@book_option
@click.option("--source", default=None, help="nur diese Quelle (finanzen-zero)")
@click.option("--scheduled", is_flag=True, help="alle Postfächer mit „automatisch abrufen“ (für den Timer)")
@click.option("--requests", "from_requests", is_flag=True,
              help="die in der Weboberfläche angeforderten Läufe ausführen (für gnubook-postbox-request.path)")
@click.option("--dry-run", is_flag=True, help="nur anmelden und die Liste lesen, nichts herunterladen")
@click.option("--process-only", is_flag=True, help="nichts abrufen, nur PP-Import und Nextcloud-Kopie nachholen")
@click.pass_context
def postbox_fetch(ctx, book_ref, source, scheduled, from_requests, dry_run, process_only):
    """Belege aus dem Postfach holen, in Portfolio Performance importieren und in die Nextcloud kopieren."""
    from .postbox.service import PostboxService

    reg = _registry(ctx)
    if not reg.cfg.postbox.enabled:
        if from_requests:
            from .postbox import jobs as pjobs

            pjobs.take(reg.cfg.app.data_dir)  # nobody would run them – do not trigger the path unit forever
        reg.dispose()
        raise click.ClickException("Belegeingang ist aus: [postbox] enabled = true fehlt in config.toml "
                                   "(deploy/install-postbox.sh).")
    import signal
    import time as _time

    # systemd stops a run with SIGTERM: end it like an error, so the run is recorded and the browser closed
    signal.signal(signal.SIGTERM, lambda *_a: sys.exit(143))
    mode = "dry-run" if dry_run else ("process" if process_only else "fetch")
    todo = _postbox_jobs(reg, book_ref, source, scheduled, from_requests, mode)
    deadline = _time.monotonic() + REQUEST_WAIT  # one waiting budget for all requested runs
    if not todo and not from_requests and not scheduled:
        click.echo("Kein Postfach eingerichtet (Weboberfläche → Belegeingang).")
    failed = 0
    for book_id, kind, job_mode, actor in todo:
        bc = reg.context(book_id)
        if bc is None:
            continue
        # a requested run waits for a run of the same postbox that is in progress (instead of being dropped)
        wait = max(0.0, deadline - _time.monotonic()) if from_requests else 0
        out = PostboxService(reg, bc).run(kind, actor, job_mode, sync_now=True, scheduled=scheduled, wait=wait)
        click.echo(f"{bc.name} ({kind}): {out.status} – {out.message}")
        failed += 1 if out.failed else 0
    reg.dispose()
    sys.exit(1 if failed else 0)


@main.command("postbox-status")
@book_option
@click.pass_context
def postbox_status(ctx, book_ref):
    """Stand des Belegeingangs anzeigen."""
    from .postbox import SOURCES
    from .postbox.service import PostboxService

    reg = _registry(ctx)
    books = [reg.context(b["id"]) for b in reg.system.books()] if book_ref is None else [_book_ctx(reg, book_ref)]
    for bc in books:
        if bc is None:
            continue
        svc = PostboxService(reg, bc)
        for row in reg.system.postbox_sources(bc.id):
            st = svc.settings_of(row)
            counts = svc.store.counts(row["kind"])
            done = svc.store.complete_day(row["kind"])
            click.echo(f"{bc.name}: {SOURCES.get(row['kind'], row['kind'])} als {row['login']}, "
                       f"automatisch {'an' if st.enabled else 'aus'}, Belege ab {st.since or '–'}, "
                       f"vollständig gelesen bis {done.isoformat() if done else '–'}, "
                       f"{counts['total']} Belege, {counts['attention']} zu prüfen")
            for r in svc.store.runs(row["kind"], 3):
                click.echo(f"  {r['started_at']} {r['actor']}: {r['status']} – {r['message'] or ''}")
    reg.dispose()


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
