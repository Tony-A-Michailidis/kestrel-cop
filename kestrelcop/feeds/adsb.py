"""ADS-B aircraft: adsb.lol (no key), OpenSky Network, or your own readsb/dump1090/tar1090 aircraft.json.

The adsb.lol v2 API and readsb's aircraft.json share the same record format, so one parser covers
both the public API and a local RTL-SDR receiver.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..cot.sidc import adsb_function, air_codes
from ..models import Track, utcnow
from .base import Feed, bbox

FT_TO_M = 0.3048

# Callsign prefixes that are reliably government/military in US airspace. Conservative on purpose.
MIL_CALLSIGN_PREFIXES = ("RCH", "REACH", "EVAC", "NAVY", "CNV", "VV", "VM", "PAT", "SAM", "ARMY", "USCG", "CG")
# ICAO 24-bit address block allocated to US military aircraft
US_MIL_HEX_RANGE = (0xADF7C8, 0xAFFFFF)


def classify_aircraft(hex_code: str, callsign: str | None, category: str | None) -> tuple[str, bool]:
    """Returns (affiliation, military)."""
    military = False
    try:
        value = int(hex_code, 16)
        military = US_MIL_HEX_RANGE[0] <= value <= US_MIL_HEX_RANGE[1]
    except (TypeError, ValueError):
        pass
    cs = (callsign or "").strip().upper()
    if cs.startswith(MIL_CALLSIGN_PREFIXES):
        military = True
    if military:
        return "friend", True
    if not cs and category in (None, "", "A0"):
        return "unknown", False
    return "neutral", False


def track_from_readsb(ac: dict, source: str = "adsb", now: datetime | None = None) -> Track | None:
    """Build a Track from one readsb / adsb.lol aircraft record. Returns None if there is no position."""
    now = now or utcnow()
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None:
        return None
    seen_pos = float(ac.get("seen_pos") or 0.0)
    if seen_pos > 90:
        return None
    hex_code = str(ac.get("hex", "")).lower().strip("~")
    callsign = (ac.get("flight") or "").strip() or None
    category = ac.get("category")
    alt = ac.get("alt_baro")
    if alt == "ground":
        alt_m = 0.0
    else:
        alt_m = float(alt) * FT_TO_M if alt is not None else (float(ac["alt_geom"]) * FT_TO_M if ac.get("alt_geom") is not None else None)
    affiliation, military = classify_aircraft(hex_code, callsign, category)
    function = adsb_function(category, military)
    sidc, cot_type = air_codes(affiliation, function)
    attrs = {
        "hex": hex_code,
        "squawk": ac.get("squawk"),
        "category": category,
        "registration": ac.get("r"),
        "aircraft_type": ac.get("t"),
        "vertical_rate_fpm": ac.get("baro_rate"),
        "alt_ft": None if alt in (None, "ground") else alt,
        "on_ground": alt == "ground",
        "rssi": ac.get("rssi"),
        "military": military,
    }
    return Track(
        uid=f"adsb-{hex_code}", domain="air", source=source, callsign=callsign,
        lat=float(lat), lon=float(lon), alt_m=alt_m,
        speed_kts=float(ac["gs"]) if ac.get("gs") is not None else None,
        course_deg=float(ac["track"]) if ac.get("track") is not None else None,
        heading_deg=float(ac["true_heading"]) if ac.get("true_heading") is not None else None,
        affiliation=affiliation, function=function, sidc=sidc, cot_type=cot_type,
        attrs={k: v for k, v in attrs.items() if v is not None},
        flags=["MILITARY"] if military else [],
        ts=now - timedelta(seconds=seen_pos),
    )


def track_from_opensky(state: list, now: datetime | None = None) -> Track | None:
    now = now or utcnow()
    if len(state) < 17 or state[5] is None or state[6] is None:
        return None
    hex_code = str(state[0]).lower()
    callsign = (state[1] or "").strip() or None
    category_map = {2: "A1", 3: "A2", 4: "A3", 5: "A4", 6: "A5", 7: "A6", 8: "A7"}
    category = category_map.get(state[17] if len(state) > 17 else None)
    affiliation, military = classify_aircraft(hex_code, callsign, category)
    function = adsb_function(category, military)
    sidc, cot_type = air_codes(affiliation, function)
    ts = datetime.fromtimestamp(state[3] or state[4], tz=timezone.utc) if (state[3] or state[4]) else now
    return Track(
        uid=f"adsb-{hex_code}", domain="air", source="adsb", callsign=callsign,
        lat=float(state[6]), lon=float(state[5]),
        alt_m=float(state[7]) if state[7] is not None else (float(state[13]) if state[13] is not None else None),
        speed_kts=float(state[9]) / 0.514444 if state[9] is not None else None,
        course_deg=float(state[10]) if state[10] is not None else None,
        affiliation=affiliation, function=function, sidc=sidc, cot_type=cot_type,
        attrs={k: v for k, v in {
            "hex": hex_code, "squawk": state[14], "category": category, "origin_country": state[2],
            "on_ground": bool(state[8]), "vertical_rate_fpm": (state[11] * 196.85 if state[11] is not None else None),
            "military": military,
        }.items() if v is not None},
        flags=["MILITARY"] if military else [],
        ts=ts,
    )


class AdsbFeed(Feed):
    name = "adsb"
    label = "ADS-B aircraft"

    def __init__(self, ctx, settings) -> None:
        super().__init__(ctx, settings, poll_seconds=settings.feeds.adsb.poll_seconds)
        self.cfg = settings.feeds.adsb
        self._opensky_token: str | None = None

    def _url(self) -> str:
        lat, lon = self.settings.area.center
        if self.cfg.provider == "url" and self.cfg.url:
            return self.cfg.url
        if self.cfg.provider == "opensky":
            s, w, n, e = bbox(self.settings.area.center, self.settings.area.radius_km)
            return f"https://opensky-network.org/api/states/all?lamin={s:.3f}&lomin={w:.3f}&lamax={n:.3f}&lomax={e:.3f}"
        dist = min(250, max(1, int(self.settings.area.radius_nm)))
        return f"https://api.adsb.lol/v2/lat/{lat:.4f}/lon/{lon:.4f}/dist/{dist}"

    async def _opensky_headers(self) -> dict[str, str]:
        if not (self.cfg.opensky_client_id and self.cfg.opensky_client_secret):
            return {}
        if self._opensky_token is None:
            resp = await self.http.post(
                "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
                data={"grant_type": "client_credentials", "client_id": self.cfg.opensky_client_id,
                      "client_secret": self.cfg.opensky_client_secret},
            )
            resp.raise_for_status()
            self._opensky_token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {self._opensky_token}"}

    async def poll(self) -> int:
        url = self._url()
        headers = await self._opensky_headers() if self.cfg.provider == "opensky" else {}
        resp = await self.http.get(url, headers=headers)
        if resp.status_code == 401 and self.cfg.provider == "opensky":
            self._opensky_token = None
        resp.raise_for_status()
        data = resp.json()
        now = utcnow()
        count = 0
        if self.cfg.provider == "opensky":
            for state in data.get("states") or []:
                t = track_from_opensky(state, now)
                if t:
                    self.emit_track(t)
                    count += 1
        else:
            for ac in data.get("ac") or data.get("aircraft") or []:
                t = track_from_readsb(ac, now=now)
                if t:
                    self.emit_track(t)
                    count += 1
        return count
