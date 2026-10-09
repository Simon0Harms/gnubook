"""Portfolio Performance in gnubook: figures, holdings, PDF import, prices and the link to the GnuCash book.

The PP file lives in pp-core (headless Portfolio Performance, see ppcore/). These pages talk to it and to the
synchronisation that books PP's transactions and prices into the GnuCash book (gnubook.pp).
"""
from __future__ import annotations

from ..i18n import gettext as _

import logging
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from flask import (Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)

from .. import reports as rp
from ..book import WriteLockError
from ..money import ZERO
from ..pp.client import PPCoreError
from ..pp.plan import SKIP_REASONS, build_plan
from ..pp.settings import ROLE_LABELS, ROLE_TYPES, PPSettings, default_name, resolved
from ..pp.sync import SyncError
from . import registry, state
from .auth import login_required
from .views import _parse_date, index

log = logging.getLogger("gnubook.web.pp")
bp = Blueprint("pp", __name__, url_prefix="/pp")

PERIOD_LABELS = {"12m": "Letzte 12 Monate", "ytd": "Dieses Jahr", "last_year": "Letztes Jahr",
                 "3y": "Letzte 3 Jahre", "5y": "Letzte 5 Jahre", "all": "Gesamter Zeitraum", "custom": "Zeitraum"}
CATEGORY_ORDER = ("INITIAL_VALUE", "CAPITAL_GAINS", "REALIZED_CAPITAL_GAINS", "EARNINGS", "FEES", "TAXES",
                  "CURRENCY_GAINS", "TRANSFERS", "FINAL_VALUE")
CATEGORY_SIGNS = {"CAPITAL_GAINS": "+", "REALIZED_CAPITAL_GAINS": "+", "EARNINGS": "+", "FEES": "−", "TAXES": "−",
                  "CURRENCY_GAINS": "+", "TRANSFERS": "+", "FINAL_VALUE": "="}
KIND_LABELS = {"BUY": "Kauf", "SELL": "Verkauf", "DIVIDENDS": "Dividende", "INTEREST": "Zinsen",
               "INTEREST_CHARGE": "Sollzinsen", "FEES": "Gebühren", "FEES_REFUND": "Gebührenerstattung",
               "TAXES": "Steuern", "TAX_REFUND": "Steuererstattung", "TRANSFER": "Depotwechsel",
               "DELIVERY_INBOUND": "Einlieferung", "DELIVERY_OUTBOUND": "Auslieferung", "OPENING": "Anfangsbestand",
               "DEPOSIT": "Einlage", "REMOVAL": "Entnahme", "TRANSFER_IN": "Umbuchung", "TRANSFER_OUT": "Umbuchung"}
STATUS_LABELS = {"booked": "übernommen", "edited": "in GnuCash bearbeitet", "conflict": "Konflikt",
                 "gone": "in GnuCash gelöscht", "detached": "abgekoppelt", "pending": "noch nicht übernommen",
                 "skipped": "nicht übernommen"}
IMPORT_STATUS = {"OK": "ok", "WARNING": "Warnung", "ERROR": "Fehler", "SKIP": "übersprungen"}


# what the shared demo user may see: the figures of a fictional PP file, nothing that changes or searches
DEMO_ENDPOINTS = {"pp.overview", "pp.holdings", "pp.transactions", "pp.securities", "pp.security", "pp.status_json"}


@bp.before_request
def _demo_guard():
    from .auth import is_demo_user

    if is_demo_user():
        if request.method != "GET" or request.endpoint not in DEMO_ENDPOINTS:
            abort(404)


def pp_demo() -> bool:
    from .auth import is_demo_user

    return is_demo_user()


def _ensure_demo_file(ctx):
    """The demo book gets a fictional PP file for the same period (rebuilt with the demo book every month)."""
    from ..demo import DEMO_SEED, demo_period

    start, months = demo_period(date.today())
    marker = f"{start.isoformat()}/{months}/{DEMO_SEED}"
    try:
        if _summary(ctx.pp).get("demo") != marker:
            ctx.pp.client.demo(ctx.pp.cid, start, months, DEMO_SEED)
            g.pop("pp_summary", None)
    except PPCoreError:
        pass  # the page shows the error itself


