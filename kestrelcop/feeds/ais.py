"""AIS vessels from aisstream.io (free API key, WebSocket). Position reports and static data are
merged per MMSI so a vessel carries its name, type and destination once they have been heard."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from ..cot.sidc import ais_function, sea_codes
from ..models import Track, utcnow
from .base import Feed, bbox

log = logging.getLogger("kestrel.feeds.ais")

NAV_STATUS = {
    0: "under way using engine", 1: "at anchor", 2: "not under command", 3: "restricted manoeuvrability",
    4: "constrained by draught", 5: "moored", 6: "aground", 7: "engaged in fishing", 8: "under way sailing",
    11: "towing astern", 12: "pushing ahead", 14: "AIS-SART", 15: "undefined",
}

LAW_ENFORCEMENT_HINTS = ("USCG", "COAST GUARD", "CG ", "POLICE", "CUSTOMS")


def classify_vessel(ship_type: int | None, name: str | None) -> tuple[str, str]:
    """Returns (affiliation, function)."""
    function = ais_function(ship_type)
    upper = (name or "").upper()
    if any(h in upper for h in LAW_ENFORCEMENT_HINTS) or function == "law_enforcement":
        return "friend", "law_enforcement"
    if function == "combatant":
        return "unknown", "combatant"
    if function == "unknown":
        return "unknown", "unknown"
    return "neutral", function


def _parse_time(value: str | None) -> datetime:
    # aisstream: "2024-05-01 12:34:56.789 +0000 UTC"
    if value:
        try:
            core = value.split(" +")[0]
            return datetime.strptime(core[:23], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return utcnow()


class AisFeed(Feed):
    name = "ais"
    label = "AIS vessels"

    def __init__(self, ctx, settings) -> None:
        super().__init__(ctx, settings, poll_seconds=5.0)
        self.cfg = settings.feeds.ais
        self.static: dict[int, dict] = {}
        self.count = 0

    def _subscription(self) -> dict:
        s, w, n, e = bbox(self.settings.area.center, self.settings.area.radius_km)
        return {
            "APIKey": self.cfg.api_key,
            "BoundingBoxes": [[[s, w], [n, e]]],
            "FilterMessageTypes": ["PositionReport", "ShipStaticData", "StandardClassBPositionReport"],
        }

    def handle(self, msg: dict) -> None:
        mtype = msg.get("MessageType")
        meta = msg.get("MetaData") or {}
        mmsi = meta.get("MMSI")
        if mmsi is None:
            return
        if mtype == "ShipStaticData":
            data = msg.get("Message", {}).get("ShipStaticData", {})
            self.static[int(mmsi)] = {
                "ship_type": data.get("Type"),
                "destination": (data.get("Destination") or "").strip() or None,
                "name": (data.get("Name") or meta.get("ShipName") or "").strip() or None,
                "callsign_radio": (data.get("CallSign") or "").strip() or None,
                "imo": data.get("ImoNumber"),
            }
            return
        if mtype not in ("PositionReport", "StandardClassBPositionReport"):
            return
        body = msg.get("Message", {}).get(mtype, {})
        lat = body.get("Latitude", meta.get("latitude"))
        lon = body.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None:
            return
        static = self.static.get(int(mmsi), {})
        name = static.get("name") or (meta.get("ShipName") or "").strip() or None
        ship_type = static.get("ship_type")
        affiliation, function = classify_vessel(ship_type, name)
        sidc, cot_type = sea_codes(affiliation, function)
        cog = body.get("Cog")
        hdg = body.get("TrueHeading")
        nav = body.get("NavigationalStatus")
        self.emit_track(Track(
            uid=f"ais-{mmsi}", domain="sea", source="ais", callsign=name or f"MMSI {mmsi}",
            lat=float(lat), lon=float(lon), alt_m=0.0,
            speed_kts=float(body["Sog"]) if body.get("Sog") is not None else None,
            course_deg=float(cog) if cog is not None and cog < 360 else None,
            heading_deg=float(hdg) if hdg is not None and hdg != 511 else None,
            affiliation=affiliation, function=function, sidc=sidc, cot_type=cot_type,
            attrs={k: v for k, v in {
                "mmsi": mmsi, "ship_type": ship_type, "destination": static.get("destination"),
                "nav_status": NAV_STATUS.get(nav) if nav is not None else None,
                "imo": static.get("imo"), "class_b": mtype == "StandardClassBPositionReport",
            }.items() if v is not None},
            ts=_parse_time(meta.get("time_utc")),
        ))
        self.count += 1

    async def run(self) -> None:
        if not self.cfg.api_key:
            self.ctx.feed_off(self.name, self.label, "no API key — set AISSTREAM_API_KEY (free at aisstream.io)")
            return
        try:
            import websockets  # optional dependency, only needed for the live AIS stream
        except ImportError:
            self.ctx.feed_off(self.name, self.label, "python package 'websockets' is not installed")
            return
        self.ctx.feed_starting(self.name, self.label)
        backoff = 3.0
        while True:
            try:
                async with websockets.connect(self.cfg.url, max_queue=1024) as ws:
                    await ws.send(json.dumps(self._subscription()))
                    self.ctx.feed_ok(self.name, self.label, 0, detail="connected")
                    backoff = 3.0
                    last_report = asyncio.get_event_loop().time()
                    async for raw in ws:
                        try:
                            self.handle(json.loads(raw))
                        except Exception as exc:  # noqa: BLE001
                            log.debug("bad AIS message: %s", exc)
                        now = asyncio.get_event_loop().time()
                        if now - last_report > 5:
                            self.ctx.feed_ok(self.name, self.label, self.count)
                            self.count = 0
                            last_report = now
                    self.ctx.feed_error(self.name, self.label, "stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("ais: %s", msg)
                self.ctx.feed_error(self.name, self.label, msg)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
