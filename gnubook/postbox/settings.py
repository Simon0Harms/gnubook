"""Settings of one postbox source (stored as JSON in system.sqlite, table postbox_sources)."""
from __future__ import annotations

import json
import re
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
    # own Nextcloud share link (folder shared with edit permission) instead of the owner's Nextcloud; nc_folder and
    # the folders below are then inside the shared folder ('' = its top). Password encrypted (crypto.SecretBox).
    nc_share_url: str = ""
    nc_share_secret: str = ""
    nc_share_legacy: bool = False
    # folders for daily and weekly account statements; '' = nc_folder
    nc_daily_folder: str = ""
    nc_weekly_folder: str = ""

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

    @property
    def nc_on(self) -> bool:
        """A copy goes to Nextcloud: a folder is set, or an own share link (then its top folder is enough)."""
        return bool(self.nc_folder or self.nc_share_url)

    def nc_base_folder(self, title: str | None) -> str:
        """Folder for a document by its title: daily or weekly statements may have their own folder."""
        kind = statement_kind(title)
        if kind == "daily" and self.nc_daily_folder:
            return self.nc_daily_folder
        if kind == "weekly" and self.nc_weekly_folder:
            return self.nc_weekly_folder
        return self.nc_folder

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


_DAILY = re.compile(r"tages(konto)?auszug|t(ä|ae)glich", re.I)
_WEEKLY = re.compile(r"wochen(konto)?auszug|w(ö|oe)chentlich", re.I)


def statement_kind(title: str | None) -> str | None:
    """'daily' for a daily account statement (finanzen.net zero: "Tageskontoauszug: EUR-Konto …"), 'weekly' for a
    weekly one (title assumed to contain "Wochen…auszug" or "wöchentlich"), else None."""
    title = title or ""
    if _DAILY.search(title):
        return "daily"
    if _WEEKLY.search(title):
        return "weekly"
    return None
