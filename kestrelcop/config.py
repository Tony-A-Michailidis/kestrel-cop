"""Configuration: a YAML file with ${ENV_VAR} expansion, validated by pydantic.

Secrets never belong in the YAML. Reference environment variables instead:
    ais: { api_key: ${AISSTREAM_API_KEY} }
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) or "")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class AreaConfig(BaseModel):
    name: str = "Hampton Roads / Chesapeake approaches"
    center: tuple[float, float] = (36.95, -76.30)  # lat, lon — Norfolk, VA
    radius_nm: float = 150.0

    @property
    def radius_km(self) -> float:
        return self.radius_nm * 1.852


class AdsbConfig(BaseModel):
    enabled: bool = True
    provider: Literal["adsblol", "opensky", "url"] = "adsblol"
    url: str | None = None  # readsb/dump1090 aircraft.json when provider == "url"
    poll_seconds: float = 10.0
    opensky_client_id: str | None = None
    opensky_client_secret: str | None = None


class AisConfig(BaseModel):
    enabled: bool = True
    api_key: str | None = None  # https://aisstream.io (free)
    url: str = "wss://stream.aisstream.io/v0/stream"


class NwsConfig(BaseModel):
    enabled: bool = True
    areas: list[str] = Field(default_factory=lambda: ["VA", "MD", "NC"])
    poll_seconds: float = 180.0


class FirmsConfig(BaseModel):
    enabled: bool = False
    map_key: str | None = None  # https://firms.modaps.eosdis.nasa.gov/api/map_key/
    product: str = "VIIRS_SNPP_NRT"
    poll_seconds: float = 900.0
    days: int = 1


class UsgsConfig(BaseModel):
    enabled: bool = True
    poll_seconds: float = 180.0
    min_magnitude: float = 2.5  # within the area radius (x3)
    global_min_magnitude: float = 5.5  # anywhere on earth


class FeedsConfig(BaseModel):
    adsb: AdsbConfig = AdsbConfig()
    ais: AisConfig = AisConfig()
    nws: NwsConfig = NwsConfig()
    firms: FirmsConfig = FirmsConfig()
    usgs: UsgsConfig = UsgsConfig()


class TakConfig(BaseModel):
    enabled: bool = False
    url: str = "tcp://127.0.0.1:8087"  # tcp:// or tls:// (TAK Server 8089) or udp://
    tls_client_cert: str | None = None
    tls_client_key: str | None = None
    tls_dont_verify: bool = False
    stale_seconds_air: int = 120
    stale_seconds_sea: int = 1800
    publish_alerts: bool = True


class ZoneConfig(BaseModel):
    name: str
    type: Literal["circle", "polygon"] = "circle"
    center: tuple[float, float] | None = None
    radius_km: float | None = None
    ring: list[list[float]] | None = None  # [[lon, lat], ...]
    severity: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"


class RulesConfig(BaseModel):
    stale_feed_seconds: float = 120.0
    ais_gap_minutes: float = 10.0
    ais_gap_min_speed_kts: float = 3.0
    quake_alert_magnitude: float = 4.0
    zones: list[ZoneConfig] = Field(default_factory=list)


class AiConfig(BaseModel):
    enabled: bool = True
    model: str = "claude-sonnet-5-5"
    triage_model: str | None = None  # e.g. claude-haiku-4-5 for cheap SPOTREPs
    auto_sitrep: bool = True
    sitrep_every_minutes: float = 10.0
    sitrep_initial_delay_seconds: float = 45.0
    triage_alerts: bool = True
    max_tool_iterations: int = 8


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    basemap_style: str | None = None  # a MapLibre style URL for air-gapped/offline use


class StoreConfig(BaseModel):
    stale_seconds_air: int = 120
    stale_seconds_sea: int = 1800
    trail_length: int = 40


class DemoConfig(BaseModel):
    enabled: bool = False
    seed: int = 42
    sea_bearing_range: tuple[float, float] = (60.0, 160.0)  # where the water is, bearings from center
    scenario: bool = True  # fire the scripted drama (emergency squawk, dark ship, ROZ entry)


def area_override_path(config_path: str | Path) -> Path:
    """Where a change of area made from the console is saved: next to the config, e.g. config/kestrel.area.yaml.
    It is applied on top of the config at load time and is gitignored, so the hand-written file keeps its comments."""
    return Path(config_path).with_suffix(".area.yaml")


class Settings(BaseModel):
    area: AreaConfig = AreaConfig()
    feeds: FeedsConfig = FeedsConfig()
    tak: TakConfig = TakConfig()
    rules: RulesConfig = RulesConfig()
    ai: AiConfig = AiConfig()
    web: WebConfig = WebConfig()
    store: StoreConfig = StoreConfig()
    demo: DemoConfig = DemoConfig()
    source_path: Path | None = Field(default=None, exclude=True)  # the file these settings came from, if any

    @classmethod
    def load(cls, path: str | Path) -> "Settings":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        override = area_override_path(path)
        if override.is_file():  # the console's "move the picture" change, applied on top of the hand-written file
            extra = yaml.safe_load(override.read_text(encoding="utf-8")) or {}
            if isinstance(extra.get("area"), dict):
                raw["area"] = extra["area"]
            if isinstance(extra.get("zones"), list):
                raw.setdefault("rules", {})["zones"] = extra["zones"]
        settings = cls.model_validate(_expand_env(raw))
        settings.source_path = Path(path)
        return settings

    def save_area_override(self) -> Path | None:
        """Persist the current area and zones so a restart keeps them. Returns the file written, or None when the
        settings did not come from a file (the demo world), in which case the change lasts for this run only."""
        if not self.source_path:
            return None
        path = area_override_path(self.source_path)
        data = {
            "area": {"name": self.area.name, "center": [float(self.area.center[0]), float(self.area.center[1])],
                     "radius_nm": float(self.area.radius_nm)},
            "zones": [json.loads(z.model_dump_json(exclude_none=True)) for z in self.rules.zones],
        }
        path.write_text(
            f"# Written by Kestrel when the area is changed from the console; applied on top of {self.source_path.name}\n"
            "# at startup. Delete this file to return to the area and zones in the config.\n"
            + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    @classmethod
    def for_demo(cls, center: tuple[float, float] | None = None, tak_url: str | None = None) -> "Settings":
        s = cls()
        s.demo.enabled = True
        if center:
            s.area.center = center
        s.rules.ais_gap_minutes = 2.0
        s.ai.sitrep_every_minutes = 3.0
        s.ai.sitrep_initial_delay_seconds = 30.0
        s.rules.zones = [
            ZoneConfig(name="ROZ ALPHA", type="circle", center=(36.85, -75.70), radius_km=18.0, severity="MEDIUM"),
        ]
        if tak_url:
            s.tak.enabled = True
            s.tak.url = tak_url
        return s


EXAMPLE_YAML = """# Kestrel COP configuration. Secrets come from the environment: ${VAR} is expanded at load time.
area:
  name: Hampton Roads / Chesapeake approaches
  center: [36.95, -76.30]      # lat, lon
  radius_nm: 150

