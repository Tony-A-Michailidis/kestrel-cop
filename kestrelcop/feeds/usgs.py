"""USGS earthquakes: regional events above min_magnitude plus anything large anywhere on earth."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..cot.sidc import EVENT_COT_TYPES
from ..geo import haversine_km
from ..models import Event
from .base import Feed


def event_from_feature(feature: dict) -> Event | None:
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []
    if len(coords) < 2 or props.get("mag") is None:
        return None
    ts = datetime.fromtimestamp((props.get("time") or 0) / 1000, tz=timezone.utc)
    mag = float(props["mag"])
    return Event(
        uid=f"usgs-{feature.get('id')}", kind="quake", source="usgs",
        title=props.get("place") or "Earthquake", lat=float(coords[1]), lon=float(coords[0]),
        magnitude=mag, severity="high" if mag >= 6 else ("moderate" if mag >= 4.5 else "low"),
        cot_type=EVENT_COT_TYPES["quake"],
        attrs={k: v for k, v in {
            "depth_km": round(float(coords[2]), 1) if len(coords) > 2 and coords[2] is not None else None,
            "tsunami": props.get("tsunami"), "url": props.get("url"), "felt": props.get("felt"),
        }.items() if v is not None},
        ts=ts, expires=ts + timedelta(days=3),
    )


class UsgsFeed(Feed):
    name = "usgs"
    label = "USGS earthquakes"

    def __init__(self, ctx, settings) -> None:
        super().__init__(ctx, settings, poll_seconds=settings.feeds.usgs.poll_seconds)
        self.cfg = settings.feeds.usgs

    def _feed_name(self) -> str:
        m = self.cfg.min_magnitude
        if m >= 4.5:
            return "4.5_day"
        if m >= 2.5:
            return "2.5_day"
        if m >= 1.0:
            return "1.0_day"
        return "all_day"

    async def poll(self) -> int:
        url = f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{self._feed_name()}.geojson"
        resp = await self.http.get(url)
        resp.raise_for_status()
        clat, clon = self.settings.area.center
        reach_km = self.settings.area.radius_km * 3
        count = 0
        for feature in resp.json().get("features", []):
            ev = event_from_feature(feature)
            if not ev:
                continue
            regional = haversine_km(clat, clon, ev.lat, ev.lon) <= reach_km and (ev.magnitude or 0) >= self.cfg.min_magnitude
            if regional or (ev.magnitude or 0) >= self.cfg.global_min_magnitude:
                ev.attrs["regional"] = regional
                self.emit_event(ev)
                count += 1
        return count
