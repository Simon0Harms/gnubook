"""The part of the Firefly III REST API that bnw/firefly-iii-fints-importer uses.

GET  /api/v1/accounts?type=asset&page=1&limit=250
POST /api/v1/transactions      (one transaction per request)
Authentication: "Authorization: Bearer <token>"; each token belongs to one user and one book.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time as dtime
from decimal import Decimal

from flask import Blueprint, g, jsonify, request, url_for

from ..book import BookError, WriteLockError
from ..importer import ImportRejected
from ..ledger import balances, load_transaction
from ..money import fraction_digits, symbol_for, to_api_string
from . import registry, state

log = logging.getLogger("gnubook.api")
bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _error(status: int, message: str, field: str | None = None):
    body = {"message": message}
    if field is not None:
        body["errors"] = {field: [message]}
    resp = jsonify(body)
    resp.status_code = status
    return resp


@bp.before_request
def authenticate():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or not header[7:].strip():
        return _error(401, "Unauthenticated.")
    found = registry().system.token_lookup(header[7:].strip())
    if found is None:
        return _error(401, "Unauthenticated.")
    g.user, book_id = found
    g.ctx = registry().context(book_id)
    if g.ctx is None:
        return _error(401, "Unauthenticated.")
    return None


def _currency_ids(index):
    return {g: i + 1 for i, g in enumerate(sorted(c.guid for c in index.commodities.values() if c.is_currency))}


def _iban_of(acc) -> str | None:
    code = (acc.code or "").replace(" ", "").upper()
    return code if len(code) >= 15 and code[:2].isalpha() and code[2:4].isdigit() else None


def _account_json(acc, api_id, balance, cur_ids, today, tz, expose_iban):
    iban = _iban_of(acc)
    places = fraction_digits(acc.commodity.fraction) if acc.commodity else 2
    return {
        "type": "accounts",
        "id": str(api_id),
        "attributes": {
            "created_at": None, "updated_at": None, "active": True, "order": None,
            "name": acc.full_name,
            "type": "asset",
            "account_role": "defaultAsset",
            "currency_id": str(cur_ids.get(acc.commodity_guid, 0)),
            "currency_code": acc.mnemonic,
            "currency_symbol": symbol_for(acc.mnemonic),
            "currency_decimal_places": places,
            "current_balance": to_api_string(balance, places),
            "current_balance_date": datetime.combine(today, dtime(23, 59, 59), tz).isoformat(),
            "notes": acc.description or None,
            "account_number": None if iban else (acc.code or None),
            "iban": iban if expose_iban else None,
            "bic": None,
            "virtual_balance": "0.00", "opening_balance": "0.00", "opening_balance_date": None,
            "include_net_worth": True,
            "gnucash_guid": acc.guid,
        },
        "links": {"self": url_for("api.account", account_id=api_id, _external=True)},
    }


@bp.get("/about")
def about():
    from .. import __version__

    return jsonify({"data": {"version": f"gnubook-{__version__}", "api_version": "2.0.0", "os": "gnubook",
                             "driver": state().book.engine.dialect.name}})


@bp.get("/about/user")
def about_user():
    return jsonify({"data": {"type": "users", "id": str(g.user["id"]), "attributes": {"email": g.user["username"],
                                                                      "blocked": False, "role": "owner"}}})


def _importable_with_balances():
    st = state()
    index = st.book.load_accounts()
    accounts = st.importer.importable_accounts(index)
    today = st.book.today()
    with st.book.connect() as conn:
        bal = balances(conn, st.book, upto=today)
    ids = st.appdb.account_ids([a.guid for a in accounts])
    return index, accounts, bal, ids, today


@bp.get("/accounts")
def accounts():
    st = state()
    typ = (request.args.get("type") or "all").lower()
    try:
        page = max(1, int(request.args.get("page", 1)))
        limit = min(500, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        return _error(422, "page/limit müssen Zahlen sein.", "page")
    index, accs, bal, ids, today = _importable_with_balances()
    if typ not in ("all", "asset", "assets"):
        accs = []
    cur_ids = _currency_ids(index)
    total = len(accs)
    pages = max(1, (total + limit - 1) // limit)
    chunk = accs[(page - 1) * limit: page * limit]
    data = [_account_json(a, ids[a.guid], bal.get(a.guid, Decimal(0)), cur_ids, today, st.book.tz,
                          st.cfg.api.expose_iban) for a in chunk]
    return jsonify({
        "data": data,
        "meta": {"pagination": {"total": total, "count": len(data), "per_page": limit, "current_page": page,
                                "total_pages": pages}},
        "links": {"self": request.url},
    })


@bp.get("/accounts/<int:account_id>")
def account(account_id: int):
    st = state()
    index, accs, bal, ids, today = _importable_with_balances()
    by_id = {ids[a.guid]: a for a in accs}
    acc = by_id.get(account_id)
    if acc is None:
        return _error(404, "Resource not found")
    return jsonify({"data": _account_json(acc, account_id, bal.get(acc.guid, Decimal(0)), _currency_ids(index),
                                          today, st.book.tz, st.cfg.api.expose_iban)})


def _group_json(tx_guid: str, entry, counter=None):
    st = state()
    index = st.book.load_accounts()
    with st.book.connect() as conn:
        tx = load_transaction(conn, st.book, index, tx_guid)
    tx_id = st.appdb.tx_id(tx_guid)
    own = entry.own
    own_split = next((s for s in tx.splits if s.account_guid == own.guid), None)
    amount = abs(own_split.value) if own_split else entry.amount
    others = [s for s in tx.splits if s.account_guid != own.guid]
    if counter is None and len(others) == 1:
        counter = others[0].account
    counter_id = st.appdb.account_id(counter.guid) if counter is not None else None
    counter_name = counter.full_name if counter is not None else (entry.cp_name or "")
    own_side = (str(st.appdb.account_id(own.guid)), own.full_name)
    other_side = (str(counter_id) if counter_id else None, counter_name)
    src, dst = (own_side, other_side) if entry.type == "withdrawal" else (other_side, own_side)
    cur = tx.currency
    places = fraction_digits(cur.fraction) if cur else 2
    split = {
        "user": "1",
        "transaction_journal_id": str(tx_id),
        "type": "transfer" if entry.counter_own is not None else entry.type,
        "date": datetime.combine(tx.day, dtime(0, 0), st.book.tz).isoformat(),
        "order": 0,
        "currency_id": str(_currency_ids(index).get(tx.currency_guid, 0)),
        "currency_code": cur.mnemonic if cur else "",
        "currency_symbol": symbol_for(cur.mnemonic) if cur else "",
        "currency_decimal_places": places,
        "amount": to_api_string(amount, places),
        "description": tx.description,
        "source_id": src[0], "source_name": src[1] or "", "source_iban": None,
        "destination_id": dst[0], "destination_name": dst[1] or "", "destination_iban": None,
        "budget_id": None, "budget_name": None, "category_id": None, "category_name": None,
        "bill_id": None, "bill_name": None, "reconciled": False,
        "notes": tx.notes or None, "tags": [], "internal_reference": None, "external_id": None,
        "sepa_ct_id": entry.sepa_ct_id or None,
        "gnucash_guid": tx.guid,
    }
    return {"data": {"type": "transactions", "id": str(tx_id),
                     "attributes": {"created_at": None, "updated_at": None, "user": "1",
                                    "group_title": None, "transactions": [split]},
                     "links": {"self": url_for("views.transaction", guid=tx.guid, _external=True)}}}


@bp.post("/transactions")
def store_transaction():
    st = state()
    try:
        body = json.loads(request.get_data(as_text=True) or "null", parse_float=Decimal)
    except ValueError:
        return _error(422, "Ungültiges JSON.", "transactions")
    try:
        result = st.importer.import_body(body, actor=f"api:{g.user['username']}")
    except ImportRejected as exc:
        return _error(422, str(exc), f"transactions.0.{exc.field}")
    except WriteLockError as exc:
        return _error(422, str(exc), "transactions.0.description")
    except BookError as exc:
        log.warning("import rejected: %s", exc)
        return _error(422, str(exc), "transactions.0.description")
    except Exception:  # keep the importer running; details are in the gnubook log
        log.exception("import failed")
        return _error(422, "Interner Fehler in gnubook beim Import – siehe gnubook-Log.", "transactions.0.description")
    if result.status == "duplicate":
        return _error(422, result.message, "transactions.0.description")
    return jsonify(_group_json(result.tx_guid, result.entry, result.counter))


@bp.errorhandler(404)
def not_found(_e):
    return _error(404, "Resource not found")


@bp.errorhandler(405)
def not_allowed(_e):
    return _error(405, "Method not allowed")
