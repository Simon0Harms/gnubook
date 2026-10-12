"""Which postbox documents to fetch and where a run stops listing. No I/O, so it is tested directly.

The finanzen.net zero postbox lists documents newest first by posting day, ten at a time ("Mehr anzeigen").
Within one day the order is not by recency (the ids rise within a day), and the user can archive or delete a
document at any time. Stopping at "the document fetched last" would therefore miss documents posted later on the
same day, or click through the whole postbox once that document is gone. A run instead lists until a whole newly
loaded batch is older than the stop day, and fetches every listed document whose id it does not know yet:

    stop day = max(since, newest completely listed day − overlap)     (since alone before the first complete run)

"Completely listed" means the run reached the stop day or the end of the list; a run that hit the click limit or
whose "Mehr anzeigen" stopped loading does not move the mark, so the next run lists the same range again.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Listed:
    """One entry of the postbox list."""
    external_id: str
    title: str
    day: date | None
    href: str
    raw_day: str = ""


_DATE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)")


def parse_day(text: str | None, today: date | None = None) -> date | None:
    """'10.10.26', '10.10.2026', 'Heute', 'Gestern' -> date; None when not recognised."""
    t = (text or "").strip().lower()
    today = today or date.today()
    if t.startswith("heute") or t.startswith("today"):
        return today
    if t.startswith("gestern") or t.startswith("yesterday"):
        return today - timedelta(days=1)
    m = _DATE.search(t)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def stop_day(since: date | None, complete_day: date | None, overlap: int, rescan: date | None = None) -> date | None:
    """Oldest posting day a run has to list. None means: no limit (the caller refuses to run without `since`)."""
    if complete_day is None:
        stop = since
    else:
        stop = complete_day - timedelta(days=max(0, overlap))
        if since is not None and stop < since:
            stop = since
    if rescan is not None and (stop is None or rescan < stop):
        stop = rescan if since is None or rescan >= since else since
    return stop


def batch_done(batch: list[Listed], stop: date | None) -> bool:
    """True when every dated entry of a newly loaded batch is older than the stop day."""
    if stop is None:
        return False
    days = [d.day for d in batch if d.day is not None]
    return bool(days) and all(day < stop for day in days)


def select(listed: list[Listed], known: set[str], since: date | None) -> list[Listed]:
    """Entries to download: unknown ids, not older than `since` (entries without a readable day are taken)."""
    out, seen = [], set()
    for d in listed:
        if not d.external_id or not d.href or d.external_id in known or d.external_id in seen:
            continue
        if since is not None and d.day is not None and d.day < since:
            continue
        seen.add(d.external_id)
        out.append(d)
    return out


def complete_day_after(listed: list[Listed], failed_days, previous: date | None) -> date | None:
    """New "completely listed" mark after a complete run: the newest listed day – but not past a document whose
    download failed, so that the next run lists it again."""
    days = [d.day for d in listed if d.day is not None]
    if not days:
        return previous
    candidates = [max(days)] + [d for d in failed_days if d is not None]
    return min(candidates)


_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def file_name(day: date | None, external_id: str, title: str) -> str:
    """'2026-10-09_246891282_Wertpapierabrechnung-Kauf-DE0008430026.pdf' – ASCII only, so pp-core keeps the
    name unchanged (it becomes the source of the PP transactions, "Beleg: …")."""
    t = unicodedata.normalize("NFKD", (title or "").translate(_TRANS)).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-")[:80].rstrip("-") or "Dokument"
    ident = re.sub(r"[^A-Za-z0-9]+", "", external_id or "")[:40] or "x"
    return f"{day.isoformat() if day else 'ohne-Datum'}_{ident}_{t}.pdf"
