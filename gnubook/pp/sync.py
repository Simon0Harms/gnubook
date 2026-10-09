"""Book a Portfolio Performance plan into the GnuCash book and keep it in sync.

Every booking has a stable key (the PP transaction UUID). gnubook remembers per key the GnuCash transaction, a
fingerprint of what it booked and a fingerprint of the transaction as it was written. With that it can

* create new bookings,
* change a booking when PP changed it – unless the transaction was changed in GnuCash meanwhile (conflict),
* delete a booking that was deleted in PP – again only when it is unchanged in GnuCash,
* leave transactions alone that were deleted in GnuCash (status "gone") or detached by the user.

All writes of one run happen in one piecash session inside Book.exclusive(), so GnuCash Desktop's lock is
respected and a failure writes nothing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text

from ..appdb import now_iso
from ..book import AccountIndex, BookError
from ..i18n import _
from ..money import ZERO, gnc_decimal
from .plan import Booking, Plan, build_plan, clean_name, mutual_type
from .settings import PPSettings, ROLE_TYPES, resolved

log = logging.getLogger("gnubook.pp")

SLOT = "gnubook-pp"
PRICE_SOURCE = "user:price"
MAX_FRACTION = 10 ** 8


class SyncError(BookError):
    pass


@dataclass
class SyncResult:
    revision: str = ""
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    conflicts: int = 0
    gone: int = 0
    skipped: int = 0
    accounts_created: int = 0
    commodities_created: int = 0
    prices_added: int = 0
    prices_updated: int = 0
    prices_removed: int = 0
    errors: list = field(default_factory=list)
    changes: list = field(default_factory=list)  # (action, key, description)
    wrote: bool = False
    book_replaced: bool = False  # the GnuCash book is another one than at the last run: links were reset

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [_("Buch ersetzt – Verknüpfungen neu aufgebaut")] if self.book_replaced else []
        parts += [_("{n} neu", n=self.created), _("{n} geändert", n=self.updated),
                 _("{n} gelöscht", n=self.deleted), _("{n} unverändert", n=self.unchanged)]
        if self.conflicts:
            parts.append(_("{n} Konflikte", n=self.conflicts))
        if self.gone:
            parts.append(_("{n} in GnuCash gelöscht", n=self.gone))
        if self.prices_added or self.prices_updated or self.prices_removed:
            parts.append(_("Kurse +{a}/~{u}/-{r}", a=self.prices_added, u=self.prices_updated, r=self.prices_removed))
        if self.accounts_created:
            parts.append(_("{n} Konten angelegt", n=self.accounts_created))
        if self.errors:
            parts.append(_("{n} Fehler", n=len(self.errors)))
        return ", ".join(parts)


# ------------------------------------------------------------------------------------------ fingerprints

def content_fingerprints(conn, book, guids) -> dict:
    """tx guid -> (fingerprint, any split reconciled) of the stored transactions. Notes, reconcile state and
    the entry date are left out: reconciling or annotating a booking in GnuCash is no conflict."""
    guids = list(guids)
    out = {}
    for i in range(0, len(guids), 400):
        chunk = guids[i:i + 400]
        names = {f"g{j}": g for j, g in enumerate(chunk)}
        clause = "(" + ", ".join(":" + n for n in names) + ")"
        heads = {r[0]: r for r in conn.execute(text(
            f"SELECT guid, currency_guid, post_date, description FROM transactions WHERE guid IN {clause}"), names)}
        splits = defaultdict(list)
        reconciled = defaultdict(bool)
        for tg, ag, vn, vd, qn, qd, memo, rs in conn.execute(text(
                "SELECT tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom, memo, "
                f"reconcile_state FROM splits WHERE tx_guid IN {clause}"), names):
            splits[tg].append([ag, _norm(gnc_decimal(int(vn), int(vd or 1))), _norm(gnc_decimal(int(qn), int(qd or 1))),
                               memo or ""])
            reconciled[tg] = reconciled[tg] or rs == "y"
        for g, (tg, cur, pd, desc) in heads.items():
            canon = [str(book.day_of(pd)), cur, desc or "", sorted(splits[g])]
            out[g] = (hashlib.sha256(json.dumps(canon, ensure_ascii=False).encode()).hexdigest(), reconciled[g])
    return out


def book_guid(conn) -> str | None:
    """GUID of the GnuCash book; a different one means the database now holds another book."""
    return conn.execute(text("SELECT guid FROM books")).scalar()


def _norm(d: Decimal) -> str:
    d = d.normalize()
    return "0" if d == 0 else format(d, "f")


# ------------------------------------------------------------------------------------------ run

class Syncer:
    """One synchronisation run for one book."""

    def __init__(self, ctx, settings: PPSettings, export: dict, force=(), actor: str = "pp-sync"):
        self.ctx = ctx
        self.book = ctx.book
        self.appdb = ctx.appdb
        self.settings = settings
        self.export = export
        self.force = set(force or ())
        self.actor = actor
        self.result = SyncResult(revision=(export.get("client") or {}).get("revision") or "")

    # ------------------------------------------------------------------ planning
    def plan(self, index: AccountIndex) -> Plan:
        base = index.root.commodity
        if base is None or not base.is_currency:
            raise SyncError(_("Das Buch hat keine Standardwährung (Wurzelkonto)."))
        return build_plan(self.export, base.mnemonic, sync_from=self.settings.start,
                          realized_gains=self.settings.realized_gains,
                          capitalize_fees=self.settings.capitalize_fees,
                          cash_accounts=self.settings.cash_accounts)

    def run(self, dry_run: bool = False) -> SyncResult:
        started = now_iso()
        index = self.book.load_accounts()
        plan = self.plan(index)
        names = resolved(self.settings, index)
        r = self.result
        r.skipped = sum(1 for b in plan.bookings if b.skip)
        with self.book.connect() as conn:
            self._book_guid = book_guid(conn)
        known = self.appdb.meta("pp_book_guid")
        # the database holds another book than at the last run (e.g. GnuCash "Save As" over it): the stored
        # transaction, commodity, account and price GUIDs belong to the old book. Without a reset every booking
        # would count as "deleted in GnuCash" and never be booked again.
        r.book_replaced = bool(known and self._book_guid and known != self._book_guid)
        if r.book_replaced:
            log.warning("PP sync: GnuCash book changed (%s -> %s), resetting the links", known, self._book_guid)
            if not dry_run:
                self.appdb.pp_reset_links()
                self.appdb.audit(self.actor, "pp-book-replaced", self._book_guid, f"vorher {known}")
        records = {} if r.book_replaced else self.appdb.pp_records()
        objects = {} if r.book_replaced else self.appdb.pp_objects()

        with self.book.connect() as conn:
            existing = content_fingerprints(conn, self.book, [rec["tx_guid"] for rec in records.values()
                                                              if rec.get("tx_guid")])
            # mapping lost (e.g. new gnubook data dir)? find our transactions by their slot
            if not records:
                for key, guid in conn.execute(text("SELECT string_val, obj_guid FROM slots WHERE name = :n"),
                                              {"n": SLOT}):
                    records[key] = {"pp_key": key, "tx_guid": guid, "spec_fp": "", "gc_fp": None,
                                    "status": "booked", "message": None}
                if records:
                    existing = content_fingerprints(conn, self.book, [rec["tx_guid"] for rec in records.values()])
                    for rec in records.values():
                        rec["gc_fp"] = existing.get(rec["tx_guid"], (None, False))[0]

        wanted = {b.key: b for b in plan.booked}
        todo_create, todo_update, todo_delete = [], [], []
        status_updates: dict[str, dict] = {}
        for key, b in wanted.items():
            rec = records.get(key)
            if rec is None:
                todo_create.append(b)
                continue
            if rec["status"] == "detached" and key not in self.force:
                continue
            current = existing.get(rec.get("tx_guid") or "")
            if current is None:  # deleted in GnuCash
                if key in self.force:
                    todo_create.append(b)
                else:
                    r.gone += 1
                    if rec["status"] != "gone":
                        status_updates[key] = dict(rec, status="gone", message="in GnuCash gelöscht")
                continue
            fp, reconciled = current
            pp_changed = rec.get("spec_fp") != b.fingerprint
            gc_changed = rec.get("gc_fp") is not None and fp != rec.get("gc_fp")
            if not pp_changed and key not in self.force:
                r.unchanged += 1
                want = "edited" if gc_changed else "booked"
                if rec["status"] != want:
                    status_updates[key] = dict(rec, status=want,
                                               message="in GnuCash bearbeitet" if gc_changed else None)
                continue
            if (gc_changed or reconciled) and key not in self.force:
                r.conflicts += 1
                msg = ("in GnuCash abgeglichen – Änderung aus PP nicht übernommen" if reconciled and not gc_changed
                       else "in PP und in GnuCash geändert – bitte entscheiden")
                if rec["status"] != "conflict" or rec.get("message") != msg:
                    status_updates[key] = dict(rec, status="conflict", message=msg)
                continue
            todo_update.append((b, rec))
        for key, rec in records.items():
            if key in wanted:
                continue
            current = existing.get(rec.get("tx_guid") or "")
            if rec["status"] == "detached" or current is None:
                todo_delete.append((key, rec, False))  # only forget it
                continue
            fp, reconciled = current
            if (fp != rec.get("gc_fp") or reconciled) and key not in self.force:
                r.conflicts += 1
                msg = "in PP gelöscht, in GnuCash geändert oder abgeglichen – bitte selbst löschen"
                if rec["status"] != "conflict" or rec.get("message") != msg:
                    status_updates[key] = dict(rec, status="conflict", message=msg)
                continue
            todo_delete.append((key, rec, True))

        prices_todo = self._price_plan(plan, index, objects, r.book_replaced) if self.settings.prices else None
        need_write = bool(todo_create or todo_update or any(d[2] for d in todo_delete)
                          or (prices_todo and prices_todo.pending))
        if dry_run:
            r.created, r.updated = len(todo_create), len(todo_update)
            r.deleted = sum(1 for d in todo_delete if d[2])
            return r
        if status_updates:
            self.appdb.pp_save_records(list(status_updates.values()))
        forget = [d[0] for d in todo_delete if not d[2]]
        if not need_write:
            if forget:
                self.appdb.pp_save_records([], delete_keys=forget)
            self._finish(started, plan)
            return r

        self._validate(plan, todo_create + [b for b, _rec in todo_update], names, index)
        written = self._write(plan, index, names, objects, todo_create, todo_update,
                              [d for d in todo_delete if d[2]], prices_todo)
        # remember what was written (fingerprints of the stored state)
        with self.book.connect() as conn:
            stored = content_fingerprints(conn, self.book, [g for g in written.values() if g])
        rows = []
        for key, guid in written.items():
            b = wanted.get(key)
            if b is None or guid is None:
                continue
            rows.append({"pp_key": key, "tx_guid": guid, "spec_fp": b.fingerprint,
                         "gc_fp": stored.get(guid, (None, False))[0], "status": "booked", "message": None,
                         "kind": b.kind, "day": b.day.isoformat(), "description": b.description,
                         "amount": str(b.amount)})
        deleted_keys = [k for k, guid in written.items() if guid is None] + forget
        self.appdb.pp_save_records(rows, delete_keys=deleted_keys)
        for action, key, desc in r.changes:
            guid = written.get(key)
            self.appdb.audit(self.actor, f"pp-{action}", guid, desc)
        r.wrote = True
        self._finish(started, plan)
        return r

    def _finish(self, started: str, plan: Plan):
        r = self.result
        self.appdb.set_meta("pp_revision", r.revision)
        self.appdb.set_meta("pp_settings_fp", self.settings.booking_fingerprint())
        if getattr(self, "_book_guid", None):
            self.appdb.set_meta("pp_book_guid", self._book_guid)
        self.appdb.set_meta("pp_last_sync", now_iso())
        details = json.dumps({"changes": r.changes[:500], "errors": r.errors}, ensure_ascii=False)
        self.appdb.pp_add_run(started, self.actor, r.revision, "ok" if r.ok else "error", r.summary(), details)

    # ------------------------------------------------------------------ checks before writing
    def _validate(self, plan: Plan, bookings, names: dict, index: AccountIndex):
        problems = []
        base = index.root.commodity
        for role in ROLE_TYPES:
            acc = index.find(names[role])
            if acc is not None:
                if acc.placeholder:
                    problems.append(_("Konto {a} ist ein Platzhalter und nimmt keine Buchungen an.", a=acc.full_name))
                if acc.commodity_guid != base.guid:
                    problems.append(_("Konto {a} wird nicht in {c} geführt.", a=acc.full_name, c=base.mnemonic))
        for pp_acc, ref in self.settings.cash_accounts.items():
            acc = index.find(ref)
            if acc is None:
                problems.append(_("Konto für das PP-Verrechnungskonto nicht gefunden: {a}", a=ref))
            elif acc.placeholder or acc.commodity_guid != base.guid:
                problems.append(_("Konto {a} taugt nicht als Verrechnungskonto (Platzhalter/Währung).", a=acc.full_name))
        root = index.find(names["securities_root"])
        if root is not None and root.type not in ("ASSET", "STOCK", "MUTUAL", "BANK"):
            problems.append(_("{a} ist kein Vermögenskonto.", a=root.full_name))
        for b in bookings:
            for leg in b.legs:
                if leg.ref[0] == "stock" and leg.quantity is not None and leg.quantity * leg.value < 0:
                    problems.append(_("{d} {t}: Stückzahl und Wert mit verschiedenem Vorzeichen.", d=b.day, t=b.description))
        if problems:
            raise SyncError(" ".join(dict.fromkeys(problems)))

    # ------------------------------------------------------------------ prices
    @dataclass
    class PriceTodo:
        add: dict      # (security uuid, day) -> value
        update: dict   # (commodity guid, day) -> (price guid, value)
        remove: dict   # (commodity guid, day) -> price guid
        keep: dict     # mapping entries that stay

        @property
        def pending(self) -> bool:
            return bool(self.add or self.update or self.remove)

    def _price_plan(self, plan: Plan, index: AccountIndex, objects: dict, fresh: bool = False):
        """Prices to write: daily for the last price_days days, one per month before, from shortly before the
        first booking of each security on. Prices that are not gnubook's (e.g. entered in GnuCash) win."""
        mine = {} if fresh else self.appdb.pp_prices()
        today = date.today()
        daily_from = today - timedelta(days=max(0, int(self.settings.price_days or 0)))
        first = {}
        for b in plan.booked:
            if b.security and b.legs and any(leg.ref[0] == "stock" for leg in b.legs):
                first[b.security] = min(first.get(b.security, b.day), b.day)
        target = {}  # (security, day) -> value in book currency
        for sec_uuid, start in first.items():
            sec = plan.securities.get(sec_uuid)
            if sec is None:
                continue
            start = start - timedelta(days=7)
            rows = [(d, vb if vb is not None else (v if sec.currency == plan.base else None))
                    for d, v, vb in sec.prices if d >= start]
            rows = [(d, v) for d, v in rows if v is not None and v > 0]
            monthly = {}
            for d, v in rows:
                if d >= daily_from:
                    target[(sec_uuid, d)] = v
                else:
                    monthly[(d.year, d.month)] = (d, v)  # rows are sorted: the last one per month stays
            for d, v in monthly.values():
                target[(sec_uuid, d)] = v
        add, update, keep = {}, {}, {}
        remove = {}
        targets_by_cdty = {}
        for (sec_uuid, d), v in target.items():
            cg = objects.get(f"security:{sec_uuid}")
            if cg is None or cg not in index.commodities:
                add[(sec_uuid, d)] = v  # commodity is created in this run
                continue
            targets_by_cdty[(cg, d.isoformat())] = v
            old = mine.get((cg, d.isoformat()))
            if old is None:
                add[(sec_uuid, d)] = v
            elif Decimal(old[1]) != v:
                update[(cg, d.isoformat())] = (old[0], v)
            else:
                keep[(cg, d.isoformat())] = old
        for k, (pguid, _v) in mine.items():
            if k not in targets_by_cdty:
                remove[k] = pguid
        return self.PriceTodo(add, update, remove, keep)

    # ------------------------------------------------------------------ writing
    def _write(self, plan: Plan, index: AccountIndex, names: dict, objects: dict, creates, updates, deletes,
               prices):
        """Returns {key: tx guid} of the bookings written (None for deleted ones)."""
        from piecash import Account, Commodity, Price, Split, Transaction

        r = self.result
        written: dict[str, str | None] = {}
        sep = index.separator
        with self.book.exclusive():
            pc = self.book.piecash_book()
            try:
                s = pc.session
                base = s.query(Commodity).filter_by(guid=index.root.commodity_guid).one()
                accounts_by_guid: dict = {}

                def acc_by_guid(guid):
                    if guid not in accounts_by_guid:
                        accounts_by_guid[guid] = s.query(Account).filter_by(guid=guid).one_or_none()
                    return accounts_by_guid[guid]

                def ensure(full_name: str, typ: str, commodity, placeholder: bool = False, code: str = ""):
                    """Account by full name, created with missing parents (parents as placeholders)."""
                    found = index.find(full_name)
                    if found is not None:
                        return acc_by_guid(found.guid)
                    parent = pc.root_account
                    parts = [p for p in full_name.split(sep) if p]
                    for i, part in enumerate(parts):
                        last = i == len(parts) - 1
                        child = next((c for c in parent.children if c.name == part), None)
                        if child is None:
                            ptype = typ if last else (parent.type if parent.type != "ROOT" else
                                                      ("ASSET" if typ in ("STOCK", "MUTUAL", "BANK") else typ))
                            if not last and ptype in ("STOCK", "MUTUAL", "BANK"):
                                ptype = "ASSET"
                            child = Account(name=part, type=ptype, parent=parent,
                                            commodity=commodity if last else base,
                                            placeholder=int(placeholder if last else True),
                                            code=code if last else "")
                            r.accounts_created += 1
                        parent = child
                    return parent

                roles = {}

                def role(name):
                    if name not in roles:
                        roles[name] = ensure(names[name], ROLE_TYPES[name], base)
                    return roles[name]

                cash = {}

                def cash_account(pp_account):
                    if pp_account not in cash:
                        acc = index.find(self.settings.cash_accounts[pp_account])
                        cash[pp_account] = acc_by_guid(acc.guid)
                    return cash[pp_account]

                refs = {}  # pp_objects key -> piecash object (GUIDs are known after the flush)
                # securities (commodities)
                commodities = {}
                taken = {(c.namespace, c.mnemonic) for c in index.commodities.values()}
                by_isin = {}
                for c in index.commodities.values():
                    if not c.is_currency:
                        cusip = s.query(Commodity.cusip).filter_by(guid=c.guid).scalar()
                        if cusip:
                            by_isin.setdefault(cusip.strip().upper(), c.guid)

                def commodity(sec_uuid):
                    if sec_uuid in commodities:
                        return commodities[sec_uuid]
                    sec = plan.securities[sec_uuid]
                    need = min(MAX_FRACTION, 10 ** max(0, sec.decimals))
                    guid = objects.get(f"security:{sec_uuid}")
                    obj = s.query(Commodity).filter_by(guid=guid).one_or_none() if guid else None
                    if obj is None and sec.isin and sec.isin.strip().upper() in by_isin:
                        obj = s.query(Commodity).filter_by(guid=by_isin[sec.isin.strip().upper()]).one()
                    if obj is None:
                        ns = (self.settings.namespace or "Portfolio Performance").strip()
                        mnemonic, n = sec.symbol, 2
                        while (ns, mnemonic) in taken:
                            mnemonic, n = f"{sec.symbol}-{n}", n + 1
                        taken.add((ns, mnemonic))
                        obj = Commodity(namespace=ns, mnemonic=mnemonic, fullname=sec.name[:2000],
                                        fraction=max(need, 1), cusip=(sec.isin or ""), quote_flag=0, quote_source="")
                        s.add(obj)
                        r.commodities_created += 1
                    elif obj.fraction < need:
                        obj.fraction = need
                    refs[f"security:{sec_uuid}"] = obj
                    commodities[sec_uuid] = obj
                    return obj

                stock_cache = {}

                def stock(port_uuid, sec_uuid):
                    key = (port_uuid, sec_uuid)
                    if key in stock_cache:
                        return stock_cache[key]
                    cdty = commodity(sec_uuid)
                    sec = plan.securities[sec_uuid]
                    obj_key = f"stock:{port_uuid}:{sec_uuid}"
                    acc = acc_by_guid(objects[obj_key]) if obj_key in objects else None
                    if acc is not None and acc.commodity is not cdty and acc.commodity.guid != cdty.guid:
                        acc = None
                    if acc is None:
                        pkey = f"portfolio:{port_uuid}"
                        parent = refs.get(pkey) or (acc_by_guid(objects[pkey]) if pkey in objects else None)
                        if parent is None:
                            pname = clean_name((plan.portfolios.get(port_uuid) or {}).get("name") or "Depot")
                            parent = ensure(f"{names['securities_root']}{sep}{pname}", "ASSET", base,
                                            placeholder=True)
                        refs[pkey] = parent
                        name = clean_name(sec.name)
                        acc = next((c for c in parent.children if c.name == name), None)
                        if acc is not None and acc.commodity.guid != cdty.guid:
                            name = clean_name(f"{sec.name} ({sec.isin or sec.symbol})")
                            acc = next((c for c in parent.children if c.name == name), None)
                        if acc is None:
                            acc = Account(name=name, type=mutual_type(sec.name), parent=parent, commodity=cdty,
                                          commodity_scu=cdty.fraction, code=(sec.isin or "")[:2048])
                            r.accounts_created += 1
                        refs[obj_key] = acc
                    if acc.commodity_scu < cdty.fraction:
                        acc.commodity_scu = cdty.fraction
                    stock_cache[key] = acc
                    return acc

                def resolve(leg):
                    kind = leg.ref[0]
                    if kind == "stock":
                        return stock(leg.ref[1], leg.ref[2])
                    if kind == "cash":
                        return cash_account(leg.ref[1])
                    return role(leg.ref[1])

                def make_splits(b: Booking):
                    out = []
                    for leg in b.legs:
                        acc = resolve(leg)
                        qty = leg.quantity if leg.ref[0] == "stock" else None
                        out.append((acc, leg.value, qty, leg.memo))
                    return out

                # piecash adds a price for every split with shares (like GnuCash's register). For transfers,
                # deliveries and opening bookings that would be the cost, not a market price: removed below.
                trade_prices_before = {row[0] for row in s.execute(text(
                    "SELECT guid FROM prices WHERE source = 'user:split-register'"))}
                no_price_days = set()
                now = datetime.now(timezone.utc).replace(microsecond=0)
                for b in [*creates, *(u[0] for u in updates)]:
                    if b.kind in ("TRANSFER", "DELIVERY_INBOUND", "DELIVERY_OUTBOUND", "OPENING"):
                        no_price_days.add((b.security, b.day))
                for b in creates:
                    tx = Transaction(currency=base, description=b.description, post_date=b.day, enter_date=now,
                                     num="", splits=[Split(account=a, value=v, quantity=q, memo=m)
                                                     for a, v, q, m in make_splits(b)])
                    tx.notes = b.notes
                    tx[SLOT] = b.key
                    s.flush()
                    written[b.key] = tx.guid
                    r.created += 1
                    r.changes.append(("create", b.key, f"{b.day} {b.description} {b.amount}"))
                for b, rec in updates:
                    tx = s.query(Transaction).filter_by(guid=rec["tx_guid"]).one()
                    if tx.currency.guid != base.guid:
                        pc.delete(tx)
                        tx = Transaction(currency=base, description=b.description, post_date=b.day,
                                         enter_date=now, num="",
                                         splits=[Split(account=a, value=v, quantity=q, memo=m)
                                                 for a, v, q, m in make_splits(b)])
                    else:
                        if (tx.description or "") != b.description:
                            tx.description = b.description
                        if tx.post_date != b.day:
                            tx.post_date = b.day
                        old = list(tx.splits)
                        used = set()
                        for a, v, q, m in make_splits(b):
                            match = next((sp for sp in old if id(sp) not in used and sp.account.guid == a.guid
                                          and (sp.quantity == 0) == (q == 0 if q is not None else sp.quantity == 0)),
                                         None) or next((sp for sp in old if id(sp) not in used
                                                        and sp.account.guid == a.guid), None)
                            if match is None:
                                tx.splits.append(Split(account=a, value=v, quantity=q, memo=m))
                                continue
                            used.add(id(match))
                            qty = v if q is None else q
                            if match.value != v or match.quantity != qty:
                                match.value = v
                                match.quantity = qty
                            if (match.memo or "") != m:
                                match.memo = m
                        for sp in old:
                            if id(sp) not in used:
                                tx.splits.remove(sp)
                    tx.notes = b.notes
                    if SLOT not in tx:
                        tx[SLOT] = b.key
                    s.flush()
                    written[b.key] = tx.guid
                    r.updated += 1
                    r.changes.append(("update", b.key, f"{b.day} {b.description} {b.amount}"))
                for key, rec, _really in deletes:
                    tx = s.query(Transaction).filter_by(guid=rec["tx_guid"]).one_or_none()
                    if tx is not None:
                        pc.delete(tx)
                    written[key] = None
                    r.deleted += 1
                    r.changes.append(("delete", key, rec.get("description") or ""))

                price_rows = {}
                price_deletes = []
                if prices is not None:
                    existing = {}
                    for sec_uuid, _d in prices.add:
                        if sec_uuid in plan.securities:
                            commodity(sec_uuid)
                    s.flush()  # new commodities get their GUIDs
                    cdty_guids = {obj.guid for obj in commodities.values()} | {g for g, _d in prices.update} | {
                        g for g, _d in prices.remove}
                    if cdty_guids:
                        for pg, cg, cug, pd, src in s.execute(text(
                                "SELECT guid, commodity_guid, currency_guid, date, source FROM prices")):
                            if cg in cdty_guids and cug == base.guid:
                                existing.setdefault((cg, str(self.book.day_of(pd))), []).append((pg, src))
                    for (sec_uuid, d), v in prices.add.items():
                        cdty = commodity(sec_uuid)
                        k = (cdty.guid, d.isoformat())
                        if k in prices.keep or k in prices.update:
                            continue
                        others = existing.get(k, [])
                        if any(src == PRICE_SOURCE for _pg, src in others):
                            continue  # someone else's price for that day (e.g. entered in GnuCash)
                        p = Price(commodity=cdty, currency=base, date=d, value=v, type="last", source=PRICE_SOURCE)
                        p.guid = uuid.uuid4().hex  # known before the (single) flush
                        s.add(p)
                        price_rows[k] = (p.guid, str(v))
                        r.prices_added += 1
                    for k, (pg, v) in prices.update.items():
                        p = s.query(Price).filter_by(guid=pg).one_or_none()
                        if p is None:
                            price_deletes.append(k)
                            continue
                        p.value = v
                        price_rows[k] = (pg, str(v))
                        r.prices_updated += 1
                    for k, pg in prices.remove.items():
                        p = s.query(Price).filter_by(guid=pg).one_or_none()
                        if p is not None:
                            s.delete(p)
                            r.prices_removed += 1
                        price_deletes.append(k)
                s.flush()
                for k, obj in refs.items():
                    objects[k] = obj.guid
                no_price = {(commodity(sec).guid, d) for sec, d in no_price_days if sec in plan.securities}
                pc.save()  # validates: here piecash adds the prices of the splits
            except Exception:
                pc.cancel()
                raise
            finally:
                pc.close()
            if no_price:
                with self.book.engine.begin() as conn:
                    doomed = [pg for pg, cg, pd in conn.execute(text(
                        "SELECT guid, commodity_guid, date FROM prices WHERE source = 'user:split-register'"))
                        if pg not in trade_prices_before and (cg, self.book.day_of(pd)) in no_price]
                    for pg in doomed:
                        conn.execute(text("DELETE FROM prices WHERE guid = :g"), {"g": pg})
        self.appdb.pp_save_objects(objects)
        if prices is not None:
            self.appdb.pp_save_prices(price_rows, price_deletes)
        return written


def sync(ctx, settings: PPSettings, export: dict, force=(), actor: str = "pp-sync",
         dry_run: bool = False) -> SyncResult:
    return Syncer(ctx, settings, export, force=force, actor=actor).run(dry_run=dry_run)
