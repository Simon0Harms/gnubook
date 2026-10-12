"""Belegeingang: documents from the broker's online postbox (gnubook.postbox, docs/POSTBOX.md).

The page keeps the source settings (login, from which day, Nextcloud folder), asks for runs (a request file that
gnubook-postbox-request.path picks up – the web app never starts a browser) and lists the documents with their
state in Portfolio Performance and Nextcloud.
"""
from __future__ import annotations

import importlib.util
from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, session, url_for

from ..i18n import gettext as _
from ..postbox import DEFAULT_SOURCE, SOURCES, jobs
from ..postbox.service import PostboxService
from ..postbox.store import PP_ATTENTION
from . import registry, state
from .auth import is_demo_user, login_required
from .views import _parse_date

bp = Blueprint("postbox", __name__, url_prefix="/postbox")

PP_LABELS = {"pending": "offen", "imported": "übernommen", "partial": "teilweise", "review": "prüfen",
             "needs_target": "Depot wählen", "unsupported": "nicht erkannt", "skipped": "übersprungen",
             "error": "Fehler", "done": "erledigt", "off": "aus"}
NC_LABELS = {"pending": "offen", "done": "kopiert", "error": "Fehler", "off": "aus"}
RUN_LABELS = {"running": "läuft", "ok": "ok", "warning": "Hinweis", "dry_run": "Probelauf", "error": "Fehler",
              "login_failed": "Anmeldung fehlgeschlagen", "approval_required": "Freigabe verlangt",
              "not_installed": "nicht installiert", "busy": "läuft schon"}
MODE_LABELS = {"fetch": "Abruf", "dry-run": "Probelauf", "process": "Nachholen"}
PAGE = 100


@bp.before_request
def _guard():
    if not registry().cfg.postbox.enabled or is_demo_user():
        abort(404)


@bp.app_context_processor
def inject_postbox():
    return {"postbox_available": lambda: bool(registry().cfg.postbox.enabled and not
                                              (g.get("user") is not None and g.user["is_demo"]))}


def _kind() -> str:
    kind = request.values.get("source") or DEFAULT_SOURCE
    if kind not in SOURCES:
        abort(404)
    return kind


def _svc() -> PostboxService:
    return PostboxService(registry(), state())


def _back(kind: str, anchor: str = ""):
    return redirect(url_for("postbox.index", source=kind) + anchor)


@bp.route("/")
@login_required
def index():
    kind = _kind()
    svc = _svc()
    reg = registry()
    row = svc.source(kind)
    st = svc.settings_of(row)
    attention = request.args.get("filter") == "attention"
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        offset = 0
    if row is not None and state().pp is not None:
        try:
            svc.refresh_sessions(kind)  # decisions made on the PDF import page since the last run
        except Exception:  # noqa: BLE001 – the page shows the stored state anyway
            pass
    owner = reg.system.user(row["user_id"]) if row is not None and row["user_id"] else None
    nc_account = reg.system.nextcloud_account(g.user["id"]) if reg.cfg.nextcloud.enabled else None
    docs = svc.store.docs(kind, attention=attention, limit=PAGE + 1, offset=offset)
    return render_template(
        "postbox/index.html", kind=kind, label=SOURCES[kind], row=row, st=st, owner=owner, nc_account=nc_account,
        nc_enabled=reg.cfg.nextcloud.enabled, pp_on=state().pp is not None,
        docs=docs[:PAGE], more=len(docs) > PAGE, offset=offset, page=PAGE, attention=attention,
        counts=svc.store.counts(kind), runs=svc.store.runs(kind, 8), running=svc.store.running(kind),
        requests=[r for r in jobs.pending(reg.cfg.app.data_dir, state().id) if r.get("source") in (kind, "", None)],
        complete_day=svc.store.complete_day(kind), playwright=importlib.util.find_spec("playwright") is not None,
        paused=svc.paused(kind, row) if row is not None else None,
        PP_LABELS=PP_LABELS, NC_LABELS=NC_LABELS, RUN_LABELS=RUN_LABELS, MODE_LABELS=MODE_LABELS,
        PP_ATTENTION=PP_ATTENTION)


