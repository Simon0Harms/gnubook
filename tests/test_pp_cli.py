"""Command line of the Portfolio Performance link (pp-core replaced by a fake): what the timer runs."""
from pathlib import Path

import pytest
from click.testing import CliRunner

from gnubook.cli import main
from gnubook.pp.client import PPCoreError
from gnubook.pp.settings import PPSettings

from .pp_fixtures import fake, make_export, pp_app  # noqa: F401 – fixtures


@pytest.fixture
def run(pp_app, cfg, tmp_path):
    """gnubook <args> against the data directory of pp_app (same system database and book)."""
    reg = pp_app.extensions["gnubook"]
    book = reg.system.books()[0]
    reg.system.set_pp_settings(book["id"], PPSettings(enabled=True))
    copy = tmp_path / "copy" / "buch.gnucash"
    reg.system.update_book(book["id"], book["name"], book["url"], book["timezone"], str(copy))
    conf = tmp_path / "config.toml"
    conf.write_text(f'[app]\nsecret_key = "{cfg.app.secret_key}"\ndata_dir = "{cfg.app.data_dir}"\n'
                    f'[pp]\nurl = "{cfg.pp.url}"\ntoken = "{cfg.pp.token}"\n', encoding="utf-8")

    def call(*args):
        result = CliRunner().invoke(main, ["--config", str(conf), *args], catch_exceptions=False)
        return result.exit_code, result.output
    call.ctx = reg.context(book["id"])
    call.copy = copy
    return call


def test_pp_sync_books_once_and_writes_the_gnucash_copy(run):
    code, out = run("pp-sync", "--dry-run")
    assert code == 0 and "14 neu" in out and not run.ctx.appdb.pp_records()
    assert not run.copy.exists()
    code, out = run("pp-sync")
    assert code == 0 and "14 neu" in out, out
    assert len(run.ctx.appdb.pp_records()) == 14
    # the command ends right away – the .gnucash copy must not be left to the background thread
    assert run.copy.exists() and run.copy.stat().st_size > 0
    code, out = run("pp-sync")
    assert code == 0 and "unverändert" in out


def test_pp_update_loads_prices_when_due_and_books_changes(run, fake):
    code, out = run("pp-update")
    assert code == 0, out
    assert ("quotes", (), 900) in fake.calls  # the fake's last price update is long ago
    assert "14 neu" in out
    fake.calls.clear()
    code, out = run("pp-update", "--no-quotes")
    assert code == 0 and "Buch ist aktuell" in out
    assert not [c for c in fake.calls if c[0] == "quotes"]
    fake.export_data = make_export(revision="r2")  # new revision, same content
    code, out = run("pp-update", "--no-quotes")
    assert code == 0 and "14 unverändert" in out


def test_pp_status(run):
    code, out = run("pp-status")
    assert code == 0 and "Portfolio Performance 0.88.0" in out and "Übernahme an" in out


def test_pp_core_down_fails_the_timer(run, fake, monkeypatch):
    def boom(*a, **k):
        raise PPCoreError("pp-core nicht erreichbar (http://pp-core.test): Connection refused")
    monkeypatch.setattr(fake, "summary", boom)
    code, out = run("pp-update")
    assert code == 1 and "nicht erreichbar" in out


def test_a_running_sync_elsewhere_is_no_failure(run, cfg, monkeypatch):
    import fcntl

    from gnubook.pp.service import PPService

    original = PPService.sync
    monkeypatch.setattr(PPService, "sync", lambda self, *a, **k: original(self, *a, **{**k, "wait": 0.2}))
    with open(Path(cfg.app.data_dir) / f"pp-sync-{run.ctx.id}.lock", "a") as other:  # e.g. the web app
        fcntl.flock(other, fcntl.LOCK_EX)
        code, out = run("pp-update", "--no-quotes")
    assert code == 0 and "läuft gerade" in out and not run.ctx.appdb.pp_records()
