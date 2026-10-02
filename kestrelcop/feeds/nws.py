"""NWS active weather alerts (api.weather.gov, CAP-derived GeoJSON). Only alerts with a geometry are
mapped; zone-only alerts would need the zone boundaries, which is a follow-up."""

from __future__ import annotations

import hashlib
from datetime import datetime

from ..cot.sidc import EVENT_COT_TYPES
from ..geo import geometry_centroid
from ..models import Event, utcnow
from .base import Feed


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def event_from_feature(feature: dict) -> Event | None:
    props = feature.get("properties") or {}
    geometry = feature.get("geometry")
    if not geometry:
        return None
    lat, lon = geometry_centroid(geometry)
    raw_id = props.get("id") or props.get("@id") or ""
    uid = "nws-" + hashlib.sha1(raw_id.encode()).hexdigest()[:10]
    expires = _dt(props.get("ends")) or _dt(props.get("expires"))
    return Event(
        uid=uid, kind="alert", source="nws", title=props.get("event") or "Weather alert",
        lat=lat, lon=lon, geometry=geometry, severity=props.get("severity"),
        cot_type=EVENT_COT_TYPES["alert"],
        attrs={k: v for k, v in {
            "headline": props.get("headline"),
            "area": props.get("areaDesc"),
            "urgency": props.get("urgency"),
            "certainty": props.get("certainty"),
            "sender": props.get("senderName"),
            "instruction": (props.get("instruction") or "")[:300] or None,
            "description": (props.get("description") or "")[:600] or None,
        }.items() if v},
        ts=_dt(props.get("sent")) or utcnow(),
        expires=expires,
    )


class NwsFeed(Feed):
    name = "nws"
    label = "NWS weather alerts"

    def __init__(self, ctx, settings) -> None:
        super().__init__(ctx, settings, poll_seconds=settings.feeds.nws.poll_seconds)
        self.cfg = settings.feeds.nws

    async def poll(self) -> int:
        area = ",".join(self.cfg.areas)
        resp = await self.http.get(
            "https://api.weather.gov/alerts/active",
            params={"area": area, "status": "actual", "message_type": "alert,update"},
            headers={"Accept": "application/geo+json"},
        )
        resp.raise_for_status()
        count = 0
        for feature in resp.json().get("features", []):
            ev = event_from_feature(feature)
            if ev:
                self.emit_event(ev)
                count += 1
        return count
