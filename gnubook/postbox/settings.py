"""Settings of one postbox source (stored as JSON in system.sqlite, table postbox_sources)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date


@dataclass
class SourceSettings:
    # take part in the scheduled runs (gnubook-postbox.timer); manual runs work without
    enabled: bool = False
    # documents posted on or after this day (YYYY-MM-DD) are fetched; older ones are never touched
    since: str = ""
    # days before the newest completely listed day that every run lists again (late or out-of-order postings)
    overlap_days: int = 3
    # one-time: the next complete run lists back to this day again (YYYY-MM-DD), then it is cleared
    rescan_from: str = ""
    # import every new document into Portfolio Performance (pp-core)
    import_pp: bool = True
    # Nextcloud folder of the source's owner for a copy of every document; empty = no copy
    nc_folder: str = ""
    # one sub folder per year (posting date) below nc_folder
    nc_year_folders: bool = True

    @classmethod
    def from_json(cls, text: str | None) -> "SourceSettings":
        try:
            data = json.loads(text or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        s = cls(**known)
        try:
            s.overlap_days = max(0, min(int(s.overlap_days), 60))
        except (TypeError, ValueError):
            s.overlap_days = 3
        return s

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _day(text: str) -> date | None:
        try:
            return date.fromisoformat(text) if text else None
        except ValueError:
            return None

    @property
    def since_day(self) -> date | None:
        return self._day(self.since)

    @property
    def rescan_day(self) -> date | None:
        return self._day(self.rescan_from)