@bp.app_context_processor
def inject_pp():
    from .auth import is_demo_user

    def _pp_on():
        ctx = g.get("ctx")
        return bool(ctx is not None and ctx.pp is not None)
    return {"pp_available": _pp_on, "pp_readonly": is_demo_user}


def _svc():
    ctx = state()
    if ctx.pp is None:
        abort(404)
    if pp_demo():
        _ensure_demo_file(ctx)
    return ctx.pp


def _pp_error(exc: Exception, template: str = "pp/error.html"):
    return render_template(template, error=str(exc), unreachable=getattr(exc, "unreachable", False)), 503


def _summary(svc):
    """Status of the PP file in pp-core (cached per request)."""
    if "pp_summary" not in g:
        g.pp_summary = svc.client.summary(svc.cid)
    return g.pp_summary


def _dec(v) -> Decimal | None:
    if v in (None, ""):
        return None
    if isinstance(v, dict):
        v = v.get("amount")
    return Decimal(str(v))


# ------------------------------------------------------------------------------------------ overview

def _chart(series, attr: str, width: float = 900, height: float = 260, percent: bool = False,
           second: str | None = None):
    """SVG coordinates for one line (plus an optional second one), server-side like the other reports."""
    if len(series) < 2:
        return None
    vals = [float(getattr(p, attr)) for p in series]
    vals2 = [float(getattr(p, second)) for p in series] if second else []
    lo, hi = min(vals + vals2 + [0.0]), max(vals + vals2 + [0.0])
    ticks = rp.nice_ticks(lo, hi)
    y0, y1 = ticks[0], ticks[-1]
    n = len(series)

    def x(i):
        return i * width / (n - 1)

    def y(v):
        return height - (v - y0) / (y1 - y0) * height if y1 != y0 else height / 2

    coords = [(x(i), y(v)) for i, v in enumerate(vals)]
    labels, last = [], None
    span_days = (series[-1].day - series[0].day).days
    for i, p in enumerate(series):
        key = p.day.year if span_days > 900 else (p.day.year, p.day.month)
        if key != last:
            if last is not None:
                labels.append({"x": x(i), "text": str(p.day.year) if span_days > 900 else p.day.strftime("%m/%y")})
            last = key
    if len(labels) > 14:
        step = -(-len(labels) // 12)
        labels = labels[::step]
    fmt_tick = (lambda t: f"{t * 100:.0f} %".replace(".", ",")) if percent else (
        lambda t: f"{t:,.0f}".replace(",", "."))
    pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in coords)
    pts2 = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals2)) if vals2 else ""
    zero = y(0.0)
    return {"width": width, "height": height, "points": pts, "points2": pts2, "zero": zero,
            "area": f"M0,{zero:.1f} L{pts.replace(' ', ' L')} L{width:.1f},{zero:.1f} Z",
            "ticks": [{"y": y(t), "text": fmt_tick(t)} for t in ticks], "labels": labels,
            "dots": [{"x": a, "y": b, "p": p} for (a, b), p in zip(coords, series)][:: max(1, n // 120)],
            "hover": max(8.0, width / max(n - 1, 1) * max(1, n // 120))}


@bp.route("/")
@login_required
def overview():
    svc = _svc()
    st = state()
    today = st.book.today()
    period = request.args.get("period", "12m")
    if period not in PERIOD_LABELS:
        period = "12m"
    portfolio = request.args.get("portfolio") or None
    try:
        summary = _summary(svc)
        if not summary.get("exists"):
            return render_template("pp/overview.html", summary=summary, perf=None, period=period,
                                   PERIOD_LABELS=PERIOD_LABELS, status=svc.status(), settings=svc.settings())
        first = date.fromisoformat(summary["firstDate"]) if summary.get("firstDate") else None
        start, end = rp.nw_period_range(period, today, first, _parse_date(request.args.get("from")),
                                        _parse_date(request.args.get("to")))
        if period == "all" and first is not None:
            start = first
        perf = svc.client.performance(svc.cid, start, end, portfolio)
        holdings = svc.client.holdings(svc.cid, today)
    except PPCoreError as exc:
        return _pp_error(exc)
    series = [SimpleNamespace(day=date.fromisoformat(d), ttwror=Decimal(str(a)), value=Decimal(t),
                              invested=Decimal(inv)) for d, a, t, inv in perf.get("series", [])]
    categories = [(k, perf["categories"][k]) for k in CATEGORY_ORDER if k in perf.get("categories", {})]
    clearing = _clearing_balance(svc)
    return render_template(
        "pp/overview.html", summary=summary, perf=perf, period=period, start=start, end=end, portfolio=portfolio,
        PERIOD_LABELS=PERIOD_LABELS, chart=_chart(series, "ttwror", percent=True),
        value_chart=_chart(series, "value", second="invested"), series=series, categories=categories, CATEGORY_SIGNS=CATEGORY_SIGNS,
        holdings=holdings, status=svc.status(), settings=svc.settings(), clearing=clearing,
        conflicts=sum(1 for r in st.appdb.pp_records().values() if r["status"] == "conflict"))


def _clearing_balance(svc):
    """(account, balance) of the clearing account, if it exists already."""
    from ..ledger import balances

    st = state()
    idx = index()
    acc = idx.find(resolved(svc.settings(), idx)["clearing"])
    if acc is None:
        return None
    with st.book.connect() as conn:
        bal = balances(conn, st.book).get(acc.guid, ZERO)
    return {"account": acc, "balance": bal}


# ------------------------------------------------------------------------------------------ holdings

@bp.route("/holdings")
@login_required
def holdings():
    svc = _svc()
    st = state()
    day = _parse_date(request.args.get("date"), st.book.today())
    try:
        summary = _summary(svc)
        data = svc.client.holdings(svc.cid, day) if summary.get("exists") else None
    except PPCoreError as exc:
        return _pp_error(exc)
    links = _security_links(svc)
    total = _dec(data["total"]) if data else ZERO
    return render_template("pp/holdings.html", summary=summary, data=data, day=day, links=links, total=total)


def _security_links(svc) -> dict:
    """PP security uuid -> GnuCash commodity guid (for links to the depot report)."""
    st = state()
    out = {}
    for key, guid in st.appdb.pp_objects().items():
        if key.startswith("security:"):
            out[key.split(":", 1)[1]] = guid
    return out


# ------------------------------------------------------------------------------------------ transactions

@bp.route("/transactions")
@login_required
def transactions():
    svc = _svc()
    st = state()
    idx = index()
    try:
        summary = _summary(svc)
        export = svc.client.export(svc.cid, prices="none") if summary.get("exists") else None
    except PPCoreError as exc:
        return _pp_error(exc)
    rows = []
    settings = svc.settings()
    if export is not None:
        base = idx.root.commodity.mnemonic if idx.root.commodity else "EUR"
        plan = build_plan(export, base, sync_from=settings.start, realized_gains=settings.realized_gains,
                          capitalize_fees=settings.capitalize_fees, cash_accounts=settings.cash_accounts)
        records = st.appdb.pp_records()
        names = {p["uuid"]: p["name"] for p in export.get("portfolios", [])}
        names.update({a["uuid"]: a["name"] for a in export.get("accounts", [])})
        for b in plan.bookings:
            rec = records.get(b.key)
            if b.skip:
                status = "skipped"
            elif rec is None:
                status = "pending" if settings.enabled else "skipped"
            else:
                status = rec["status"]
            sec = plan.securities.get(b.security or "")
            rows.append({"b": b, "rec": rec, "status": status, "security": sec,
                         "where": names.get(b.portfolio or "") or names.get(b.account or "") or "",
                         "skip": SKIP_REASONS.get(b.skip, b.skip) if b.skip else ""})
    status_filter = request.args.get("status", "")
    q = (request.args.get("q") or "").strip().casefold()
    year = request.args.get("year", "")
    years = sorted({r["b"].day.year for r in rows}, reverse=True)
    if status_filter == "attention":
        rows = [r for r in rows if r["status"] in ("conflict", "gone", "edited") or r["b"].warnings]
    elif status_filter:
        rows = [r for r in rows if r["status"] == status_filter]
    if q:
        rows = [r for r in rows if q in r["b"].description.casefold() or q in (r["b"].notes or "").casefold()
                or (r["security"] and (q in (r["security"].isin or "").casefold()
                                       or q in (r["security"].wkn or "").casefold()))]
    if year.isdigit():
        rows = [r for r in rows if r["b"].day.year == int(year)]
    rows.sort(key=lambda r: (r["b"].day, r["b"].key), reverse=True)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return render_template("pp/transactions.html", summary=summary, rows=rows[:1000], total=len(rows),
                           status_filter=status_filter, q=request.args.get("q", ""), year=year, years=years,
                           counts=counts, KIND_LABELS=KIND_LABELS, STATUS_LABELS=STATUS_LABELS,
                           settings=settings, ids=st.appdb)


@bp.route("/transactions/<uuid>/delete", methods=["POST"])
@login_required
def transaction_delete(uuid):
    svc = _svc()
    try:
        svc.client.delete_transaction(svc.cid, uuid)
        flash(_("Buchung in Portfolio Performance gelöscht."), "success")
        svc.request_sync()
    except PPCoreError as exc:
        flash(str(exc), "danger")
    return redirect(request.form.get("next") or url_for("pp.transactions"))


@bp.route("/sync", methods=["POST"])
@login_required
def sync_now():
    svc = _svc()
    dry = request.form.get("dry") == "1"
    force_all = request.form.get("force") == "1"
    try:
        force = ()
        if force_all:
            force = tuple(state().appdb.pp_records().keys())
        result = svc.sync(force=force, actor=g.user["username"], dry_run=dry)
        if dry:
            flash(_("Probelauf: {a0} neu, {a1} zu ändern, {a2} zu löschen, {a3} Konflikte.", a0=result.created,
                    a1=result.updated, a2=result.deleted, a3=result.conflicts), "info")
        else:
            flash(_("Übernommen: {a0}", a0=result.summary()), "success" if result.ok else "warning")
    except WriteLockError as exc:
        flash(str(exc), "warning")
    except (PPCoreError, SyncError) as exc:
        flash(str(exc), "danger")
    return redirect(request.form.get("next") or url_for("pp.overview"))


@bp.route("/sync/resolve", methods=["POST"])
@login_required
def resolve():
    """Conflicts and transactions deleted in GnuCash: take PP's version, or detach the booking."""
    svc = _svc()
    st = state()
    key = request.form.get("key", "")
    action = request.form.get("action", "")
    rec = st.appdb.pp_records().get(key)
    if rec is None:
        abort(404)
    if action == "detach":
        st.appdb.pp_set_status(key, "detached", _("vom Benutzer abgekoppelt"))
        flash(_("Die Buchung wird nicht mehr mit Portfolio Performance abgeglichen."), "success")
    elif action == "attach":
        st.appdb.pp_set_status(key, "booked", None)
        flash(_("Die Buchung wird wieder abgeglichen."), "success")
    elif action == "force":
        try:
            result = svc.sync(force=(key,), actor=g.user["username"])
            flash(_("Stand aus Portfolio Performance übernommen: {a0}", a0=result.summary()), "success")
        except WriteLockError as exc:
            flash(str(exc), "warning")
        except (PPCoreError, SyncError) as exc:
            flash(str(exc), "danger")
    else:
        abort(400)
    return redirect(request.form.get("next") or url_for("pp.transactions", status="attention"))


# ------------------------------------------------------------------------------------------ PDF import

@bp.route("/import", methods=["GET", "POST"])
@login_required
def pdf_import():
    svc = _svc()
    result = None
    try:
        summary = _summary(svc)
        if request.method == "POST":
            if not summary.get("exists"):
                flash(_("Zuerst eine Portfolio-Performance-Datei hochladen oder anlegen."), "warning")
                return redirect(url_for("pp.settings"))
            files = [(f.filename or "dokument.pdf", f.read()) for f in request.files.getlist("files") if f]
            files = [(n, d) for n, d in files if d]
            if not files:
                flash(_("Bitte mindestens eine PDF-Datei auswählen."), "warning")
                return redirect(url_for("pp.pdf_import"))
            result = svc.client.import_pdfs(svc.cid, files, portfolio=request.form.get("portfolio") or None,
                                            account=request.form.get("account") or None,
                                            auto_feed=request.form.get("auto_feed") == "1")
            session["pp_import"] = result.get("session")
            _after_import(svc, result)
            return redirect(url_for("pp.pdf_import", session=result.get("session")))
        sid = request.args.get("session")
        if sid:
            try:
                result = svc.client.import_session(svc.cid, sid)
            except PPCoreError as exc:
                if exc.status != 404:
                    raise
                flash(_("Das Importergebnis ist nicht mehr verfügbar."), "info")
    except PPCoreError as exc:
        return _pp_error(exc)
    return render_template("pp/import.html", summary=summary, result=result, IMPORT_STATUS=IMPORT_STATUS)


def _after_import(svc, result: dict):
    n = int(result.get("imported") or 0)
    if result.get("needsTarget"):
        flash(_("Bitte Depot und Verrechnungskonto für die Bank wählen – dann wird übernommen."), "warning")
    elif n:
        flash(_("{a0} Positionen in Portfolio Performance übernommen.", a0=n), "success")
        if svc.enabled:
            svc.request_sync()
            flash(_("Die Buchungen werden im Hintergrund ins GnuCash-Buch übernommen."), "info")
    else:
        flash(_("Nichts übernommen – siehe Meldungen unten."), "warning")


@bp.route("/import/<sid>/apply", methods=["POST"])
@login_required
def pdf_import_apply(sid):
    svc = _svc()
    try:
        result = svc.client.import_apply(svc.cid, sid, force=request.form.getlist("force"),
                                         portfolio=request.form.get("portfolio") or None,
                                         account=request.form.get("account") or None,
                                         extractor=request.form.get("extractor") or None)
        _after_import(svc, result)
    except PPCoreError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("pp.pdf_import", session=sid))


# ------------------------------------------------------------------------------------------ securities & prices

@bp.route("/securities")
@login_required
def securities():
    svc = _svc()
    try:
        summary = _summary(svc)
        export = svc.client.export(svc.cid, prices="none") if summary.get("exists") else None
        job = svc.client.quotes_status(svc.cid) if summary.get("exists") else None
        feeds = {f["id"]: f["name"] for f in svc.client.feeds()}
    except PPCoreError as exc:
        return _pp_error(exc)
    results = {r["uuid"]: r for r in (job or {}).get("securities") or []}
    held = set()
    if export:
        for t in export.get("transactions", []):
            if t.get("security"):
                held.add(t["security"])
    rows = []
    for s in (export or {}).get("securities", []):
        rows.append({"s": s, "feed": feeds.get(s.get("feed") or "", s.get("feed") or ""),
                     "result": results.get(s["uuid"]), "used": s["uuid"] in held})
    show_all = request.args.get("all") == "1"
    if not show_all:
        rows = [r for r in rows if not r["s"].get("retired") and not r["s"].get("exchangeRate")]
    rows.sort(key=lambda r: (not r["used"], r["s"]["name"].casefold()))
    return render_template("pp/securities.html", summary=summary, rows=rows, job=job, show_all=show_all)


@bp.route("/securities/<uuid>", methods=["GET", "POST"])
@login_required
def security(uuid):
    svc = _svc()
    try:
        if request.method == "POST":
            fields = {}
            for name in ("feed", "ticker", "feedUrl", "latestFeed"):
                if name in request.form:
                    fields[name] = request.form.get(name, "").strip()
            svc.client.update_security(svc.cid, uuid, fields)
            flash(_("Kursquelle gespeichert."), "success")
            if request.form.get("update") == "1":
                svc.client.quotes_start(svc.cid, securities=[uuid])
                svc.request_sync(after_quotes=True)
                flash(_("Kurse werden geladen …"), "info")
            else:
                svc.request_copy()
            return redirect(url_for("pp.security", uuid=uuid))
        export = svc.client.export(svc.cid, prices="none")
        sec = next((s for s in export.get("securities", []) if s["uuid"] == uuid), None)
        if sec is None:
            abort(404)
        feeds = svc.client.feeds()
        q = None if pp_demo() else request.args.get("q")  # no searches from the shared demo
        results = None
        if q is not None:
            q = q.strip() or sec.get("isin") or sec.get("name")
            results = svc.client.search(svc.cid, q) if len(q) >= 2 else []
        job = svc.client.quotes_status(svc.cid)
    except PPCoreError as exc:
        if request.method == "POST":
            flash(str(exc), "danger")
            return redirect(url_for("pp.security", uuid=uuid))
        return _pp_error(exc)
    result = next((r for r in (job.get("securities") or []) if r["uuid"] == uuid), None)
    return render_template("pp/security.html", sec=sec, feeds=feeds, results=results,
                           q=q if q is not None else (sec.get("isin") or sec.get("name")), result=result)


@bp.route("/quotes", methods=["POST"])
@login_required
def quotes():
    svc = _svc()
    try:
        job = svc.client.quotes_start(svc.cid)
        svc.request_sync(after_quotes=True)
        if job.get("state") != "running":
            msg = _("Kurse aktualisiert.")
        elif svc.enabled:
            msg = _("Kursaktualisierung läuft – das kann einige Minuten dauern. Danach werden die Kurse ins Buch übernommen.")  # noqa: E501
        else:
            msg = _("Kursaktualisierung läuft – das kann einige Minuten dauern.")
        flash(msg, "info")
    except PPCoreError as exc:
        flash(str(exc), "danger")
    return redirect(request.form.get("next") or url_for("pp.securities"))


# ------------------------------------------------------------------------------------------ settings & file

def _account_choices(idx, types, currency_guid=None):
    out = []
    for a in idx.walk():
        if a.type in types and (currency_guid is None or a.commodity_guid == currency_guid):
            out.append(a)
    return out


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    svc = _svc()
    st = state()
    idx = index()
    current = svc.settings()
    if request.method == "POST":
        new = PPSettings.from_json(current.to_json())
        f = request.form
        new.enabled = f.get("enabled") == "1"
        new.securities_root = f.get("securities_root", "").strip()
        new.accounts = {role: f.get(f"role_{role}", "").strip() for role in ROLE_TYPES
                        if f.get(f"role_{role}", "").strip()}
        new.cash_accounts = {}
        for key, value in f.items():
            if key.startswith("cash_") and value.strip():
                new.cash_accounts[key[5:]] = value.strip()
        new.bank_accounts = [v for v in f.getlist("bank_accounts") if v]
        sync_from = _parse_date(f.get("sync_from"))
        new.sync_from = sync_from.isoformat() if sync_from else ""
        new.realized_gains = f.get("realized_gains") == "1"
        new.capitalize_fees = f.get("capitalize_fees") == "1"
        new.prices = f.get("prices") == "1"
        try:
            new.price_days = max(0, min(3650, int(f.get("price_days") or 400)))
        except ValueError:
            new.price_days = 400
        new.namespace = (f.get("namespace") or "").strip()[:100] or "Portfolio Performance"
        problems = _check_settings(new, idx)
        if problems:
            for p in problems:
                flash(p, "danger")
        else:
            registry().system.set_pp_settings(st.id, new)
            flash(_("Einstellungen gespeichert."), "success")
            if new.enabled:
                fresh = registry().context(st.id)
                if fresh is not None and fresh.pp is not None:
                    fresh.pp.request_sync()
                    flash(_("Die Übernahme ins GnuCash-Buch läuft im Hintergrund."), "info")
        return redirect(url_for("pp.settings"))
    try:
        summary = _summary(svc)
        health = svc.client.health()
    except PPCoreError as exc:
        summary, health = {"exists": False, "error": str(exc)}, None
    base = idx.root.commodity
    names = resolved(current, idx)
    roles = []
    for role, typ in ROLE_TYPES.items():
        configured = current.accounts.get(role, "")
        roles.append({"role": role, "label": ROLE_LABELS[role], "type": typ, "value": configured,
                      "default": default_name(idx, role), "effective": names[role],
                      "exists": idx.find(names[role]) is not None,
                      "choices": _account_choices(idx, (typ,) if typ != "ASSET" else ("ASSET", "BANK"),
                                                  base.guid if base else None)})
    bank_choices = _account_choices(idx, ("BANK", "ASSET", "CASH"), base.guid if base else None)
    asset_choices = [a for a in _account_choices(idx, ("ASSET", "STOCK", "MUTUAL"), base.guid if base else None)]
    return render_template(
        "pp/settings.html", summary=summary, health=health, settings=current, roles=roles,
        bank_choices=bank_choices, asset_choices=asset_choices, names=names, runs=st.appdb.pp_runs(15),
        status=svc.status(), default_root=default_name(idx, "securities_root"))


def _check_settings(s: PPSettings, idx) -> list[str]:
    problems = []
    base = idx.root.commodity
    for role, ref in s.accounts.items():
        acc = idx.find(ref)
        if acc is None:
            continue  # created on the first run
        if acc.placeholder:
            problems.append(_("{a0} ist ein Platzhalterkonto und nimmt keine Buchungen an.", a0=acc.full_name))
        if base is not None and acc.commodity_guid != base.guid:
            problems.append(_("{a0} wird nicht in {a1} geführt.", a0=acc.full_name, a1=base.mnemonic))
    for ref in [*s.cash_accounts.values(), *s.bank_accounts]:
        acc = idx.find(ref)
        if acc is None:
            problems.append(_("Konto nicht gefunden: {a0}", a0=ref))
        elif acc.placeholder:
            problems.append(_("{a0} ist ein Platzhalterkonto und nimmt keine Buchungen an.", a0=acc.full_name))
    clearing = resolved(s, idx)["clearing"]
    for ref in s.bank_accounts:
        acc = idx.find(ref)
        if acc is not None and acc.full_name == clearing:
            problems.append(_("Das Zwischenkonto kann nicht zugleich Bankkonto für den Import sein."))
    return problems


@bp.route("/file", methods=["GET", "POST"])
@login_required
def pp_file():
    svc = _svc()
    if request.method == "GET":
        try:
            data, name = svc.client.download(svc.cid)
        except PPCoreError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("pp.settings"))
        original = (_summary(svc).get("originalName") or name).rsplit("/", 1)[-1] or name
        ext = name.rsplit(".", 1)[-1]
        if not original.lower().endswith("." + ext):
            original = f"{original.rsplit('.', 1)[0]}.{ext}"
        return Response(data, mimetype="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{original}"'})
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash(_("Bitte eine Portfolio-Performance-Datei auswählen."), "warning")
        return redirect(url_for("pp.settings"))
    try:
        info = svc.client.upload(svc.cid, upload.read(), upload.filename)
        flash(_("PP-Datei übernommen: {a0} Wertpapiere, {a1} Buchungen.", a0=info.get("securities"),
                a1=info.get("transactions")), "success")
        svc.request_sync()
    except PPCoreError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("pp.settings"))


@bp.route("/file/create", methods=["POST"])
@login_required
def pp_file_create():
    svc = _svc()
    idx = index()
    try:
        svc.client.create(svc.cid, currency=idx.root.commodity.mnemonic if idx.root.commodity else "EUR",
                          portfolio=request.form.get("portfolio") or "Depot",
                          account=request.form.get("account") or "Verrechnungskonto")
        svc.request_copy()
        flash(_("Neue, leere Portfolio-Performance-Datei angelegt."), "success")
    except PPCoreError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("pp.settings"))


@bp.route("/status.json")
@login_required
def status_json():
    svc = _svc()
    st = svc.status()
    return jsonify({"busy": st["busy"], "waiting": st["waiting"], "error": st["error"],
                    "last_sync": st["last_sync"]})
