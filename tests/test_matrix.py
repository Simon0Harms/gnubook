"""Matrix: linking, login links, inbox handling and Belegeingang notifications (sidecar replaced by the spool)."""
import json
import re

import pytest

from gnubook import create_app, matrix
from gnubook.postbox.service import RunOutcome

from .conftest import PASSWORD, csrf_from

BOT = "@gnubookbot:example.org"
ALICE = "@alice:example.org"


@pytest.fixture
def mcfg(cfg):
    cfg.matrix.bot_mxid = BOT
    cfg.matrix.public_url = "https://buch.example.org/"
    cfg.matrix.poll_seconds = 0  # the tests handle the inbox themselves
    return cfg


@pytest.fixture
def mapp(mcfg):
    application = create_app(mcfg)
    application.testing = True
    yield application
    application.extensions["gnubook"].dispose()


@pytest.fixture
def reg(mapp):
    return mapp.extensions["gnubook"]


@pytest.fixture
def mclient(mapp):
    c = mapp.test_client()
    page = c.get("/login")
    r = c.post("/login", data={"username": "tester", "password": PASSWORD, "csrf_token": csrf_from(page.text)})
    assert r.status_code == 302
    c.csrf = csrf_from(c.get("/").text)
    return c


def outbox(reg):
    d = matrix.base_dir(reg.cfg) / "outbox"
    msgs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))]
    for p in d.glob("*.json"):
        p.unlink()
    return msgs


def inbound(reg, body, sender=ALICE, room="!dm:example.org"):
    return matrix.Inbox(reg).handle({"eventId": "$x", "roomId": room, "sender": sender, "body": body, "ts": 0})


def link(reg, client, sender=ALICE, room="!dm:example.org"):
    r = client.post("/account/matrix/link", data={"csrf_token": client.csrf})
    assert r.status_code == 302
    code = re.search(r"GB-[A-Z2-9]{4}-[A-Z2-9]{4}", client.get("/account/password").text).group(0)
    assert inbound(reg, f"hier: {code}", sender, room) == "linked"
    return code


def tester(reg):
    return reg.system.user_by_name("tester")


def test_off_without_bot(app, client):
    assert client.post("/account/matrix/link", data={"csrf_token": client.csrf}).status_code == 404
    assert "Login-Link per Matrix" not in app.test_client().get("/login").text
    assert 'id="matrix"' not in client.get("/account/password").text


def test_link_by_code(reg, mclient):
    link(reg, mclient)
    u = tester(reg)
    assert u["mxid"] == ALICE and u["matrix_room"] == "!dm:example.org"
    (welcome,) = outbox(reg)
    assert welcome["roomId"] == "!dm:example.org" and "tester" in welcome["body"]
    page = mclient.get("/account/password").text
    assert ALICE in page and "Matrix-Konto verknüpft." in page


def test_code_only_once_and_not_for_a_second_user(reg, mclient):
    code = link(reg, mclient)
    outbox(reg)
    assert inbound(reg, code, "@mallory:example.org", "!other:example.org") == "badcode"
    other = reg.system.add_user("bob", "bob-passwort-123")
    raw, _cid = matrix.create_link_code(reg, other)
    assert inbound(reg, raw) == "mxidtaken"  # ALICE belongs to tester already
    assert reg.system.user(other)["mxid"] == ""


def test_login_link_from_login_page(reg, mapp, mclient):
    link(reg, mclient)
    outbox(reg)
    anon = mapp.test_client()
    page = anon.get("/login").text
    assert "Login-Link per Matrix" in page
    r = anon.post("/login/matrix", data={"login": "tester", "csrf_token": csrf_from(page)})
    assert r.status_code == 302
    (msg,) = outbox(reg)
    url = re.search(r"https://buch\.example\.org/login/matrix/(\S+)", msg["body"])
    assert url, msg["body"]
    path = "/login/matrix/" + url.group(1)
    # GET only shows a button – a link preview must not use the link up
    confirm = anon.get(path)
    assert confirm.status_code == 200 and "tester" in confirm.text
    r = anon.post(path, data={"csrf_token": csrf_from(confirm.text)})
    assert r.status_code == 302 and anon.get("/").status_code == 200
    # one-time
    again = mapp.test_client()
    assert again.get(path).status_code == 404


def test_login_link_answer_does_not_reveal_users(reg, mapp):
    anon = mapp.test_client()
    page = anon.get("/login").text
    anon.post("/login/matrix", data={"login": "gibtsnicht", "csrf_token": csrf_from(page)})
    assert outbox(reg) == []
    assert "gibtsnicht" in anon.get("/login").text  # same generic message


