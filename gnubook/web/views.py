"""HTML views: dashboard, accounts, registers, transaction editor, checkpoints, imports, settings."""
from __future__ import annotations

from ..i18n import gettext as _

import logging
import re
from datetime import date
from decimal import Decimal

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template, request, session, url_for)

from .. import checkpoints as cps
from ..book import ASSET_TYPES, LIABILITY_TYPES, BookError, WriteLockError, latest_prices
from ..importer import is_special_account
from ..ledger import (MULTI, annotate_rows, balances, description_suggestions, latest_by_description,
                      load_transaction, monthly_income_expense, recent_transactions, register_rows,
                      search_transactions, tree_totals)
from ..money import AmountError, ZERO, fmt, parse_amount
from ..writer import (ConflictError, SplitInput, TxInput, ValidationError, create_transaction,
                      delete_transaction, update_transaction)
from . import registry, state
from .auth import login_required

log = logging.getLogger("gnubook.web")
bp = Blueprint("views", __name__)

PAGE_SIZE = 100
COLUMN_LABELS = {
    "BANK": ("Einzahlung", "Auszahlung"), "ASSET": ("Zunahme", "Abnahme"), "CASH": ("Einnahme", "Ausgabe"),
    "STOCK": ("Kauf", "Verkauf"), "MUTUAL": ("Kauf", "Verkauf"), "RECEIVABLE": ("Rechnung", "Zahlung"),
    "CREDIT": ("Zahlung", "Belastung"), "LIABILITY": ("Abnahme", "Zunahme"), "PAYABLE": ("Zahlung", "Rechnung"),
    "INCOME": ("Belastung", "Ertrag"), "EXPENSE": ("Aufwand", "Erstattung"), "EQUITY": ("Abnahme", "Zunahme"),
}
STATUS_LABELS = {"created": "neu angelegt", "matched": "mit vorhandener Buchung verknüpft",
                 "possible_duplicate": "mögliches Duplikat"}
SOURCE_LABELS = {"history": "Historie (IBAN/Name)", "bayes": "GnuCash-Importzuordnung", "fallback": "Auffangkonto",
                 "own": "eigenes Konto", "transit": "Geldtransit", "checkpoint": "Saldo-Zeile (0 €)",
                 "match": "vorhandene Buchung"}


# ------------------------------------------------------------------------------------------ helpers

def index():
    if "index" not in g:
        g.index = state().book.load_accounts()
    return g.index


def lock_info():
    if "lock_info" not in g:
        try:
            g.lock_info = state().book.foreign_lock_holders()
        except Exception as exc:  # DB unreachable
            log.warning("lock check failed: %s", exc)
            g.lock_info = None
    return g.lock_info


@bp.app_context_processor
def inject_lock():
    def _lock():
        return lock_info() if g.get("ctx") is not None else []
    return {"book_lock": _lock}


def _parse_date(value: str | None, default=None):
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", value.strip())
        if m:
            y = int(m.group(3))
            y = y + 2000 if y < 100 else y
            try:
                return date(y, int(m.group(2)), int(m.group(1)))
            except ValueError:
                return default
    return default


def _account_or_404(guid: str):
    acc = index().get(guid)
    if acc is None:
        abort(404)
    return acc


def _checkpoint_snapshot(account_guids) -> dict:
    st = state()
    guids = {g_ for g_ in account_guids if index().get(g_) is not None and index().get(g_).is_balance_sheet}
    if not guids:
        return {}
    with st.book.connect() as conn:
        checks = cps.evaluate(conn, st.book, index(), guids, st.appdb.acceptances())
    return {(cp.account_guid, cp.tx_guid): cp for ch in checks.values() for cp in ch.checkpoints}


def _report_checkpoint_changes(before: dict, after: dict):
    newly_bad, fixed = [], []
    for key, cp in after.items():
        old = before.get(key)
        if cp.status == "open" and (old is None or old.status != "open" or old.diff_stand != cp.diff_stand
                                    or old.diff_end != cp.diff_end):
            newly_bad.append(cp)
        elif cp.status != "open" and old is not None and old.status == "open":
            fixed.append(cp)
    idx = index()
    for cp in newly_bad[:5]:
        acc = idx.get(cp.account_guid)
        flash(_("Saldo-Prüfpunkt weicht ab: {a0}, Stand {a1:%d.%m.%Y} – Buch {a2} / Bank {a3} (Differenz {a4}", a0=acc.name if acc else '?', a1=cp.stand_date, a2=fmt(cp.book_stand), a3=fmt(cp.bank_stand), a4=fmt(cp.diff_stand))
              + (_(", Endsaldo-Differenz {d}", d=fmt(cp.diff_end)) if cp.diff_end else "") + ").", "warning")
    if len(newly_bad) > 5:
        flash(_("… und {a0} weitere abweichende Prüfpunkte.", a0=len(newly_bad) - 5), "warning")
    for cp in fixed[:5]:
        acc = idx.get(cp.account_guid)
        flash(_("Saldo-Prüfpunkt stimmt jetzt: {a0}, Stand {a1:%d.%m.%Y}.", a0=acc.name if acc else '?', a1=cp.stand_date),
              "success")


