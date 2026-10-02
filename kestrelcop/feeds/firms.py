"""NASA FIRMS active-fire detections (VIIRS/MODIS) for the area, via the free MAP_KEY area API."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from ..cot.sidc import EVENT_COT_TYPES
from ..models import Event
from .base import Feed, bbox


def event_from_row(row: dict, product: str) -> Event | None:
    try:
        lat = float(row["latitude"])
        lon = float(row["longitude"])
    except (KeyError, ValueError):
        return None
    date = row.get("acq_date", "")
    hhmm = str(row.get("acq_time", "0")).zfill(4)
    try:
        ts = datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        ts = datetime.now(timezone.utc)
    frp = row.get("frp")
    uid = f"firms-{lat:.3f}-{lon:.3f}-{date}-{hhmm}"
    return Event(
        uid=uid, kind="fire", source="firms", title=f"{product.split('_')[0]} fire detection",
        lat=lat, lon=lon, cot_type=EVENT_COT_TYPES["fire"],
        severity="high" if frp and float(frp) >= 50 else "moderate",
        attrs={k: v for k, v in {
            "frp": float(frp) if frp else None,
            "confidence": row.get("confidence"),
            "brightness": row.get("bright_ti4") or row.get("brightness"),
            "satellite": row.get("satellite"),
            "daynight": row.get("daynight"),
        }.items() if v is not None},
        ts=ts, expires=ts + timedelta(hours=24),
    )


class FirmsFeed(Feed):
    name = "firms"
    label = "NASA FIRMS fires"

    def __init__(self, ctx, settings) -> None:
        super().__init__(ctx, settings, poll_seconds=settings.feeds.firms.poll_seconds)
        self.cfg = settings.feeds.firms

    async def run(self) -> None:
        if not self.cfg.map_key:
            self.ctx.feed_off(self.name, self.label, "no MAP_KEY — set FIRMS_MAP_KEY (free from NASA FIRMS)")
            return
        await super().run()

    async def poll(self) -> int:
        s, w, n, e = bbox(self.settings.area.center, self.settings.area.radius_km)
        url = (f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{self.cfg.map_key}/{self.cfg.product}/"
               f"{w:.3f},{s:.3f},{e:.3f},{n:.3f}/{self.cfg.days}")
        resp = await self.http.get(url)
        resp.raise_for_status()
        count = 0
        for row in csv.DictReader(io.StringIO(resp.text)):
            ev = event_from_row(row, self.cfg.product)
            if ev:
                self.emit_event(ev)
                count += 1
        return count
