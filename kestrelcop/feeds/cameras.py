"""Traffic and roadside cameras from open government sources, as `camera` events on the picture.

Every source publishes a list of fixed cameras with a position and a URL of a still image that the agency
refreshes every one to fifteen minutes. Kestrel keeps the list (filtered to the configured area), draws each
camera on the map, lets the console and the watch officer fetch the current still, and never re-hosts imagery:
the browser pulls from the agency, and the server only proxies a frame on demand for the model.

Sources (one pure parser each; the adapter below polls whichever are enabled):

* DriveBC (British Columbia): https://drivebc.ca/api/webcams, no key, Open Government Licence - BC.
* Caltrans CWWP2 (California): per-district JSON, no key, no charge. Many cameras also offer an HLS stream.
* City of Toronto open data: GeoJSON camera list, stills every 2-3 minutes, Open Government Licence - Toronto.
* City of Ottawa: JSON camera list at traffic.ottawa.ca, images at 60 s intervals (city and MTO cameras).
* WSDOT (Washington): Traveler Information API, free access code, snapshots only.
* The "511" platform (CARS) used by Ontario, Alberta, Wisconsin, Georgia, Arizona, Iowa, Yukon and others:
  `/api/v2/get/cameras`, free developer key per site, ten calls a minute.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import Any

from ..cot.sidc import EVENT_COT_TYPES
from ..geo import haversine_km
from ..models import Event, utcnow
from .base import Feed

log = logging.getLogger("kestrel.feeds.cameras")

CAMERA_TTL = timedelta(minutes=50)   # a camera disappears if its source has not listed it for this long
IMAGE_MAX_BYTES = 4 * 1024 * 1024
IMAGE_CACHE_SECONDS = 20.0


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def _camera(uid: str, source: str, title: str, lat: float, lon: float, image_url: str, *,
            road: str | None = None, direction: str | None = None, place: str | None = None,
            refresh_seconds: int | None = None, stream_url: str | None = None, page_url: str | None = None,
            owner: str | None = None, credit: str | None = None, now=None) -> Event:
    """The one shape every source is normalised to. `attrs.image_url` is what the console and the model fetch."""
    now = now or utcnow()
    attrs = {k: v for k, v in {
        "image_url": image_url, "road": road, "direction": direction, "place": place,
        "refresh_seconds": refresh_seconds, "stream_url": stream_url, "page_url": page_url,
        "owner": owner, "credit": credit,
    }.items() if v not in (None, "")}
    return Event(uid=uid, kind="camera", source=source, title=title, lat=float(lat), lon=float(lon),
                 cot_type=EVENT_COT_TYPES["camera"], attrs=attrs, ts=now, expires=now + CAMERA_TTL)


# ---- pure parsers: one source record in, one camera Event (or None) out ---------------------------------------
def camera_from_drivebc(rec: dict, now=None) -> Event | None:
    loc = (rec.get("location") or {}).get("coordinates") or []
    img = (rec.get("links") or {}).get("imageDisplay")
    if len(loc) < 2 or not img or rec.get("is_on") is False:
        return None
    name = rec.get("name_override") or rec.get("name") or f"DriveBC {rec.get('id')}"
    period = rec.get("update_period_mean")
    return _camera(f"cam-bc-{rec.get('id')}", "drivebc", name, loc[1], loc[0], "https://images.drivebc.ca" + str(img),
                   road=f"Hwy {rec['highway_display']}" if rec.get("highway_display") else None,
                   direction=rec.get("orientation"), place=rec.get("caption_override") or rec.get("caption"),
                   refresh_seconds=int(period) if period else None,
                   page_url=f"https://www.drivebc.ca/cameras/{rec.get('id')}", credit=rec.get("credit") or "DriveBC",
                   now=now)


def camera_from_caltrans(rec: dict, now=None) -> Event | None:
    c = rec.get("cctv") or rec
    loc = c.get("location") or {}
    static = (c.get("imageData") or {}).get("static") or {}
    img = static.get("currentImageURL")
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if not img or lat in (None, "") or lon in (None, "") or str(c.get("inService", "true")).lower() == "false":
        return None
    name = loc.get("locationName") or f"{loc.get('route', '')} {loc.get('nearbyPlace', '')}".strip()
    freq = static.get("currentImageUpdateFrequency")
    stream = (c.get("imageData") or {}).get("streamingVideoURL") or None
    return _camera(f"cam-ca-d{loc.get('district', '0')}-{_slug(name) or c.get('index')}", "caltrans", name,
                   float(lat), float(lon), img.replace("\\/", "/"),
                   road=loc.get("route") or None, direction=loc.get("direction") or None,
                   place=loc.get("nearbyPlace") or loc.get("county") or None,
                   refresh_seconds=int(float(freq)) * 60 if freq not in (None, "") else None,
                   stream_url=stream.replace("\\/", "/") if stream else None, credit="Caltrans", now=now)


def camera_from_toronto(feature: dict, now=None) -> Event | None:
    p = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []
    if geom.get("type") == "MultiPoint" and coords:
        coords = coords[0]
    img = p.get("IMAGEURL")
    if len(coords) < 2 or not img:
        return None
    main, cross = (p.get("MAINROAD") or "").strip(), (p.get("CROSSROAD") or "").strip()
    name = f"{main} / {cross}" if main and cross else (main or cross or f"Toronto {p.get('REC_ID')}")
    return _camera(f"cam-to-{p.get('REC_ID') or p.get('_id')}", "toronto", name.title(), coords[1], coords[0],
                   str(img).replace("http://", "https://"), road=main.title() or None, refresh_seconds=150,
                   page_url="https://www.toronto.ca/services-payments/streets-parking-transportation/traffic-management/pavement-markings/traffic-monitoring-cameras/",
                   credit="City of Toronto", now=now)


def camera_from_ottawa(rec: dict, now=None) -> Event | None:
    lat, lon, num = rec.get("latitude"), rec.get("longitude"), rec.get("camera_number")
    if lat is None or lon is None or num is None:
        return None
    owner = (rec.get("cameraOwner") or "").upper()
    return _camera(f"cam-ott-{num}", "ottawa", rec.get("name") or f"Ottawa {num}", float(lat), float(lon),
                   f"https://traffic.ottawa.ca/map/camera?id={num}", refresh_seconds=60,
                   owner="MTO" if owner == "MTO" or int(num) >= 2000 else "City of Ottawa",
                   page_url="https://traffic.ottawa.ca/map", credit="City of Ottawa / MTO", now=now)


def camera_from_wsdot(rec: dict, now=None) -> Event | None:
    loc = rec.get("CameraLocation") or {}
    lat = rec.get("DisplayLatitude") or loc.get("Latitude")
    lon = rec.get("DisplayLongitude") or loc.get("Longitude")
    img = rec.get("ImageURL")
    if not img or lat in (None, 0) or lon in (None, 0) or rec.get("IsActive") is False:
        return None
    return _camera(f"cam-wa-{rec.get('CameraID')}", "wsdot", rec.get("Title") or f"WSDOT {rec.get('CameraID')}",
                   float(lat), float(lon), str(img), road=loc.get("RoadName") or None,
                   direction=loc.get("Direction") or None, place=loc.get("Description") or None,
                   owner=rec.get("CameraOwner") or None, page_url=rec.get("OwnerURL") or None,
                   credit="WSDOT", now=now)


def camera_from_cars(rec: dict, site: str, now=None) -> Event | None:
    """The 511/CARS platform: v2 records carry a `Views` list; older ones a flat `Url`. Keys vary in case."""
    g = {str(k).lower(): v for k, v in rec.items()}
    lat, lon = g.get("latitude"), g.get("longitude")
    views = g.get("views") or []
    view = next((v for v in views if (v.get("Url") or v.get("url"))), None) if views else None
    img = (view.get("Url") or view.get("url")) if view else g.get("url")
    if lat in (None, 0, "") or lon in (None, 0, "") or not img:
        return None
    if str(g.get("status", view.get("Status") if view else "")).lower() in ("disabled", "offline", "inactive"):
        return None
    cam_id = g.get("id") or g.get("sourceid") or _slug(str(img))[-40:]
    name = g.get("location") or g.get("name") or (view or {}).get("Description") or f"{site} {cam_id}"
    return _camera(f"cam-{_slug(site)}-{cam_id}", f"511-{_slug(site)}", str(name), float(lat), float(lon), str(img),
                   road=g.get("roadway") or None, direction=g.get("direction") or None,
                   stream_url=(view or {}).get("VideoUrl") or g.get("videourl") or None,
                   owner=g.get("source") or None, credit=f"511 {site}", now=now)


# ---- the image fetch shared by the proxy endpoint and the watch officer ---------------------------------------
_image_cache: dict[str, tuple[float, bytes, str]] = {}


def _sniff_image(data: bytes) -> str | None:
    """The real image type from the magic bytes, or None when it is not a known raster format."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def fetch_camera_image(http: Any, url: str) -> tuple[bytes, str]:
    """Current still for a camera, cached for a few seconds so a burst of viewers is one request to the agency.
    Raises on HTTP errors, non-image responses and oversize bodies."""
    hit = _image_cache.get(url)
    if hit and time.monotonic() - hit[0] < IMAGE_CACHE_SECONDS:
        return hit[1], hit[2]
    resp = await http.get(url, headers={"Accept": "image/*"})
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip().lower()
    data = resp.content
    sniffed = _sniff_image(data)   # agencies mislabel: Toronto serves PNG as image/jpeg, and the model API checks the bytes
    if sniffed:
        ctype = sniffed
    elif ctype == "image/jpg":
        ctype = "image/jpeg"
    if not ctype.startswith("image/"):
        raise ValueError(f"camera returned {ctype}, not an image")
    if len(data) > IMAGE_MAX_BYTES:
        raise ValueError(f"camera image too large ({len(data)} bytes)")
    if len(_image_cache) > 256:
        _image_cache.clear()
    _image_cache[url] = (time.monotonic(), data, ctype)
    return data, ctype