def _write_error(exc: Exception):
    if isinstance(exc, ValidationError):
        for m in exc.messages:
            flash(m, "danger")
    elif isinstance(exc, (ConflictError, WriteLockError, BookError)):
        flash(str(exc), "danger")
    else:
        log.exception("write failed")
        flash(f"Speichern fehlgeschlagen: {exc}", "danger")


def _import_rows(records):
    """Import records with the current counter account and whether they still need attention."""
    from sqlalchemy import text

    st = state()
    idx = index()
    fallback = st.importer.fallback_account(idx, idx.root.commodity_guid) if idx.root.commodity_guid else None
    rows = []
    with st.book.connect() as conn:
        for r in records:
            accs = []
            if r["tx_guid"]:
                accs = [a for (a,) in conn.execute(text("SELECT account_guid FROM splits WHERE tx_guid = :t"),
                                                   {"t": r["tx_guid"]})]
            others = [idx.get(a) for a in accs if a != r["account_guid"]]
            on_fallback = fallback is not None and fallback.guid in accs
            attention = bool(accs) and not r["reviewed"] and (r["status"] == "possible_duplicate" or on_fallback)
            rows.append({"r": r, "account": idx.get(r["account_guid"]), "exists": bool(accs),
                         "counter": ", ".join(a.full_name for a in others if a) if accs else None,
                         "on_fallback": on_fallback, "attention": attention})
    return rows, fallback


def attention_count() -> int:
    rows, _ = _import_rows(state().appdb.imports(limit=500, only_open=True))
    return sum(1 for row in rows if row["attention"])


# ------------------------------------------------------------------------------------------ dashboard

@bp.route("/")
@login_required
def dashboard():
    st = state()
    idx = index()
    today = st.book.today()
    with st.book.connect() as conn:
        own = balances(conn, st.book, upto=today)
        prices = latest_prices(conn)
        totals = tree_totals(idx, own, prices)
        recent = recent_transactions(conn, st.book, idx, 12)
        months = monthly_income_expense(conn, st.book, idx, 12)
        checks = cps.evaluate(conn, st.book, idx, None, st.appdb.acceptances())
    base = idx.root.commodity
    from ..book import convert

    assets = liabilities = ZERO
    for top in idx.top_level():
        t = totals.get(top.guid)
        if t is None:
            continue
        v = convert(t.value, top.commodity, base, prices)
        if v is None:
            continue
        if top.type in ASSET_TYPES:
            assets += v
        elif top.type in LIABILITY_TYPES:
            liabilities += v
    fallback = st.importer.fallback_account(idx, idx.root.commodity_guid) if idx.root.commodity_guid else None
    bank_accounts = []
    for a in idx.walk():
        if a.hidden or a.placeholder or not (a.type == "BANK" or a.guid in checks):
            continue
        a.is_imbalance = is_special_account(a) or (fallback is not None and a.guid == fallback.guid)
        if a.is_imbalance and not (totals.get(a.guid) and totals[a.guid].value):
            continue  # only shown when something landed there
        bank_accounts.append(a)
    month = months[-1] if months else ("", ZERO, ZERO)
    peak = max([max(i, e) for _, i, e in months] + [Decimal(1)])
    return render_template(
        "dashboard.html", assets=assets, liabilities=liabilities, base=base, totals=totals,
        bank_accounts=bank_accounts, recent=recent, months=months, peak=peak, month=month,
        checks=checks, cp_summary=cps.summary(checks), open_imports=attention_count(),
        last_import=st.appdb.last_import_at())


# ------------------------------------------------------------------------------------------ accounts

@bp.route("/accounts")
@login_required
def accounts():
    st = state()
    idx = index()
    show_hidden = request.args.get("hidden") == "1"
    with st.book.connect() as conn:
        own = balances(conn, st.book)
        prices = latest_prices(conn)
    totals = tree_totals(idx, own, prices)
    rows = []
    for acc in idx.walk():
        if acc.hidden and not show_hidden:
            continue
        hidden_parent = False
        p = idx.get(acc.parent_guid)
        while p is not None:
            if p.hidden:
                hidden_parent = True
                break
            p = idx.get(p.parent_guid)
        if hidden_parent and not show_hidden:
            continue
        rows.append(acc)
    return render_template("accounts/index.html", rows=rows, totals=totals, own=own, show_hidden=show_hidden)