def test_login_command_and_rate_limit(reg, mclient):
    link(reg, mclient)
    outbox(reg)
    assert inbound(reg, "login") == "loginlink"
    assert inbound(reg, "login") == "rate"
    assert len(outbox(reg)) == 1
    assert inbound(reg, "login", "@stranger:example.org", "!s:example.org") == "nouser"


def test_login_from_new_chat_moves_delivery(reg, mclient):
    link(reg, mclient)
    assert matrix.Inbox(reg).handle({"type": "leave", "roomId": "!dm:example.org", "sender": ALICE}) == "left"
    assert tester(reg)["matrix_room"] == ""
    outbox(reg)
    assert inbound(reg, "login", room="!new:example.org") == "loginlink"
    assert tester(reg)["matrix_room"] == "!new:example.org"
    assert outbox(reg)[0]["roomId"] == "!new:example.org"


def test_unlink_leaves_room(reg, mclient):
    link(reg, mclient)
    outbox(reg)
    mclient.post("/account/matrix/unlink", data={"csrf_token": mclient.csrf})
    assert tester(reg)["mxid"] == ""
    (bye,) = outbox(reg)
    assert bye["leave"] is True


def test_inbox_files_are_processed_once(reg, mclient):
    link(reg, mclient)
    outbox(reg)
    inbox = matrix.base_dir(reg.cfg) / "inbox"
    (inbox / "1.json").write_text(json.dumps({"roomId": "!dm:example.org", "sender": ALICE, "body": "login"}))
    (inbox / "2.json").write_text("kaputt")
    assert matrix.Inbox(reg).process() == 1
    assert list(inbox.iterdir()) == []
    assert len(outbox(reg)) == 1


def test_postbox_notifications(reg, mclient, state):
    link(reg, mclient)
    outbox(reg)
    bid = state.id
    fail = RunOutcome("login_failed", "Anmeldung abgelehnt")
    assert matrix.notify_postbox(reg, bid, "Demo", "finanzen-zero", fail) == 1
    (msg,) = outbox(reg)
    assert "Anmeldung fehlgeschlagen" in msg["body"] and "Anmeldung abgelehnt" in msg["body"]
    assert "https://buch.example.org/postbox/" in msg["body"]
    # the same failure again: no second message (timer runs)
    assert matrix.notify_postbox(reg, bid, "Demo", "finanzen-zero", fail) == 0
    assert matrix.notify_postbox(reg, bid, "Demo", "finanzen-zero", RunOutcome("paused", "x")) == 0
    # processing errors of an otherwise good run count, too
    errs = RunOutcome("ok", "1 neu", processed={"errors": ["Nextcloud: 503"]})
    assert matrix.notify_postbox(reg, bid, "Demo", "finanzen-zero", errs) == 1
    assert "Nextcloud: 503" in outbox(reg)[0]["body"]
    # works again: told once
    assert matrix.notify_postbox(reg, bid, "Demo", "finanzen-zero", RunOutcome("ok", "2 neu")) == 1
    assert "läuft wieder" in outbox(reg)[0]["body"]
    assert matrix.notify_postbox(reg, bid, "Demo", "finanzen-zero", RunOutcome("ok", "2 neu")) == 0


def test_documents_to_review_are_reported(reg, mclient, state):
    link(reg, mclient)
    outbox(reg)
    bid = state.id
    out = RunOutcome("ok", "2 neu", processed={"attention": 1, "errors": []})
    assert matrix.notify_postbox(reg, bid, "Demo", "finanzen-zero", out) == 1
    (msg,) = outbox(reg)
    assert "1 zur Prüfung" in msg["body"]
    assert "/postbox/?source=finanzen-zero&filter=attention" in msg["body"]
    # every run with new documents to review is reported
    assert matrix.notify_postbox(reg, bid, "Demo", "finanzen-zero", out) == 1
    assert matrix.notify_postbox(reg, bid, "Demo", "finanzen-zero",
                                 RunOutcome("ok", "2 neu", processed={"attention": 0})) == 0
    # together with a processing error: two messages
    both = RunOutcome("ok", "1 neu", processed={"attention": 2, "errors": ["Nextcloud: 503"]})
    assert matrix.notify_postbox(reg, bid, "Demo", "finanzen-zero", both) == 2


def test_notifications_can_be_turned_off(reg, mclient, state):
    link(reg, mclient)
    outbox(reg)
    mclient.post("/account/matrix/notify", data={"on": "0", "csrf_token": mclient.csrf})
    assert matrix.notify_postbox(reg, state.id, "Demo", "finanzen-zero", RunOutcome("error", "kaputt")) == 0
    assert outbox(reg) == []


def test_english_messages(reg, mclient):
    reg.system.set_language(tester(reg)["id"], "en")
    link(reg, mclient)
    (welcome,) = outbox(reg)
    assert "Linked: gnubook user" in welcome["body"]