feeds:
  adsb:
    enabled: true
    provider: adsblol          # adsblol (no key) | opensky | url (your own readsb/dump1090 aircraft.json)
    # url: http://192.168.1.50/tar1090/data/aircraft.json
    poll_seconds: 10
  ais:
    enabled: true
    api_key: ${AISSTREAM_API_KEY}   # free key from https://aisstream.io
  nws:
    enabled: true
    areas: [VA, MD, NC]
    poll_seconds: 180
  firms:
    enabled: false
    map_key: ${FIRMS_MAP_KEY}       # free key from https://firms.modaps.eosdis.nasa.gov/api/map_key/
    product: VIIRS_SNPP_NRT
    poll_seconds: 900
  usgs:
    enabled: true
    poll_seconds: 180
    min_magnitude: 2.5
    global_min_magnitude: 5.5

tak:
  enabled: false
  url: tcp://127.0.0.1:8087        # TAK Server: tls://host:8089 with the certs below
  # tls_client_cert: certs/kestrel.pem
  # tls_client_key: certs/kestrel.key
  # tls_dont_verify: true
  publish_alerts: true

rules:
  stale_feed_seconds: 120
  ais_gap_minutes: 10
  quake_alert_magnitude: 4.0
  zones:
    - name: ROZ ALPHA
      type: circle
      center: [36.85, -75.70]
      radius_km: 18
      severity: MEDIUM

ai:
  enabled: true
  model: claude-sonnet-5-5         # ANTHROPIC_API_KEY in the environment; without it Kestrel runs the mock watch officer
  triage_model: claude-haiku-4-5-20251001   # cheaper model for alert SPOTREPs (optional)
  auto_sitrep: true
  sitrep_every_minutes: 10
  triage_alerts: true

web:
  host: 127.0.0.1
  port: 8000
  # basemap_style: http://tileserver.local/styles/dark/style.json   # for offline / air-gapped use
"""
