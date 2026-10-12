"""Run requests from the web app ("Jetzt abrufen").

The web app does not start the browser itself (it runs hardened, as gunicorn worker). It writes a small JSON file
into data_dir/postbox/requests; the systemd unit gnubook-postbox-request.path starts gnubook-postbox-request.service
as long as such a file exists, and `gnubook postbox-fetch --requests` takes and deletes them.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

MODES = ("fetch", "dry-run", "process")


def request_dir(data_dir) -> Path:
    return Path(data_dir) / "postbox" / "requests"


def add_request(data_dir, book_id: int, source: str, mode: str, actor: str) -> Path:
    if mode not in MODES:
        raise ValueError(mode)
    d = request_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{int(book_id)}-{time.time_ns()}-{secrets.token_hex(3)}"
    tmp = d / f".{name}.tmp"
    tmp.write_text(json.dumps({"book": int(book_id), "source": source, "mode": mode, "actor": actor,
                               "at": time.time()}), encoding="utf-8")
    final = d / f"{name}.json"
    os.replace(tmp, final)  # the path unit only ever sees complete files
    return final


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and str(data.get("book", "")).isdigit() and data.get("mode") in MODES:
            return data
    except (OSError, ValueError):
        pass
    return None


def pending(data_dir, book_id: int | None = None) -> list[dict]:
    out = []
    for p in sorted(request_dir(data_dir).glob("*.json")):
        data = _read(p)
        if data is not None and (book_id is None or int(data["book"]) == int(book_id)):
            out.append(data)
    return out


def take(data_dir) -> list[dict]:
    """All requests, deleted from disk (also malformed ones, which are dropped)."""
    out = []
    for p in sorted(request_dir(data_dir).glob("*.json")):
        data = _read(p)
        try:
            p.unlink()
        except FileNotFoundError:
            continue  # taken by another process
        except OSError:
            continue
        if data is not None:
            out.append(data)
    return out


def merge(requests: list[dict]) -> dict[tuple[int, str], list[str]]:
    """(book, source) -> modes to run in this order: a real fetch covers a dry run and the processing; otherwise
    the processing first, then the dry run."""
    wanted: dict[tuple[int, str], set] = {}
    for r in requests:
        wanted.setdefault((int(r["book"]), str(r.get("source") or "")), set()).add(r["mode"])
    return {key: ["fetch"] if "fetch" in modes else [m for m in ("process", "dry-run") if m in modes]
            for key, modes in wanted.items()}
