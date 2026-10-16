"""Settings of one postbox source (stored as JSON in system.sqlite, table postbox_sources)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date


SUBFOLDERS = ("", "year", "week", "day", "ymd")


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
    # one sub folder per year (posting date) below nc_folder (old setting, read when nc_subfolders is not set)
    nc_year_folders: bool = True
    # sub folders by posting date below nc_folder: "" (none), "year" (2026), "week" (2026/KW41), "day" (2026/2026-10-09), "ymd" (2026/10/09);
    # None = from nc_year_folders
    nc_subfolders: str | None = None
    # delete the PDF kept in data/postbox once it is in Nextcloud and nothing is left to do for PP
    delete_local: bool = True
    # own Nextcloud share link (folder shared with edit permission) instead of the owner's Nextcloud; nc_folder and
    # the folders below are then inside the shared folder ('' = its top). Password encrypted (crypto.SecretBox).
    nc_share_url: str = ""
    nc_share_secret: str = ""
    nc_share_legacy: bool = False

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
        if s.nc_subfolders not in SUBFOLDERS:
            s.nc_subfolders = "year" if s.nc_year_folders else ""
        try:
            s.overlap_days = max(0, min(int(s.overlap_days), 60))
        except (TypeError, ValueError):
            s.overlap_days = 3
        return s

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    def nc_subfolder(self, day: date | None) -> str:
        """Sub folder below nc_folder for a document posted on `day` ('' = none)."""
        if not self.nc_subfolders:
            return ""
        if day is None:
            return "ohne-Datum"
        if self.nc_subfolders == "week":
            year, week, _ = day.isocalendar()
            return f"{year}/KW{week:02d}"
        if self.nc_subfolders == "ymd":
            return f"{day:%Y/%m/%d}"
        if self.nc_subfolders == "day":
            return f"{day:%Y}/{day.isoformat()}"
        return f"{day:%Y}"

    @property
    def nc_on(self) -> bool:
        """A copy goes to Nextcloud: a folder is set, or an own share link (then its top folder is enough)."""
        return bool(self.nc_folder or self.nc_share_url)

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

