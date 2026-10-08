"""Configuration loading (TOML file + environment overrides).

Lookup order for the file: $GNUBOOK_CONFIG, /etc/gnubook/config.toml, ./config.toml.
Environment variables GNUBOOK_<SECTION>_<KEY> override single values, e.g.
GNUBOOK_BOOK_URL or GNUBOOK_APP_SECRET_KEY.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATHS = ("/etc/gnubook/config.toml", "config.toml")


class ConfigError(RuntimeError):
    pass


@dataclass
class BookConfig:
    url: str = ""
    timezone: str = "Europe/Berlin"
    account_separator: str = ":"


@dataclass
class AppConfig:
    secret_key: str = ""
    data_dir: str = "data"
    username: str = "admin"
    password_hash: str = ""
    session_cookie_secure: bool = False
    behind_proxy: bool = False
    session_days: int = 14
    title: str = "gnubook"


@dataclass
class ApiConfig:
    # sha256 hex digest of the API token (see `gnubook gen-token`)
    token_sha256: str = ""
    # Report IBANs in GET /api/v1/accounts. Keep this off: the FinTS importer then always sends
    # withdrawals/deposits and gnubook recognises own-account transfers itself.
    expose_iban: bool = False


@dataclass
class ImportConfig:
    # account that receives the counter split when nothing better is known (full name or GUID)
    fallback_account: str = ""
    # optional transit account for transfers between two imported bank accounts
    transit_account: str = ""
    transit_between: list[str] = field(default_factory=list)
    # restrict the accounts offered to the importer (full names); empty = all bank/asset/cash/credit accounts
    accounts: list[str] = field(default_factory=list)
    # IBAN -> account (full name) for counterparties that are your own accounts
    iban_map: dict[str, str] = field(default_factory=dict)
    # days around the booking date in which an existing, not yet imported booking counts as the same one
    match_days: int = 3
    transfer_match_days: int = 7


@dataclass
class Config:
    book: BookConfig = field(default_factory=BookConfig)
    app: AppConfig = field(default_factory=AppConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    importer: ImportConfig = field(default_factory=ImportConfig)
    source: str = ""

    @property
    def data_path(self) -> Path:
        return Path(self.app.data_dir)


def _coerce(current, raw: str):
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on", "ja")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def _apply(section_obj, values: dict, section_name: str):
    for key, value in values.items():
        if not hasattr(section_obj, key):
            raise ConfigError(f"Unbekannter Konfigurationsschlüssel [{section_name}] {key}")
        setattr(section_obj, key, value)


def load_config(path: str | os.PathLike | None = None, env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    cfg = Config()
    candidates = [path] if path else ([env["GNUBOOK_CONFIG"]] if env.get("GNUBOOK_CONFIG") else list(DEFAULT_PATHS))
    for cand in candidates:
        if cand and Path(cand).is_file():
            with open(cand, "rb") as fh:
                data = tomllib.load(fh)
            sections = {"book": cfg.book, "app": cfg.app, "api": cfg.api, "import": cfg.importer}
            for name, values in data.items():
                if name not in sections:
                    raise ConfigError(f"Unbekannter Konfigurationsabschnitt [{name}] in {cand}")
                _apply(sections[name], values, name)
            cfg.source = str(cand)
            break
    explicit = path or env.get("GNUBOOK_CONFIG")
    if explicit and not cfg.source:
        raise ConfigError(f"Konfigurationsdatei {explicit} nicht gefunden")

    for section_name, obj in (("BOOK", cfg.book), ("APP", cfg.app), ("API", cfg.api), ("IMPORT", cfg.importer)):
        for key in vars(obj):
            env_key = f"GNUBOOK_{section_name}_{key.upper()}"
            if env_key in env:
                setattr(obj, key, _coerce(getattr(obj, key), env[env_key]))
    return cfg


def validate_for_web(cfg: Config) -> list[str]:
    """Problems that prevent running the web app safely."""
    problems = []
    if not cfg.book.url:
        problems.append("[book] url fehlt")
    if not cfg.app.secret_key or len(cfg.app.secret_key) < 32:
        problems.append("[app] secret_key fehlt oder ist kürzer als 32 Zeichen")
    if not cfg.app.password_hash:
        problems.append("[app] password_hash fehlt (gnubook hash-password)")
    return problems
