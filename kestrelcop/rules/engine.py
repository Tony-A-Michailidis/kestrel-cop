"""Built-in detectors. Each one turns a condition on the picture into an Alert, exactly once.

This is the hard-coded ancestor of the rules DSL: when the DSL exists, these become its standard library.
"""

from __future__ import annotations

import itertools
from datetime import timedelta
from typing import Iterable

from ..config import RulesConfig, ZoneConfig
from ..geo import haversine_km, point_in_polygon
from ..models import Alert, Event, FeedStatus, Track, utcnow
from ..store import Store

EMERGENCY_SQUAWKS = {"7500": "HIJACK (7500)", "7600": "RADIO FAILURE (7600)", "7700": "GENERAL EMERGENCY (7700)"}


class RuleEngine:
    def __init__(self, store: Store, config: RulesConfig) -> None:
        self.store = store
        self.config = config
        self._seq = itertools.count(1)
        self._fired: set[str] = set()  # dedupe keys: "<rule>:<subject>"

    # ---- helpers ------------------------------------------------------------------------------
    def _alert(self, key: str, severity: str, rule: str, title: str, text: str,
               subject: Track | Event | None = None) -> Alert | None:
        if key in self._fired:
            return None
        self._fired.add(key)
        lat = getattr(subject, "lat", None)
        lon = getattr(subject, "lon", None)
        return Alert(
            id=f"alt-{next(self._seq)}", severity=severity, rule=rule, title=title, text=text,
            subject_uid=getattr(subject, "uid", None), lat=lat, lon=lon,
        )

    def _clear(self, key: str) -> None:
        self._fired.discard(key)

    @staticmethod
    def _in_zone(zone: ZoneConfig, lat: float, lon: float) -> bool:
        if zone.type == "circle" and zone.center and zone.radius_km:
            return haversine_km(zone.center[0], zone.center[1], lat, lon) <= zone.radius_km
        if zone.type == "polygon" and zone.ring:
            return point_in_polygon(lat, lon, zone.ring)
        return False

    # ---- per-track ----------------------------------------------------------------------------
    def on_track(self, track: Track, prev: Track | None) -> list[Alert]:
        alerts: list[Alert] = []

        # a track that reports again is no longer dark
        if "DARK" in track.flags:
            track.flags.remove("DARK")
            self._clear(f"ais_gap:{track.uid}")

        # emergency squawk
        squawk = str(track.attrs.get("squawk") or "")
        if squawk in EMERGENCY_SQUAWKS:
            if "EMERGENCY" not in track.flags:
                track.flags.append("EMERGENCY")
            a = self._alert(
                f"squawk:{track.uid}:{squawk}", "HIGH", "emergency_squawk",
                f"{EMERGENCY_SQUAWKS[squawk]} — {track.label}",
                f"{track.label} [{track.uid}] is squawking {squawk} at "
                f"{int(track.alt_m or 0)} m, {int(track.speed_kts or 0)} kt, course {int(track.course_deg or 0)}°. "
                f"Position {track.lat:.3f}, {track.lon:.3f}.",
                track,
            )
            if a:
                alerts.append(a)
        elif "EMERGENCY" in track.flags and squawk and squawk not in EMERGENCY_SQUAWKS:
            track.flags.remove("EMERGENCY")

        # geofenced zones
        for zone in self.config.zones:
            flag = f"IN_ZONE:{zone.name}"
            inside = self._in_zone(zone, track.lat, track.lon)
            was_inside = prev is not None and flag in prev.flags
            if inside and flag not in track.flags:
                track.flags.append(flag)
            if not inside and flag in track.flags:
                track.flags.remove(flag)
                self._clear(f"zone:{zone.name}:{track.uid}")
            if inside and not was_inside:
                a = self._alert(
                    f"zone:{zone.name}:{track.uid}", zone.severity, "zone_entry",
                    f"{zone.name} entry — {track.label}",
                    f"{track.label} [{track.uid}] ({track.domain}, {track.function}) entered {zone.name} at "
                    f"{track.lat:.3f}, {track.lon:.3f}, {int(track.speed_kts or 0)} kt"
                    + (f", {int(track.alt_m)} m" if track.alt_m is not None else "") + ".",
                    track,
                )
                if a:
                    alerts.append(a)
        return alerts

    # ---- per-event ----------------------------------------------------------------------------
    def on_event(self, event: Event, is_new: bool) -> list[Alert]:
        if not is_new:
            return []
        alerts: list[Alert] = []
        if event.kind == "quake" and (event.magnitude or 0) >= self.config.quake_alert_magnitude:
            sev = "HIGH" if (event.magnitude or 0) >= 6.0 else "MEDIUM"
            a = self._alert(f"quake:{event.uid}", sev, "earthquake", f"M{event.magnitude} earthquake — {event.title}",
                            f"USGS reports a magnitude {event.magnitude} earthquake: {event.title} [{event.uid}] "
                            f"at {event.lat:.2f}, {event.lon:.2f}, depth {event.attrs.get('depth_km', '?')} km.",
                            event)
            if a:
                alerts.append(a)
        if event.kind == "alert" and (event.severity or "").lower() in ("severe", "extreme"):
            sev = "HIGH" if (event.severity or "").lower() == "extreme" else "MEDIUM"
            a = self._alert(f"wx:{event.uid}", sev, "weather_alert", f"{event.severity} weather — {event.title}",
                            f"NWS {event.title} [{event.uid}]: {event.attrs.get('headline', '')}", event)
            if a:
                alerts.append(a)
        if event.kind == "fire" and float(event.attrs.get("frp", 0) or 0) >= 50:
            # one alert per ~10 km cell: a hot fire produces many detections per pass
            a = self._alert(f"fire:{event.lat:.1f}:{event.lon:.1f}", "MEDIUM", "fire_detection", f"High-intensity fire detection — FRP {event.attrs.get('frp')} MW",
                            f"FIRMS detection [{event.uid}] with fire radiative power {event.attrs.get('frp')} MW "
                            f"at {event.lat:.3f}, {event.lon:.3f}.", event)
            if a:
                alerts.append(a)
        return alerts

    # ---- periodic -----------------------------------------------------------------------------
    def periodic(self, feeds: Iterable[FeedStatus]) -> list[Alert]:
        """Run every few seconds: stale feeds and vessels that went quiet while under way."""
        alerts: list[Alert] = []
        now = utcnow()

        for fs in feeds:
            key = f"feed_stale:{fs.name}"
            if fs.mode == "off":
                continue
            if fs.status in ("down", "degraded") and fs.last_message and \
                    (now - fs.last_message) > timedelta(seconds=fs.stale_after_s or self.config.stale_feed_seconds):
                a = self._alert(key, "MEDIUM", "feed_stale", f"Feed stale — {fs.label}",
                                f"{fs.label} has delivered nothing for "
                                f"{int((now - fs.last_message).total_seconds())} s (status {fs.status}"
                                + (f": {fs.error}" if fs.error else "") + ").")
                if a:
                    alerts.append(a)
            elif fs.status == "ok":
                self._clear(key)

        gap = timedelta(minutes=self.config.ais_gap_minutes)
        for t in self.store.tracks.values():
            if t.domain != "sea" or "DARK" in t.flags:
                continue
            if (t.speed_kts or 0) < self.config.ais_gap_min_speed_kts:
                continue
            if now - t.ts > gap:
                t.flags.append("DARK")
                a = self._alert(f"ais_gap:{t.uid}", "MEDIUM", "ais_gap", f"AIS gap — {t.label}",
                                f"{t.label} [{t.uid}] was making {t.speed_kts:.0f} kt on course {int(t.course_deg or 0)}° "
                                f"and has not reported for {int((now - t.ts).total_seconds() / 60)} min. "
                                f"Last position {t.lat:.3f}, {t.lon:.3f}. Possible AIS gap or transponder off.",
                                t)
                if a:
                    alerts.append(a)
        return alerts
