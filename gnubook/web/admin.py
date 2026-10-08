"""Administration: users, books (GnuCash databases) and who may use which book."""
from __future__ import annotations

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

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
            flash("Benutzer angelegt.", "success")
            return redirect(url_for("admin.users"))
        except UserError as exc:
            flash(str(exc), "danger")
    rows = [(u, system.user_books(u["id"])) for u in system.users()]
    return render_template("admin/users.html", rows=rows, books=system.books())


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
    return render_template("admin/books.html", rows=rows, users=system.users(), form=form)


@bp.route("/books/<int:book_id>", methods=["POST"])
@login_required(book=False, admin=True)
def book_update(book_id):
    reg = registry()
    system = reg.system
    row = system.book(book_id)
    if row is None:
        abort(404)
    if request.form.get("action") == "delete":
        system.delete_book(book_id)
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