# ---- the feed ---------------------------------------------------------------------------------------------------
class CamerasFeed(Feed):
    """Polls every enabled camera source, keeps the cameras inside the area, and emits them as events.

    Sources are fetched concurrently; one failing source is logged and skipped, the rest still land. The feed
    is `ok` when at least one source answered, `degraded` (via feed_error) only when all of them failed.
    """

    name = "cameras"
    label = "Traffic cameras"

    def __init__(self, ctx, settings) -> None:
        super().__init__(ctx, settings, poll_seconds=settings.feeds.cameras.poll_seconds)
        self.cfg = settings.feeds.cameras
        self.last_sources: dict[str, int | str] = {}  # per-source count or error, for the health detail

    def _in_area(self, ev: Event) -> bool:
        clat, clon = self.settings.area.center
        return haversine_km(clat, clon, ev.lat, ev.lon) <= self.settings.area.radius_km

    async def _get_json(self, url: str, **kw: Any) -> Any:
        resp = await self.http.get(url, **kw)
        resp.raise_for_status()
        return resp.json()

    # each source returns a list of Events (already filtered to the area)
    async def _drivebc(self) -> list[Event]:
        data = await self._get_json("https://drivebc.ca/api/webcams")
        return [ev for rec in data for ev in [camera_from_drivebc(rec)] if ev and self._in_area(ev)]

    async def _caltrans(self) -> list[Event]:
        out: list[Event] = []
        failed: list[str] = []
        for d in self.cfg.caltrans_districts:
            try:
                data = await self._get_json(f"https://cwwp2.dot.ca.gov/data/d{d}/cctv/cctvStatusD{d:02d}.json")
            except Exception as exc:  # noqa: BLE001 - one district down must not hide the others
                log.warning("caltrans district %s: %s", d, exc)
                failed.append(f"d{d}: {exc}")
                continue
            out.extend(ev for rec in data.get("data", []) for ev in [camera_from_caltrans(rec)] if ev and self._in_area(ev))
        if failed and len(failed) == len(self.cfg.caltrans_districts):
            raise RuntimeError("all districts failed: " + failed[0])
        return out

    async def _toronto(self) -> list[Event]:
        data = await self._get_json(self.cfg.toronto_url)
        return [ev for f in data.get("features", []) for ev in [camera_from_toronto(f)] if ev and self._in_area(ev)]

    async def _ottawa(self) -> list[Event]:
        data = await self._get_json("https://traffic.ottawa.ca/map/service/camera")
        return [ev for rec in data.get("cameras", []) for ev in [camera_from_ottawa(rec)] if ev and self._in_area(ev)]

    async def _wsdot(self) -> list[Event]:
        url = "https://wsdot.wa.gov/Traffic/api/HighwayCameras/HighwayCamerasREST.svc/GetCamerasAsJson"
        data = await self._get_json(url, params={"AccessCode": self.cfg.wsdot_access_code})
        return [ev for rec in data for ev in [camera_from_wsdot(rec)] if ev and self._in_area(ev)]

    async def _cars(self, site: Any) -> list[Event]:
        data = await self._get_json(site.host.rstrip("/") + "/api/v2/get/cameras", params={"format": "json", "key": site.key})
        return [ev for rec in data for ev in [camera_from_cars(rec, site.name)] if ev and self._in_area(ev)]

    def _sources(self) -> list[tuple[str, Any]]:
        s: list[tuple[str, Any]] = []
        if self.cfg.drivebc:
            s.append(("drivebc", self._drivebc()))
        if self.cfg.caltrans:
            s.append(("caltrans", self._caltrans()))
        if self.cfg.toronto:
            s.append(("toronto", self._toronto()))
        if self.cfg.ottawa:
            s.append(("ottawa", self._ottawa()))
        if self.cfg.wsdot_access_code:
            s.append(("wsdot", self._wsdot()))
        for site in self.cfg.cars:
            if site.key:
                s.append((f"511-{site.name}", self._cars(site)))
        return s

    async def poll(self) -> int:
        sources = self._sources()
        if not sources:
            raise RuntimeError("no camera source is enabled (set drivebc/caltrans/toronto/ottawa, a WSDOT code or a 511 key)")
        results = await asyncio.gather(*(coro for _, coro in sources), return_exceptions=True)
        count, failures = 0, 0
        for (name, _), res in zip(sources, results):
            if isinstance(res, BaseException):
                failures += 1
                self.last_sources[name] = f"{type(res).__name__}: {res}"[:120]
                log.warning("camera source %s failed: %s", name, res)
                continue
            self.last_sources[name] = len(res)
            for ev in res[: max(0, self.cfg.max_cameras - count)]:
                self.emit_event(ev)
                count += 1
        if failures == len(sources):
            raise RuntimeError("every camera source failed: " + "; ".join(f"{k}: {v}" for k, v in self.last_sources.items()))
        return count

    def detail(self) -> str:
        return ", ".join(f"{k} {v}" if isinstance(v, int) else f"{k} failed" for k, v in self.last_sources.items())
