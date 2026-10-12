"""Loads bench.toml, the ignored per-user settings; bench.example.toml is its template."""

import datetime as dt
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "bench.toml"


@dataclass(frozen=True)
class Config:
    cwds: tuple[str, ...]
    since: dt.date | None
    until: dt.date | None


class ConfigError(Exception):
    pass


def _require(table: dict, key: str, where: str):
    if key not in table:
        raise ConfigError(f"{where}: missing key '{key}'")
    return table[key]


def _date(value, name: str) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise ConfigError(f"{name} must be a TOML date such as 2026-06-13, got {value!r}")
    return value


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.is_file():
        raise ConfigError(f"config not found: {path} (copy bench.example.toml to bench.toml)")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    extract = _require(data, "extract", str(path))
    cwds = _require(extract, "cwds", f"{path} [extract]")
    if not isinstance(cwds, list) or not cwds or not all(isinstance(c, str) and c for c in cwds):
        raise ConfigError(f"{path} [extract] cwds must be a non-empty list of paths")
    return Config(
        cwds=tuple(c.lower().rstrip("\\/") for c in cwds),
        since=_date(extract.get("since"), "since"),
        until=_date(extract.get("until"), "until"),
    )
