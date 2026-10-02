"""A seeded synthetic world so the picture lives without any API keys or internet access.

Everything is deterministic for a given seed (the Rust Bucket rule: a demo you can reproduce is a
demo you can debug). Aircraft follow waypoints with realistic turn rates, vessels work a shipping
lane or fish, and a short scripted scenario exercises the rules engine and the watch officer:

    t+60s   an airliner squawks 7700 and diverts toward the field
    t+100s  a cargo ship under way stops transmitting AIS (reappears at t+400s)
    ~t+200s a light aircraft blunders into ROZ ALPHA
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import Settings
from ..cot.sidc import EVENT_COT_TYPES, air_codes, sea_codes
from ..geo import KM_PER_NM, bearing_deg, destination, haversine_km, turn_toward
from ..models import Event, Track, utcnow
from .base import Feed

FT_TO_M = 0.3048

AIRLINES = ["UAL", "DAL", "AAL", "JBU", "SWA", "BAW", "DLH", "AFR", "KLM", "FDX", "UPS", "EJA"]
REGIONALS = ["RPA", "SKW", "PDT", "ENY", "JIA"]
GA_SUFFIX = ["AB", "TC", "RM", "KX", "PJ", "WL", "DE", "GH"]
CARGO_NAMES = ["MAERSK SAVANNAH", "EVER LIBERAL", "MSC ANTONIA", "NORDIC HARRIER", "PACIFIC VENTURE",
               "CMA CGM TANGER", "ATLANTIC NAVIGATOR", "HANSA ROTENBURG", "SEABOARD PRIDE", "HORIZON TRADER",
               "GOLDEN CURL", "OCEAN STAR"]
TANKER_NAMES = ["STENA CONQUEST", "NORDIC AQUARIUS", "EAGLE BRASILIA", "TORM ALICE"]
FISHING_NAMES = ["LADY CAROLINE", "MISS BRENDA", "CAPT JOHN", "SEA HUNTER", "DOUBLE TROUBLE", "MARLIN MAGIC"]
LEISURE_NAMES = ["WIND DANCER", "SERENITY", "KNOT WORKING", "BLUE MOON"]
MID_US, MID_LIB, MID_MARSHALL, MID_PANAMA, MID_SING = 367, 636, 538, 352, 563


@dataclass
class SimAircraft:
    hex: str
    callsign: str
    kind: str          # airliner | regional | ga | heli | mil
    category: str
    lat: float
    lon: float
    alt_ft: float
    gs: float
    trk: float
    squawk: str
    waypoint: tuple[float, float]
    target_alt: float | None = None
    emergency: bool = False
    fixed_route: list[tuple[float, float]] = field(default_factory=list)

    def step(self, dt: float, rng: random.Random, world: "DemoWorld") -> None:
        wp_lat, wp_lon = self.waypoint
        bearing = bearing_deg(self.lat, self.lon, wp_lat, wp_lon)
        max_turn = {"airliner": 1.5, "regional": 2.0, "mil": 2.5, "ga": 3.0, "heli": 6.0}[self.kind] * dt
        self.trk = turn_toward(self.trk, bearing, max_turn)
        self.gs = max(40.0, self.gs + rng.uniform(-1.0, 1.0))
        dist_km = self.gs * KM_PER_NM / 3600.0 * dt
        self.lat, self.lon = destination(self.lat, self.lon, self.trk, dist_km)
        if self.target_alt is not None:
            rate = 1800.0 / 60.0 * dt if self.emergency else 1200.0 / 60.0 * dt
            if abs(self.target_alt - self.alt_ft) <= rate:
                self.alt_ft = self.target_alt
                self.target_alt = None
            else:
                self.alt_ft += rate if self.target_alt > self.alt_ft else -rate
        if haversine_km(self.lat, self.lon, wp_lat, wp_lon) < 3.0:
            if self.fixed_route:
                self.waypoint = self.fixed_route.pop(0)
            else:
                self.waypoint = world.random_point(rng, min_km=40, max_km=world.radius_km * 1.2)
                if self.kind in ("airliner", "regional") and rng.random() < 0.3:
                    self.target_alt = rng.choice([24000, 28000, 31000, 35000, 38000])

    def track(self, now: datetime) -> Track:
        military = self.kind == "mil" or self.callsign.startswith("USCG")
        rotary = self.category == "A7"
        function = ("mil_rotary" if rotary else "mil_fixed_wing") if military else ("rotary" if rotary else "fixed_wing")
        affiliation = "friend" if military else "neutral"
        sidc, cot_type = air_codes(affiliation, function)
        return Track(
            uid=f"adsb-{self.hex}", domain="air", source="adsb", callsign=self.callsign,
            lat=self.lat, lon=self.lon, alt_m=self.alt_ft * FT_TO_M, speed_kts=self.gs, course_deg=self.trk,
            affiliation=affiliation, function=function, sidc=sidc, cot_type=cot_type,
            attrs={"hex": self.hex, "squawk": self.squawk, "category": self.category, "alt_ft": int(self.alt_ft),
                   "military": military, "vertical_rate_fpm": (
                       (1200 if (self.target_alt or 0) > self.alt_ft else -1200) if self.target_alt is not None else 0)},
            flags=["MILITARY"] if military else [], ts=now,
        )


@dataclass
class SimVessel:
    mmsi: int
    name: str
    kind: str          # cargo | tanker | fishing | leisure | tow | law_enforcement | anchored
    ship_type: int
    lat: float
    lon: float
    sog: float
    cog: float
    waypoint: tuple[float, float]
    nav_status: str = "under way using engine"
    destination: str | None = None
    route: list[tuple[float, float]] = field(default_factory=list)
    dark: bool = False

    def step(self, dt: float, rng: random.Random, world: "DemoWorld") -> None:
        if self.kind == "anchored":
            return
        wp_lat, wp_lon = self.waypoint
        bearing = bearing_deg(self.lat, self.lon, wp_lat, wp_lon)
        max_turn = {"cargo": 0.4, "tanker": 0.3, "tow": 0.8, "law_enforcement": 2.0, "fishing": 3.0, "leisure": 2.0}.get(self.kind, 1.0) * dt
        self.cog = turn_toward(self.cog, bearing, max_turn)
        self.sog = max(0.5, self.sog + rng.uniform(-0.1, 0.1))
        dist_km = self.sog * KM_PER_NM / 3600.0 * dt
        self.lat, self.lon = destination(self.lat, self.lon, self.cog, dist_km)
        if haversine_km(self.lat, self.lon, wp_lat, wp_lon) < 1.0:
            if self.route:
                self.route.append(self.waypoint)
                self.waypoint = self.route.pop(0)
            elif self.kind == "fishing":
                self.waypoint = destination(self.lat, self.lon, rng.uniform(0, 360), rng.uniform(3, 12))
            else:
                self.waypoint = world.random_sea_point(rng)

    def track(self, now: datetime) -> Track:
        function = {"cargo": "cargo", "tanker": "tanker", "fishing": "fishing", "leisure": "leisure", "tow": "tow",
                    "law_enforcement": "law_enforcement", "anchored": "cargo"}[self.kind]
        affiliation = "friend" if self.kind == "law_enforcement" else "neutral"
        sidc, cot_type = sea_codes(affiliation, function)
        return Track(
            uid=f"ais-{self.mmsi}", domain="sea", source="ais", callsign=self.name,
            lat=self.lat, lon=self.lon, alt_m=0.0, speed_kts=self.sog, course_deg=self.cog, heading_deg=self.cog,
            affiliation=affiliation, function=function, sidc=sidc, cot_type=cot_type,
            attrs={k: v for k, v in {"mmsi": self.mmsi, "ship_type": self.ship_type, "nav_status": self.nav_status,
                                     "destination": self.destination}.items() if v is not None},
            ts=now,
        )


class DemoWorld:
    def __init__(self, settings: Settings, seed: int = 42) -> None:
        self.settings = settings
        self.rng = random.Random(seed)
        self.center = settings.area.center
        self.radius_km = settings.area.radius_km
        self.sea_bearings = settings.demo.sea_bearing_range
        self.elapsed = 0.0
        self.aircraft: list[SimAircraft] = []
        self.vessels: list[SimVessel] = []
        self.events: list[Event] = []
        self._scenario_done: set[str] = set()
        self._build()

    # ---- geometry helpers ---------------------------------------------------------------------
    def random_point(self, rng: random.Random, min_km: float, max_km: float) -> tuple[float, float]:
        return destination(self.center[0], self.center[1], rng.uniform(0, 360), rng.uniform(min_km, max_km))

    def random_sea_point(self, rng: random.Random, min_km: float = 45.0, max_km: float | None = None) -> tuple[float, float]:
        """A point over water (by configured bearing sector) that is not inside a configured zone."""
        b0, b1 = self.sea_bearings
        for _ in range(40):
            p = destination(self.center[0], self.center[1], rng.uniform(b0, b1), rng.uniform(min_km, max_km or self.radius_km * 0.9))
            if not self._in_any_zone(p, margin_km=8.0):
                return p
        return p

    def _in_any_zone(self, p: tuple[float, float], margin_km: float = 0.0) -> bool:
        for z in self.settings.rules.zones:
            if z.type == "circle" and z.center and z.radius_km:
                if haversine_km(z.center[0], z.center[1], p[0], p[1]) <= z.radius_km + margin_km:
                    return True
        return False

    def _hex(self, military: bool = False) -> str:
        if military:
            return f"{self.rng.randint(0xADF7C8, 0xAFFFFF):06x}"
        return f"a{self.rng.randint(0x10000, 0xFFFFF):05x}"

    # ---- world construction -------------------------------------------------------------------
    def _build(self) -> None:
        rng = self.rng
        clat, clon = self.center

        def make_aircraft(kind: str, callsign: str, category: str, alt: float, gs: float, military: bool = False,
                          start: tuple[float, float] | None = None, waypoint: tuple[float, float] | None = None,
                          squawk: str | None = None) -> SimAircraft:
            lat, lon = start or self.random_point(rng, 10, self.radius_km)
            wp = waypoint or self.random_point(rng, 40, self.radius_km * 1.2)
            return SimAircraft(
                hex=self._hex(military), callsign=callsign, kind=kind, category=category, lat=lat, lon=lon,
                alt_ft=alt, gs=gs, trk=bearing_deg(lat, lon, wp[0], wp[1]),
                squawk=squawk or (f"{rng.randint(0, 7)}{rng.randint(0, 7)}{rng.randint(0, 7)}{rng.randint(0, 7)}"),
                waypoint=wp,
            )

        for i in range(22):
            cs = f"{rng.choice(AIRLINES)}{rng.randint(100, 2899)}"
            self.aircraft.append(make_aircraft("airliner", cs, rng.choice(["A3", "A3", "A5"]),
                                               rng.choice([30000, 32000, 34000, 36000, 38000]), rng.uniform(430, 485)))
        for i in range(8):
            cs = f"{rng.choice(REGIONALS)}{rng.randint(3000, 5999)}"
            self.aircraft.append(make_aircraft("regional", cs, "A2", rng.choice([20000, 23000, 26000, 28000]), rng.uniform(330, 400)))
        for i in range(7):
            cs = f"N{rng.randint(100, 999)}{rng.choice(GA_SUFFIX)}"
            self.aircraft.append(make_aircraft("ga", cs, "A1", rng.choice([2500, 3500, 4500, 5500, 6500]), rng.uniform(95, 150), squawk="1200"))
        self.aircraft.append(make_aircraft("heli", "N911MD", "A7", 1200, 118, squawk="1200",
                                           start=destination(clat, clon, 300, 30), waypoint=(clat, clon)))
        self.aircraft.append(make_aircraft("heli", "USCG6031", "A7", 800, 105, military=True,
                                           start=destination(clat, clon, 120, 25), waypoint=destination(clat, clon, 145, 95)))
        self.aircraft.append(make_aircraft("mil", "RCH441", "A5", 26000, 345, military=True,
                                           start=destination(clat, clon, 200, 200), waypoint=destination(clat, clon, 20, 200)))

        # the light aircraft that will enter ROZ ALPHA: starts 30 km west of the zone, heading east
        zone = self.settings.rules.zones[0] if self.settings.rules.zones and self.settings.rules.zones[0].center else None
        if zone and zone.center:
            start = destination(zone.center[0], zone.center[1], 270, 30)
            exit_point = destination(zone.center[0], zone.center[1], 90, 60)
            roz_ac = make_aircraft("ga", "N427WL", "A1", 3500, 120, squawk="1200", start=start, waypoint=exit_point)
            roz_ac.fixed_route = [destination(zone.center[0], zone.center[1], 20, 120)]
            self.aircraft.append(roz_ac)

        # vessels: a shipping lane from the Chesapeake approaches out to sea
        lane_a = (clat - 0.02, clon + 0.40)
        lane_b = destination(lane_a[0], lane_a[1], 135, 220)
        for i in range(10):
            frac = rng.uniform(0.05, 0.95)
            lat, lon = destination(lane_a[0], lane_a[1], 135, 220 * frac)
            outbound = rng.random() < 0.5
            wp, back = (lane_b, lane_a) if outbound else (lane_a, lane_b)
            is_tanker = i % 4 == 3
            self.vessels.append(SimVessel(
                mmsi=int(f"{rng.choice([MID_US, MID_LIB, MID_MARSHALL, MID_PANAMA, MID_SING])}{rng.randint(100000, 999999)}"),
                name=TANKER_NAMES[i // 4 % len(TANKER_NAMES)] if is_tanker else CARGO_NAMES[i % len(CARGO_NAMES)],
                kind="tanker" if is_tanker else "cargo", ship_type=rng.choice([80, 81, 82]) if is_tanker else rng.choice([70, 71, 74, 79]),
                lat=lat, lon=lon, sog=rng.uniform(10, 16), cog=135 if outbound else 315, waypoint=wp, route=[back],
                destination=rng.choice(["NORFOLK", "BALTIMORE", "NEW YORK", "SAVANNAH", "ROTTERDAM", "COLON"]),
            ))
        for i in range(6):
            lat, lon = self.random_sea_point(rng, 55, 150)
            self.vessels.append(SimVessel(
                mmsi=int(f"{MID_US}{rng.randint(100000, 999999)}"), name=FISHING_NAMES[i], kind="fishing", ship_type=30,
                lat=lat, lon=lon, sog=rng.uniform(3, 6), cog=rng.uniform(0, 360),
                waypoint=destination(lat, lon, rng.uniform(0, 360), rng.uniform(3, 10)), nav_status="engaged in fishing",
            ))
        for i in range(4):
            lat, lon = self.random_sea_point(rng, 45, 75)
            self.vessels.append(SimVessel(
                mmsi=int(f"{MID_US}{rng.randint(100000, 999999)}"), name=LEISURE_NAMES[i], kind="leisure", ship_type=36,
                lat=lat, lon=lon, sog=rng.uniform(4, 7), cog=rng.uniform(0, 360),
                waypoint=self.random_sea_point(rng, 45, 80), nav_status="under way sailing",
            ))
        anchorage = (clat - 0.05, clon + 0.35)
        for i in range(3):
            lat, lon = destination(anchorage[0], anchorage[1], rng.uniform(0, 360), rng.uniform(0.5, 3))
            self.vessels.append(SimVessel(
                mmsi=int(f"{MID_LIB}{rng.randint(100000, 999999)}"), name=f"{rng.choice(['ORIENT', 'GLOBAL', 'ATLAS'])} {rng.choice(['PIONEER', 'SPIRIT', 'UNITY'])}",
                kind="anchored", ship_type=70, lat=lat, lon=lon, sog=0.1, cog=rng.uniform(0, 360), waypoint=(lat, lon),
                nav_status="at anchor", destination="NORFOLK",
            ))
        tug_start = (clat - 0.01, clon + 0.30)
        self.vessels.append(SimVessel(
            mmsi=int(f"{MID_US}{rng.randint(100000, 999999)}"), name="MCALLISTER SISTERS", kind="tow", ship_type=52,
            lat=tug_start[0], lon=tug_start[1], sog=7.0, cog=90, waypoint=destination(tug_start[0], tug_start[1], 90, 25),
            route=[tug_start],
        ))
        if zone and zone.center:
            patrol = [destination(zone.center[0], zone.center[1], b, 22) for b in (45, 135, 225, 315)]
            self.vessels.append(SimVessel(
                mmsi=367000000 + rng.randint(1000, 9999), name="USCGC FORWARD", kind="law_enforcement", ship_type=55,
                lat=patrol[0][0], lon=patrol[0][1], sog=14.0, cog=135, waypoint=patrol[1], route=patrol[2:] + [patrol[0]],
            ))

        # static events
        now = utcnow()
        swamp = (clat - 0.33, clon - 0.17)
        for i in range(6):
            lat = swamp[0] + rng.uniform(-0.07, 0.07)
            lon = swamp[1] + rng.uniform(-0.09, 0.09)
            frp = 72.4 if i == 2 else round(rng.uniform(8, 45), 1)
            self.events.append(Event(
                uid=f"firms-{lat:.3f}-{lon:.3f}-demo", kind="fire", source="firms", title="VIIRS fire detection",
                lat=lat, lon=lon, severity="high" if frp >= 50 else "moderate", cot_type=EVENT_COT_TYPES["fire"],
                attrs={"frp": frp, "confidence": rng.choice(["n", "h"]), "satellite": "N", "daynight": "D"},
                ts=now - timedelta(hours=rng.uniform(1, 5)), expires=now + timedelta(hours=20),
            ))
        self.events.append(Event(
            uid="usgs-demo-mineral", kind="quake", source="usgs", title="3 km SE of Mineral, Virginia",
            lat=38.01, lon=-77.89, magnitude=2.7, severity="low", cot_type=EVENT_COT_TYPES["quake"],
            attrs={"depth_km": 7.6, "regional": True}, ts=now - timedelta(hours=3), expires=now + timedelta(days=3),
        ))
        self.events.append(Event(
            uid="usgs-demo-honshu", kind="quake", source="usgs", title="112 km E of Ishinomaki, Japan",
            lat=38.32, lon=142.69, magnitude=6.1, severity="high", cot_type=EVENT_COT_TYPES["quake"],
            attrs={"depth_km": 34.0, "regional": False, "tsunami": 0}, ts=now - timedelta(minutes=50), expires=now + timedelta(days=3),
        ))
        coastal = [[clon + 0.30, clat - 0.45], [clon + 1.05, clat - 0.45], [clon + 1.05, clat + 0.40], [clon + 0.30, clat + 0.40], [clon + 0.30, clat - 0.45]]
        self.events.append(Event(
            uid="nws-demo-sca", kind="alert", source="nws", title="Small Craft Advisory", lat=clat - 0.02, lon=clon + 0.67,
            geometry={"type": "Polygon", "coordinates": [coastal]}, severity="Minor", cot_type=EVENT_COT_TYPES["alert"],
            attrs={"headline": "Small Craft Advisory until 10 PM EDT this evening", "area": "Coastal waters from Cape Charles Light to Currituck Beach Light out 20 NM",
                   "urgency": "Expected", "certainty": "Likely", "sender": "NWS Wakefield VA"},
            ts=now - timedelta(hours=2), expires=now + timedelta(hours=8),
        ))
        storm = [[clon - 1.20, clat - 0.45], [clon - 0.60, clat - 0.45], [clon - 0.60, clat + 0.25], [clon - 1.20, clat + 0.25], [clon - 1.20, clat - 0.45]]
        self.events.append(Event(
            uid="nws-demo-svr", kind="alert", source="nws", title="Severe Thunderstorm Warning", lat=clat - 0.10, lon=clon - 0.90,
            geometry={"type": "Polygon", "coordinates": [storm]}, severity="Severe", cot_type=EVENT_COT_TYPES["alert"],
            attrs={"headline": "Severe Thunderstorm Warning until 7:45 PM EDT", "area": "Southampton; Isle of Wight; Suffolk", "urgency": "Immediate",
                   "certainty": "Observed", "sender": "NWS Wakefield VA", "instruction": "Move to an interior room on the lowest floor of a sturdy building."},
            ts=now - timedelta(minutes=12), expires=now + timedelta(minutes=45),
        ))

    # ---- time -----------------------------------------------------------------------------------
    def step(self, dt: float) -> None:
        self.elapsed += dt
        for a in self.aircraft:
            a.step(dt, self.rng, self)
        for v in self.vessels:
            v.step(dt, self.rng, self)
        if self.settings.demo.scenario:
            self._scenario()

    def _scenario(self) -> None:
        if self.elapsed >= 60 and "emergency" not in self._scenario_done:
            self._scenario_done.add("emergency")
            airliners = [a for a in self.aircraft if a.kind == "airliner"]
            if not airliners:
                return
            ac = airliners[0]
            ac.squawk = "7700"
            ac.emergency = True
            ac.target_alt = 9000
            ac.waypoint = self.center
            ac.fixed_route = [destination(self.center[0], self.center[1], 180, 15)]
        if self.elapsed >= 100 and "dark" not in self._scenario_done:
            self._scenario_done.add("dark")
            cargo = [v for v in self.vessels if v.kind == "cargo"]
            if cargo:
                cargo[len(cargo) // 2].dark = True
        if self.elapsed >= 400 and "undark" not in self._scenario_done:
            self._scenario_done.add("undark")
            for v in self.vessels:
                v.dark = False

    def tracks(self, now: datetime, include_sea: bool = True) -> list[Track]:
        out = [a.track(now) for a in self.aircraft]
        if include_sea:
            out.extend(v.track(now) for v in self.vessels if not v.dark)
        return out


class DemoFeed(Feed):
    """Drives the DemoWorld and reports as if it were all five live feeds."""

    name = "demo"
    label = "Demo world"
    mode = "demo"

    def __init__(self, ctx, settings: Settings) -> None:
        super().__init__(ctx, settings, poll_seconds=5.0)
        self.world = DemoWorld(settings, seed=settings.demo.seed)
        self.ticks = 0
        self.last_events = -1e9

    async def run(self) -> None:
        import asyncio
        import logging
        for name, label in (("adsb", "ADS-B aircraft"), ("ais", "AIS vessels"), ("nws", "NWS weather alerts"),
                            ("firms", "NASA FIRMS fires"), ("usgs", "USGS earthquakes")):
            self.ctx.feed_starting(name, label, "demo")
        try:
            while True:
                try:
                    await self.poll()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logging.getLogger("kestrel.feeds.demo").exception("demo tick failed")
                await asyncio.sleep(self.poll_seconds)
        finally:
            await self.aclose()

    async def poll(self) -> int:
        now = utcnow()
        dt = 0.0 if self.ticks == 0 else self.poll_seconds
        self.world.step(dt)
        self.ticks += 1
        include_sea = self.ticks % 2 == 1
        tracks = self.world.tracks(now, include_sea=include_sea)
        for t in tracks:
            self.emit_track(t)
        if self.world.elapsed - self.last_events >= 60 or self.ticks == 1:
            self.last_events = self.world.elapsed
            for e in self.world.events:
                self.emit_event(e)
        air = sum(1 for t in tracks if t.domain == "air")
        sea = sum(1 for t in tracks if t.domain == "sea")
        self.ctx.feed_ok("adsb", "ADS-B aircraft", air, mode="demo", detail="synthetic traffic")
        if include_sea:
            self.ctx.feed_ok("ais", "AIS vessels", sea, mode="demo", detail="synthetic traffic")
        self.ctx.feed_ok("nws", "NWS weather alerts", sum(1 for e in self.world.events if e.kind == "alert"), mode="demo")
        self.ctx.feed_ok("firms", "NASA FIRMS fires", sum(1 for e in self.world.events if e.kind == "fire"), mode="demo")
        self.ctx.feed_ok("usgs", "USGS earthquakes", sum(1 for e in self.world.events if e.kind == "quake"), mode="demo")
        return len(tracks)