@bp.route("/accounts/<guid>")
@login_required
def register(guid):
    st = state()
    acc = _account_or_404(guid)
    q = (request.args.get("q") or "").strip()
    d_from = _parse_date(request.args.get("from"))
    d_to = _parse_date(request.args.get("to"))
    include_children = request.args.get("sub") == "1" or (acc.placeholder and request.args.get("sub") != "0")
    with st.book.connect() as conn:
        rows = register_rows(conn, st.book, index(), acc, include_children=include_children)
        balance = rows[-1].balance if rows else ZERO
        today = st.book.today()
        present = ZERO
        for r in rows:
            if r.day <= today:
                present = r.balance
        selected = list(reversed(rows))
        if d_from:
            selected = [r for r in selected if r.day >= d_from]
        if d_to:
            selected = [r for r in selected if r.day <= d_to]
        if q:
            ql = q.casefold()
            try:
                amount = parse_amount(q)
            except AmountError:
                amount = None
            selected = [r for r in selected if ql in r.description.casefold() or ql in r.memo.casefold()
                        or ql in r.num.casefold() or (amount is not None and abs(r.amount) == abs(amount))]
        try:
            page = max(1, int(request.args.get("page", 1) or 1))
        except ValueError:
            page = 1
        pages = max(1, (len(selected) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        shown = selected[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        annotate_rows(conn, index(), shown)
        checks = cps.evaluate(conn, st.book, index(), {acc.guid}, st.appdb.acceptances()) \
            if acc.is_balance_sheet else {}
    importable = {a.guid for a in st.importer.importable_accounts(index())}
    api_id = st.appdb.account_id(acc.guid) if acc.guid in importable else None
    cp_by_tx = {cp.tx_guid: cp for ch in checks.values() for cp in ch.checkpoints}
    labels = tuple(_(x) for x in COLUMN_LABELS.get(acc.type, ("Soll", "Haben")))
    return render_template("accounts/register.html", acc=acc, rows=shown, total_rows=len(selected), page=page,
                           pages=pages, balance=balance, present=present, q=q, d_from=d_from, d_to=d_to,
                           labels=labels, include_children=include_children, api_id=api_id,
                           check=checks.get(acc.guid), cp_by_tx=cp_by_tx, MULTI=MULTI)


# ------------------------------------------------------------------------------------------ transactions

@bp.route("/transactions/<guid>")
@login_required
def transaction(guid):
    st = state()
    with st.book.connect() as conn:
        tx = load_transaction(conn, st.book, index(), guid)
    if tx is None:
        abort(404)
    records = st.appdb.imports_for_tx(guid)
    import json as _json

    payloads = []
    for r in records:
        try:
            payloads.append((r, _json.loads(r["payload"])))
        except ValueError:
            payloads.append((r, {}))
    back = request.args.get("back")
    return render_template("transactions/show.html", tx=tx, records=payloads, tx_id=st.appdb.tx_id(guid),
                           back=back if back and back.startswith("/") and not back.startswith("//") else None,
                           STATUS_LABELS=STATUS_LABELS, SOURCE_LABELS=SOURCE_LABELS)


def _form_rows_from_request():
    """Collect split rows from the posted form (rows may be numbered with gaps)."""
    form = request.form
    ids = sorted({int(m.group(1)) for k in form for m in [re.fullmatch(r"split-(\d+)-account", k)] if m})
    rows = []
    for i in ids:
        get = lambda f: (form.get(f"split-{i}-{f}") or "").strip()  # noqa: E731
        rows.append({"guid": get("guid") or None, "account": get("account"), "memo": get("memo"),
                     "debit": get("debit"), "credit": get("credit"), "action": get("action"),
                     "reconcile": "c" if form.get(f"split-{i}-reconcile") else "n",
                     "reconcile_orig": get("reconcile_orig")})
    return rows


def _draft_from_rows(rows, errors) -> list[SplitInput]:
    splits = []
    for n, r in enumerate(rows, 1):
        if not r["account"] and not r["debit"] and not r["credit"] and not r["memo"]:
            continue
        if not r["account"]:
            errors.append(_("Zeile {a0}: Konto fehlt.", a0=n))
            continue
        try:
            debit = parse_amount(r["debit"]) or ZERO
            credit = parse_amount(r["credit"]) or ZERO
        except AmountError as exc:
            errors.append(_("Zeile {a0}: {a1}", a0=n, a1=exc))
            continue
        reconcile = "y" if r["reconcile_orig"] == "y" else r["reconcile"]
        splits.append(SplitInput(r["account"], debit - credit, memo=r["memo"], action=r["action"],
                                 reconcile=reconcile, guid=r["guid"]))
    return splits


def _rows_for_form(tx=None, context_acc=None, copy=False):
    rows = []
    if tx is not None:
        for s in tx.splits:
            rows.append({"guid": None if copy else s.guid, "account": s.account_guid, "memo": s.memo,
                         "action": s.action, "debit": fmt(s.value) if s.value > 0 else "",
                         "credit": fmt(-s.value) if s.value < 0 else "",
                         "reconcile": "n" if copy else s.reconcile, "reconcile_orig": "" if copy else s.reconcile})
        if context_acc is not None:  # context account first, as in the register
            rows.sort(key=lambda r: r["account"] != context_acc.guid)
    else:
        rows.append({"guid": None, "account": context_acc.guid if context_acc else "", "memo": "", "action": "",
                     "debit": "", "credit": "", "reconcile": "n", "reconcile_orig": ""})
        rows.append({"guid": None, "account": "", "memo": "", "action": "", "debit": "", "credit": "",
                     "reconcile": "n", "reconcile_orig": ""})
    return rows


def _account_options(currency_guid=None, extra=()):
    idx = index()
    accs = [a for a in idx.postable(currency_guid) if not a.hidden or a.guid in extra]
    present = {a.guid for a in accs}
    for g_ in extra:
        a = idx.get(g_)
        if a is not None and g_ not in present:
            accs.append(a)
    return accs


def _safe_back(value):
    return value if value and value.startswith("/") and not value.startswith("//") else None


def _render_form(mode, form, rows, context_acc=None, tx=None, status=200):
    currency_guid = tx.currency_guid if tx is not None else (context_acc.commodity_guid if context_acc else
                                                             (index().root.commodity_guid))
    used = [r["account"] for r in rows if r["account"]]
    labels = tuple(_(x) for x in (COLUMN_LABELS.get(context_acc.type, ("Soll", "Haben")) if context_acc
                                  else ("Soll", "Haben")))
    return render_template("transactions/form.html", mode=mode, form=form, rows=rows, tx=tx,
                           context_acc=context_acc, accounts=_account_options(currency_guid, used),
                           currency=index().commodities.get(currency_guid), labels=labels), status


@bp.route("/transactions/new", methods=["GET", "POST"])
@login_required
def transaction_new():
    st = state()
    context_acc = index().get(request.values.get("account") or "")
    back = _safe_back(request.values.get("back"))
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in ("date", "num", "description", "notes")}
        form["back"] = back or ""
        rows = _form_rows_from_request()
        errors = []
        day = _parse_date(form["date"])
        if day is None:
            errors.append(_("Bitte ein gültiges Datum angeben."))
        splits = _draft_from_rows(rows, errors)
        if errors:
            for e in errors:
                flash(e, "danger")
            return _render_form("new", form, rows or _rows_for_form(None, context_acc), context_acc, status=422)
        draft = TxInput(day=day, description=form["description"].strip(), splits=splits, num=form["num"].strip(),
                        notes=form["notes"].strip())
        before = _checkpoint_snapshot([s.account_guid for s in splits])
        try:
            guid = create_transaction(st.book, index(), draft)
        except Exception as exc:  # noqa: BLE001
            _write_error(exc)
            return _render_form("new", form, rows, context_acc, status=422)
        st.appdb.audit(session.get("user", "?"), "create", guid, draft.description)
        g.pop("index", None)
        _report_checkpoint_changes(before, _checkpoint_snapshot([s.account_guid for s in splits]))
        flash(_("Buchung gespeichert."), "success")
        if request.form.get("again"):
            return redirect(url_for("views.transaction_new", account=context_acc.guid if context_acc else None,
                                    back=back, date=day.isoformat()))
        return redirect(back or url_for("views.transaction", guid=guid))
    # GET: new, optionally as a copy of an existing transaction
    copy_of = request.args.get("copy")
    tx = None
    if copy_of:
        with st.book.connect() as conn:
            tx = load_transaction(conn, st.book, index(), copy_of)
        if tx is None or not tx.editable:
            abort(404)
    day = _parse_date(request.args.get("date"), st.book.today())
    form = {"date": day.isoformat(), "num": "", "description": tx.description if tx else "",
            "notes": tx.notes if tx else "", "back": back or ""}
    return _render_form("new", form, _rows_for_form(tx, context_acc, copy=True), context_acc)


@bp.route("/transactions/<guid>/edit", methods=["GET", "POST"])
@login_required
def transaction_edit(guid):
    st = state()
    with st.book.connect() as conn:
        tx = load_transaction(conn, st.book, index(), guid)
    if tx is None:
        abort(404)
    context_acc = index().get(request.values.get("account") or "")
    back = _safe_back(request.values.get("back"))
    if not tx.editable:
        flash(_("Diese Buchung kann nur in GnuCash Desktop bearbeitet werden: ") + ", ".join(tx.readonly_reasons),
              "warning")
        return redirect(url_for("views.transaction", guid=guid))
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in ("date", "num", "description", "notes", "fingerprint")}
        form["back"] = back or ""
        rows = _form_rows_from_request()
        errors = []
        day = _parse_date(form["date"])
        if day is None:
            errors.append(_("Bitte ein gültiges Datum angeben."))
        splits = _draft_from_rows(rows, errors)
        if errors:
            for e in errors:
                flash(e, "danger")
            return _render_form("edit", form, rows, context_acc, tx, status=422)
        draft = TxInput(day=day, description=form["description"].strip(), splits=splits, num=form["num"].strip(),
                        notes=form["notes"].strip())
        touched = {s.account_guid for s in tx.splits} | {s.account_guid for s in splits}
        before = _checkpoint_snapshot(touched)
        try:
            update_transaction(st.book, index(), guid, draft, form["fingerprint"])
        except Exception as exc:  # noqa: BLE001
            _write_error(exc)
            return _render_form("edit", form, rows, context_acc, tx, status=422)
        st.appdb.audit(session.get("user", "?"), "update", guid, draft.description)
        g.pop("index", None)
        _report_checkpoint_changes(before, _checkpoint_snapshot(touched))
        flash(_("Änderungen gespeichert."), "success")
        return redirect(back or url_for("views.transaction", guid=guid))
    form = {"date": tx.day.isoformat(), "num": tx.num, "description": tx.description, "notes": tx.notes,
            "fingerprint": tx.fingerprint, "back": back or ""}
    return _render_form("edit", form, _rows_for_form(tx, context_acc), context_acc, tx)


@bp.route("/transactions/<guid>/delete", methods=["POST"])
@login_required
def transaction_delete(guid):
    st = state()
    with st.book.connect() as conn:
        tx = load_transaction(conn, st.book, index(), guid)
    if tx is None:
        flash(_("Die Buchung existiert nicht mehr."), "warning")
        return redirect(url_for("views.dashboard"))
    touched = {s.account_guid for s in tx.splits}
    before = _checkpoint_snapshot(touched)
    try:
        delete_transaction(st.book, index(), guid, request.form.get("fingerprint", ""))
    except Exception as exc:  # noqa: BLE001
        _write_error(exc)
        return redirect(url_for("views.transaction", guid=guid))
    st.appdb.audit(session.get("user", "?"), "delete", guid, tx.description)
    g.pop("index", None)
    _report_checkpoint_changes(before, _checkpoint_snapshot(touched))
    flash(_("Buchung „{a0}“ gelöscht.", a0=tx.description), "success")
    back = _safe_back(request.form.get("back"))
    return redirect(back or url_for("views.dashboard"))


@bp.route("/transactions/suggest")
@login_required
def suggest():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    with state().book.connect() as conn:
        return jsonify(description_suggestions(conn, q))


@bp.route("/transactions/template")
@login_required
def template():
    """Splits of the latest transaction with this description (GnuCash 'quickfill')."""
    st = state()
    desc = request.args.get("description") or ""
    context = request.args.get("account") or ""
    with st.book.connect() as conn:
        guid = latest_by_description(conn, desc)
        tx = load_transaction(conn, st.book, index(), guid) if guid else None
    if tx is None or not tx.editable:
        return jsonify(None)
    splits = [{"account": s.account_guid, "memo": s.memo,
               "debit": fmt(s.value) if s.value > 0 else "", "credit": fmt(-s.value) if s.value < 0 else ""}
              for s in tx.splits]
    splits.sort(key=lambda s: s["account"] != context)
    return jsonify({"notes": tx.notes, "splits": splits})


@bp.route("/search")
@login_required
def search():
    st = state()
    q = (request.args.get("q") or "").strip()
    d_from = _parse_date(request.args.get("from"))
    d_to = _parse_date(request.args.get("to"))
    results = []
    if q:
        with st.book.connect() as conn:
            results = search_transactions(conn, st.book, index(), q, 300, d_from, d_to)
    return render_template("transactions/search.html", q=q, results=results, d_from=d_from, d_to=d_to)


# ------------------------------------------------------------------------------------------ checkpoints

@bp.route("/checkpoints")
@login_required
def checkpoints():
    st = state()
    only_bad = request.args.get("all") != "1"
    with st.book.connect() as conn:
        checks = cps.evaluate(conn, st.book, index(), None, st.appdb.acceptances())
    for ch in checks.values():
        acc = index().get(ch.account_guid)
        ch.account_name = acc.full_name if acc else ch.account_guid
    ordered = sorted(checks.values(), key=lambda c: c.account_name)
    return render_template("checkpoints/index.html", checks=ordered, summary=cps.summary(checks),
                           only_bad=only_bad)


@bp.route("/checkpoints/accept", methods=["POST"])
@login_required
def checkpoint_accept():
    st = state()
    acc_guid = request.form.get("account", "")
    tx_guid = request.form.get("tx", "")
    with st.book.connect() as conn:
        checks = cps.evaluate(conn, st.book, index(), {acc_guid}, st.appdb.acceptances())
    cp = next((c for ch in checks.values() for c in ch.checkpoints if c.tx_guid == tx_guid), None)
    if cp is None or cp.ok:
        flash(_("Prüfpunkt nicht gefunden oder bereits stimmig."), "warning")
    else:
        st.appdb.accept(acc_guid, tx_guid, cp.diff_stand, cp.diff_end, request.form.get("note", "").strip())
        st.appdb.audit(session.get("user", "?"), "checkpoint-accept", tx_guid,
                       f"{cp.stand_date} Differenz {cp.diff_stand}/{cp.diff_end}")
        flash(_("Abweichung zum {a0:%d.%m.%Y} akzeptiert. Ändert sich die Differenz, wird sie wieder gemeldet.", a0=cp.stand_date), "success")
    return redirect(request.form.get("back") if _safe_back(request.form.get("back"))
                    else url_for("views.checkpoints"))


@bp.route("/checkpoints/unaccept", methods=["POST"])
@login_required
def checkpoint_unaccept():
    st = state()
    st.appdb.unaccept(request.form.get("account", ""), request.form.get("tx", ""))
    flash(_("Akzeptanz zurückgenommen."), "info")
    return redirect(request.form.get("back") if _safe_back(request.form.get("back"))
                    else url_for("views.checkpoints"))


# ------------------------------------------------------------------------------------------ imports

@bp.route("/imports")
@login_required
def imports():
    st = state()
    show_all = request.args.get("all") == "1"
    rows, fallback = _import_rows(st.appdb.imports(limit=500, only_open=not show_all))
    if not show_all:
        rows = [row for row in rows if row["attention"]]
    return render_template("imports/index.html", rows=rows, show_all=show_all, STATUS_LABELS=STATUS_LABELS,
                           SOURCE_LABELS=SOURCE_LABELS, fallback=fallback)


@bp.route("/imports/review", methods=["POST"])
@login_required
def imports_review():
    st = state()
    ids = request.form.getlist("id")
    st.appdb.mark_reviewed(ids, request.form.get("undo") != "1")
    flash(_("{a0} Import(e) als geprüft markiert.", a0=len(ids)) if request.form.get("undo") != "1"
          else "Markierung entfernt.", "success")
    return redirect(url_for("views.imports", all=request.form.get("all") or None))


# ------------------------------------------------------------------------------------------ settings

@bp.route("/settings")
@login_required
def settings():
    st = state()
    idx = index()
    try:
        schema = st.book.schema_info()
    except Exception as exc:  # noqa: BLE001
        schema = {"gnucash": "?", "supported": False, "error": str(exc)}
    importable = st.importer.importable_accounts(idx)
    ids = st.appdb.account_ids([a.guid for a in importable])
    url = st.book.engine.url
    safe_url = url.render_as_string(hide_password=True) if hasattr(url, "render_as_string") else str(url)
    return render_template("settings.html", schema=schema, importable=importable, ids=ids, safe_url=safe_url,
                           cfg=st.cfg, fallback=st.importer.fallback_account(idx, idx.root.commodity_guid),
                           audit=st.appdb.audit_log(60), all_locks=st.book.lock_holders(),
                           backup=st.backup, tokens=registry().system.tokens(st.id),
                           new_token=session.pop("new_token", None), **_nextcloud_view(st))


# ------------------------------------------------------------------------------------------ Nextcloud

def _nextcloud_view(st) -> dict:
    reg = registry()
    if not reg.cfg.nextcloud.enabled:
        return {"nc_enabled": False}
    uid = g.user["id"]
    target = reg.system.nextcloud_target(uid, st.id)
    account = reg.system.nextcloud_account(uid)
    status = st.backup.remote_status.get(uid) if st.backup is not None else None
    from ..system import slug

    return {"nc_enabled": True, "nc_account": account, "nc_target": target,
            "nc_status": status,
            "nc_default_folder": "/" if (account and account["kind"] == "share") else "/gnubook", "nc_default_file": f"{slug(st.name)}.gnucash"}


def _nc_client(account):
    from ..crypto import SecretError
    from ..nextcloud import NextcloudError, client_for

    try:
        password = registry().system.nextcloud_password(account)
    except SecretError as exc:
        raise NextcloudError(_("Gespeichertes Passwort nicht lesbar – bitte Nextcloud trennen und neu verbinden.")) from exc
    return client_for(account, password)


def _nc_settings():
    return redirect(url_for("views.settings") + "#nextcloud")


def _nc_store(server: str, login: str, app_password: str):
    """Checks the credentials and stores them for the current user."""
    from ..nextcloud import Client

    info = Client(server, login, app_password).whoami()
    registry().system.set_nextcloud_account(g.user["id"], server, login, app_password, info["id"], info["display"])
    state().appdb.audit(session.get("user", "?"), "nextcloud-connect", None, f"{login} @ {server}")


@bp.route("/settings/nextcloud/connect", methods=["POST"])
@login_required
def nextcloud_connect():
    from ..nextcloud import NextcloudError, normalize_server

    if not registry().cfg.nextcloud.enabled:
        abort(404)
    try:
        server = normalize_server(request.form.get("server", ""), registry().cfg.nextcloud.allow_http)
        login = request.form.get("login", "").strip()
        password = request.form.get("app_password", "").strip()
        if not login or not password:
            raise NextcloudError(_("Bitte Benutzer und App-Passwort angeben."))
        _nc_store(server, login, password)
        flash(_("Nextcloud verbunden."), "success")
    except NextcloudError as exc:
        flash(str(exc), "danger")
    return _nc_settings()


@bp.route("/settings/nextcloud/share", methods=["POST"])
@login_required
def nextcloud_share():
    """Connect via a public share link of one folder (edit permission): gnubook only reaches that folder."""
    from ..nextcloud import NextcloudError, connect_share, parse_share_link

    cfg = registry().cfg.nextcloud
    if not cfg.enabled:
        abort(404)
    try:
        server, token = parse_share_link(request.form.get("link", ""), cfg.allow_http)
        password = request.form.get("share_password", "")
        client, legacy = connect_share(server, token, password)
        client.test_write("/")
        registry().system.set_nextcloud_account(g.user["id"], server, token, password, "", "", "share", legacy)
        state().appdb.audit(session.get("user", "?"), "nextcloud-connect", None, f"Freigabe @ {server}")
        flash(_("Freigabe verbunden. gnubook kann nur in diesen einen Ordner schreiben."), "success")
    except NextcloudError as exc:
        flash(str(exc), "danger")
    return _nc_settings()


@bp.route("/settings/nextcloud/flow", methods=["POST"])
@login_required
def nextcloud_flow_start():
    from ..nextcloud import NextcloudError, login_flow_start, normalize_server

    if not registry().cfg.nextcloud.enabled:
        abort(404)
    try:
        server = normalize_server(request.form.get("server", ""), registry().cfg.nextcloud.allow_http)
        flow = login_flow_start(server)
    except NextcloudError as exc:
        return jsonify({"error": str(exc)}), 400
    session["nc_flow"] = {"server": server, "token": flow["token"], "endpoint": flow["endpoint"]}
    return jsonify({"login": flow["login"]})


@bp.route("/settings/nextcloud/flow/poll", methods=["POST"])
@login_required
def nextcloud_flow_poll():
    from ..nextcloud import NextcloudError, login_flow_poll

    flow = session.get("nc_flow")
    if not flow:
        return jsonify({"error": _("Keine Anmeldung bei der Nextcloud gestartet.")}), 400
    try:
        result = login_flow_poll(flow["server"], flow["endpoint"], flow["token"])
        if result is None:
            return jsonify({"status": "pending"})
        session.pop("nc_flow", None)
        _nc_store(flow["server"], result["login"], result["app_password"])
    except NextcloudError as exc:
        session.pop("nc_flow", None)
        return jsonify({"error": str(exc)}), 400
    flash(_("Nextcloud verbunden."), "success")
    return jsonify({"status": "done"})


@bp.route("/settings/nextcloud/disconnect", methods=["POST"])
@login_required
def nextcloud_disconnect():
    reg = registry()
    account = reg.system.nextcloud_account(g.user["id"])
    if account is not None:
        try:
            if account["kind"] == "account":  # a share link is deleted by the user in Nextcloud
                _nc_client(account).revoke()
        except Exception:  # noqa: BLE001 – the app password may already be gone
            pass
        reg.system.delete_nextcloud_account(g.user["id"])
        state().appdb.audit(session.get("user", "?"), "nextcloud-disconnect", None, account["server"])
    flash(_("Nextcloud getrennt."), "success")
    return _nc_settings()


@bp.route("/settings/nextcloud/target", methods=["POST"])
@login_required
def nextcloud_target():
    from ..nextcloud import NextcloudError, clean_filename, clean_folder

    reg, st = registry(), state()
    account = reg.system.nextcloud_account(g.user["id"])
    if account is None:
        flash(_("Zuerst eine Nextcloud verbinden."), "warning")
        return _nc_settings()
    try:
        folder = clean_folder(request.form.get("folder", ""))
        filename = clean_filename(request.form.get("filename", ""))
        enabled = request.form.get("enabled") == "1"
        if enabled:
            _nc_client(account).test_write(folder)
        reg.system.set_nextcloud_target(g.user["id"], st.id, folder, filename, enabled)
        st.appdb.audit(session.get("user", "?"), "nextcloud-target", None, f"{folder.rstrip('/')}/{filename}")
        if enabled:
            st.backup.request()
            flash(_("Gespeichert. Schreibtest erfolgreich – die Datei wird jetzt hochgeladen."), "success")
        else:
            flash(_("Gespeichert. Upload in die Nextcloud ist für dieses Buch aus."), "success")
    except NextcloudError as exc:
        flash(_("Nicht gespeichert: {a0}", a0=exc), "danger")
    return _nc_settings()


@bp.route("/settings/nextcloud/target/delete", methods=["POST"])
@login_required
def nextcloud_target_delete():
    registry().system.delete_nextcloud_target(g.user["id"], state().id)
    flash(_("Nextcloud-Ziel entfernt. Die Datei in der Nextcloud bleibt erhalten."), "success")
    return _nc_settings()


@bp.route("/settings/nextcloud/upload", methods=["POST"])
@login_required
def nextcloud_upload_now():
    state().backup.request()
    flash(_("Sicherung angestoßen."), "info")
    return _nc_settings()


@bp.route("/settings/nextcloud/folders")
@login_required
def nextcloud_folders():
    from ..nextcloud import NextcloudError, clean_folder

    account = registry().system.nextcloud_account(g.user["id"])
    if account is None:
        return jsonify({"error": _("Zuerst eine Nextcloud verbinden.")}), 400
    try:
        folder = clean_folder(request.args.get("path", "/"))
        return jsonify({"path": folder, "folders": _nc_client(account).folders(folder)})
    except NextcloudError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.route("/settings/token", methods=["POST"])
@login_required
def token_create():
    st = state()
    label = (request.form.get("label") or "FinTS-Importer").strip()[:80]
    session["new_token"] = registry().system.create_token(g.user["id"], st.id, label)
    st.appdb.audit(session.get("user", "?"), "token-create", None, label)
    return redirect(url_for("views.settings") + "#api")


@bp.route("/settings/token/<int:token_id>/delete", methods=["POST"])
@login_required
def token_delete(token_id):
    st = state()
    registry().system.delete_token(token_id, st.id)
    st.appdb.audit(session.get("user", "?"), "token-delete", None, str(token_id))
    flash(_("Token gelöscht."), "success")
    return redirect(url_for("views.settings") + "#api")


@bp.route("/settings/unlock", methods=["POST"])
@login_required
def unlock():
    st = state()
    if request.form.get("confirm") != "yes":
        flash(_("Bitte bestätigen, dass GnuCash Desktop wirklich geschlossen ist."), "warning")
        return redirect(url_for("views.settings"))
    n = st.book.remove_foreign_locks()
    st.appdb.audit(session.get("user", "?"), "unlock", None, _("{a0} Sperreinträge entfernt", a0=n))
    flash(_("{a0} Sperreintrag/-einträge entfernt.", a0=n), "success")
    return redirect(url_for("views.settings"))


# ------------------------------------------------------------------------------------------ errors

@bp.app_errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"message": "Resource not found"}), 404
    return render_template("error.html", code=404, message=_("Seite nicht gefunden.")), 404


@bp.app_errorhandler(400)
def bad_request(e):
    if request.path.startswith("/api/"):
        return jsonify({"message": str(e.description)}), 400
    return render_template("error.html", code=400, message=e.description), 400


@bp.app_errorhandler(500)
def server_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"message": "Internal server error"}), 500
    return render_template("error.html", code=500, message="Interner Fehler – Details im Log."), 500
