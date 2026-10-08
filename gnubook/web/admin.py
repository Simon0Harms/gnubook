"""Administration: users, books (GnuCash databases) and who may use which book."""
from __future__ import annotations

import os
import tempfile

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

from ..book import Book
from ..system import UserError, mask_url
from . import registry
from .auth import login_required

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _test_book(url: str, timezone: str) -> str | None:
    """None if the database is a GnuCash book gnubook can use, else an error message."""
    try:
        b = Book(url, timezone)
        try:
            info = b.schema_info()
            b.load_accounts()
        finally:
            b.dispose()
    except Exception as exc:  # noqa: BLE001
        return f"Verbindung oder Buch ungültig: {exc}"
    if not info["supported"]:
        return f"GnuCash-Version {info['gnucash']} wird nicht unterstützt (nur lesen wäre möglich)."
    return None


def _save_upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        raise UserError("Bitte eine .gnucash-Datei (SQLite-Format) auswählen.")
    tmpdir = registry().data / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".gnucash", dir=tmpdir)
    os.close(fd)
    f.save(path)
    return path


def _create_book(name, content, users, backup):
    path = _save_upload() if content == "upload" else None
    try:
        bid, creds = registry().create_book(name, content, path, users, backup)
    finally:
        if path:
            os.unlink(path)
    session["new_book_credentials"] = creds
    return bid


@bp.route("/users", methods=["GET", "POST"])
@login_required(book=False, admin=True)
def users():
    system = registry().system
    if request.method == "POST":
        try:
            uid = system.add_user(request.form.get("username", ""), request.form.get("password", ""),
                                  request.form.get("is_admin") == "1")
            for bid in request.form.getlist("books"):
                system.grant(uid, int(bid))
            if request.form.get("own_book") == "1":
                try:
                    _create_book(request.form.get("book_name") or request.form.get("username", ""),
                                 request.form.get("content", "simple"), [uid, g.user["id"]], True)
                except UserError as exc:
                    flash(f"Benutzer angelegt, aber kein Buch: {exc}", "warning")
                    return redirect(url_for("admin.users"))
                flash("Benutzer und Buch angelegt.", "success")
                return redirect(url_for("admin.books"))
            flash("Benutzer angelegt.", "success")
            return redirect(url_for("admin.users"))
        except UserError as exc:
            flash(str(exc), "danger")
    rows = [(u, system.user_books(u["id"])) for u in system.users()]
    return render_template("admin/users.html", rows=rows, books=system.books(),
                           can_create=bool(registry().cfg.postgres.admin_url))


@bp.route("/users/<int:user_id>", methods=["POST"])
@login_required(book=False, admin=True)
def user_update(user_id):
    system = registry().system
    if system.user(user_id) is None:
        abort(404)
    action = request.form.get("action")
    try:
        if action == "delete":
            if user_id == g.user["id"]:
                raise UserError("Den eigenen Benutzer kann man nicht löschen.")
            system.delete_user(user_id)
            flash("Benutzer gelöscht.", "success")
        elif action == "password":
            system.set_password(user_id, request.form.get("password", ""))
            flash("Passwort gesetzt.", "success")
        else:
            system.update_user(user_id, request.form.get("is_admin") == "1", request.form.get("active") == "1")
            with system.conn() as c:
                c.execute("DELETE FROM user_books WHERE user_id = ?", (user_id,))
                c.executemany("INSERT INTO user_books (user_id, book_id) VALUES (?, ?)",
                              [(user_id, int(b)) for b in request.form.getlist("books")])
            flash("Gespeichert.", "success")
    except UserError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin.users"))


@bp.route("/books", methods=["GET", "POST"])
@login_required(book=False, admin=True)
def books():
    reg = registry()
    system = reg.system
    form = {"name": "", "url": "", "timezone": reg.cfg.book.timezone, "backup_file": ""}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in form}
        error = _test_book(form["url"], form["timezone"] or "Europe/Berlin")
        if error:
            flash(error, "danger")
        else:
            try:
                backup = form["backup_file"] if request.form.get("backup") == "1" else ""
                if request.form.get("backup") == "1" and not backup:
                    backup = reg.default_backup_file(form["name"])
                bid = system.add_book(form["name"], form["url"], form["timezone"] or "Europe/Berlin", backup)
                system.set_book_users(bid, request.form.getlist("users") + [str(g.user["id"])])
                flash(f"Buch „{form['name']}“ verbunden.", "success")
                return redirect(url_for("admin.books"))
            except UserError as exc:
                flash(str(exc), "danger")
    rows = [(b, mask_url(b["url"]), system.book_users(b["id"])) for b in system.books()]
    return render_template("admin/books.html", rows=rows, users=system.users(), form=form,
                           can_create=bool(reg.cfg.postgres.admin_url),
                           credentials=session.pop("new_book_credentials", None))


@bp.route("/books/new", methods=["POST"])
@login_required(book=False, admin=True)
def book_create():
    try:
        _create_book(request.form.get("name", ""), request.form.get("content", "empty"),
                     [int(u) for u in request.form.getlist("users")] + [g.user["id"]],
                     request.form.get("backup") == "1")
        flash("Buch angelegt.", "success")
    except UserError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin.books"))


@bp.route("/books/<int:book_id>", methods=["POST"])
@login_required(book=False, admin=True)
def book_update(book_id):
    reg = registry()
    system = reg.system
    row = system.book(book_id)
    if row is None:
        abort(404)
    if request.form.get("action") in ("delete", "drop"):
        drop = request.form.get("action") == "drop"
        if drop and request.form.get("confirm_name", "").strip() != row["name"]:
            flash("Zum Löschen der Datenbank bitte den Buchnamen genau eintippen.", "danger")
            return redirect(url_for("admin.books"))
        try:
            saved = reg.remove_book(book_id, drop_database=drop)
        except UserError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("admin.books"))
        if drop:
            flash(f"Buch „{row['name']}“ und seine Datenbank gelöscht. Letzte Sicherung: {saved}", "success")
        else:
            flash(f"Verbindung zu „{row['name']}“ entfernt. Die GnuCash-Datenbank selbst ist unverändert.", "success")
        return redirect(url_for("admin.books"))
    url = request.form.get("url", "").strip() or row["url"]  # empty field keeps the stored URL (password)
    tz = request.form.get("timezone", "").strip() or row["timezone"]
    if url != row["url"]:
        error = _test_book(url, tz)
        if error:
            flash(error, "danger")
            return redirect(url_for("admin.books"))
    system.update_book(book_id, request.form.get("name", row["name"]), url, tz, request.form.get("backup_file", ""))
    system.set_book_users(book_id, request.form.getlist("users"))
    flash("Gespeichert.", "success")
    return redirect(url_for("admin.books"))