@bp.route("/source", methods=["POST"])
@login_required(demo_never=True)
def save_source():
    from ..nextcloud import NextcloudError, clean_folder

    kind = _kind()
    reg, ctx = registry(), state()
    f = request.form
    row = reg.system.postbox_source(ctx.id, kind)
    st = PostboxService.settings_of(row)
    login = (f.get("login") or "").strip()
    password = f.get("password") or ""
    since = _parse_date(f.get("since"))
    rescan = _parse_date(f.get("rescan_from"))
    problems = []
    if not login:
        problems.append(_("Bitte den Benutzernamen angeben."))
    if not password and (row is None or not row["secret"]):
        problems.append(_("Bitte das Passwort angeben."))
    if since is None:
        problems.append(_("Bitte angeben, ab welchem Tag Belege geholt werden („Belege ab“)."))
    elif since > date.today():
        problems.append(_("„Belege ab“ liegt in der Zukunft."))
    if rescan is not None and since is not None and rescan < since:
        problems.append(_("„Einmalig erneut prüfen ab“ liegt vor „Belege ab“."))
    try:
        overlap = max(0, min(60, int(f.get("overlap_days") or 3)))
    except ValueError:
        overlap = 3
    nc_folder = (f.get("nc_folder") or "").strip()
    if nc_folder:
        try:
            nc_folder = clean_folder(nc_folder)
        except NextcloudError as exc:
            problems.append(str(exc))
    if problems:
        for p in problems:
            flash(p, "danger")
        return _back(kind, "#source")
    if rescan is None and row is not None and st.since_day is not None and since < st.since_day:
        rescan = since  # "Belege ab" moved back: the next run lists back to the new day once
    st.enabled = f.get("enabled") == "1"
    st.since = since.isoformat()
    st.overlap_days = overlap
    st.rescan_from = rescan.isoformat() if rescan else ""
    st.import_pp = f.get("import_pp") == "1"
    st.nc_folder = nc_folder
    st.nc_year_folders = f.get("nc_year_folders") == "1"
    reg.system.set_postbox_source(ctx.id, kind, g.user["id"], login, password or None, st.to_json())
    ctx.appdb.audit(session.get("user", "?"), "postbox-source", None,
                    f"{SOURCES[kind]}: {login}, ab {st.since}" + (" (Passwort geändert)" if password else ""))
    msg = _("Gespeichert.")
    if nc_folder and reg.cfg.nextcloud.enabled and reg.system.nextcloud_account(g.user["id"]) is None:
        msg += " " + _("Für die Kopie in die Nextcloud zuerst unter Einstellungen eine Nextcloud verbinden.")
    flash(msg, "success")
    return _back(kind)


@bp.route("/source/delete", methods=["POST"])
@login_required(demo_never=True)
def delete_source():
    kind = _kind()
    ctx = state()
    registry().system.delete_postbox_source(ctx.id, kind)
    ctx.appdb.audit(session.get("user", "?"), "postbox-source-delete", None, SOURCES[kind])
    flash(_("Zugangsdaten entfernt. Abgerufene Belege und ihre Liste bleiben erhalten."), "success")
    return _back(kind)


@bp.route("/run", methods=["POST"])
@login_required(demo_never=True)
def run():
    kind = _kind()
    mode = request.form.get("mode") or "fetch"
    if mode not in jobs.MODES:
        abort(400)
    reg, ctx = registry(), state()
    if reg.system.postbox_source(ctx.id, kind) is None:
        flash(_("Zuerst die Zugangsdaten speichern."), "warning")
        return _back(kind)
    jobs.add_request(reg.cfg.app.data_dir, ctx.id, kind, mode, g.user["username"])
    flash(_("Angefordert – der Abruf startet in wenigen Sekunden im Hintergrund. Die Seite neu laden, um das Ergebnis zu sehen."), "info")
    return _back(kind)


def _doc_or_404(svc, doc_id: int, kind: str):
    row = svc.store.doc(doc_id)
    if row is None or row["source"] != kind:
        abort(404)
    return row


@bp.route("/docs/<int:doc_id>/pdf")
@login_required
def doc_pdf(doc_id):
    kind = _kind()
    svc = _svc()
    row = _doc_or_404(svc, doc_id, kind)
    path = svc.doc_path(row)
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="application/pdf", download_name=row["filename"],
                     as_attachment=request.args.get("download") == "1")


@bp.route("/docs/<int:doc_id>/review", methods=["POST"])
@login_required(demo_never=True)
def doc_review(doc_id):
    from ..pp.client import PPCoreError

    kind = _kind()
    svc = _svc()
    _doc_or_404(svc, doc_id, kind)
    if state().pp is None:
        abort(404)
    try:
        sid = svc.review(doc_id)
    except (PPCoreError, OSError) as exc:
        flash(str(exc), "danger")
        return _back(kind, "#docs")
    if not sid:
        flash(_("Keine Importsitzung erhalten."), "warning")
        return _back(kind, "#docs")
    session["pp_import"] = sid
    return redirect(url_for("pp.pdf_import", session=sid))


@bp.route("/docs/<int:doc_id>/done", methods=["POST"])
@login_required(demo_never=True)
def doc_done(doc_id):
    kind = _kind()
    svc = _svc()
    row = _doc_or_404(svc, doc_id, kind)
    svc.store.set_pp(doc_id, "done", _("vom Benutzer als erledigt markiert"), row["pp_items"], row["pp_imported"])
    state().appdb.audit(session.get("user", "?"), "postbox-done", None, f"{row['external_id']} {row['title']}")
    return _back(kind, "#docs")


@bp.route("/docs/<int:doc_id>/nextcloud", methods=["POST"])
@login_required(demo_never=True)
def doc_nextcloud(doc_id):
    kind = _kind()
    svc = _svc()
    _doc_or_404(svc, doc_id, kind)
    ok, message = svc.upload_one(kind, doc_id)
    if ok:
        flash(_("In die Nextcloud kopiert."), "success")
    else:
        flash(_("Nicht kopiert: {a0}", a0=message), "danger")
    return _back(kind, "#docs")


@bp.route("/nextcloud/all", methods=["POST"])
@login_required(demo_never=True)
def nextcloud_all():
    kind = _kind()
    reg, ctx = registry(), state()
    svc = _svc()
    n = svc.store.reset_nc(kind)
    jobs.add_request(reg.cfg.app.data_dir, ctx.id, kind, "process", g.user["username"])
    flash(_("{a0} Belege werden im Hintergrund in die Nextcloud kopiert.", a0=n), "info")
    return _back(kind)
