"""Settings loaded from config/airports.yml and environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "airports.yml"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so local runs work without an extra dependency.

    Existing environment variables win, so CI secrets are never overridden.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Airport:
    icao: str
    name: str
    notam_source: str | None = None  # None: no NOTAM feed


@dataclass(frozen=True)
class Settings:
    airports: list[Airport]
    opensky: dict = field(default_factory=dict)
    rapidapi: dict = field(default_factory=dict)

    @property
    def icao_codes(self) -> list[str]:
        return [a.icao for a in self.airports]

    def airports_for_notam_source(self, source: str) -> list[str]:
        return [a.icao for a in self.airports if a.notam_source == source]

    @property
    def warehouse(self) -> str:
        """DuckDB file path (relative paths resolve from the repo root) or md:<db>."""
        target = os.environ.get("WAREHOUSE", "").strip() or "data/aviation.duckdb"
        if target.startswith("md:") or Path(target).is_absolute():
            return target
        return str(REPO_ROOT / target)


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    _load_dotenv(REPO_ROOT / ".env")
    raw = yaml.safe_load(path.read_text())
    return Settings(
        airports=[Airport(**a) for a in raw["airports"]],
        opensky=raw.get("opensky", {}),
        rapidapi=raw.get("rapidapi", {}),
    )


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Environment variable {name} is not set. Copy .env.example to .env and fill it in, "
            f"or add it as a GitHub Actions secret."
        )
    return value
