# Adding a new feed to Kestrel COP

This guide is for whoever administers a Kestrel deployment and needs to bring a new data source onto the
picture: another receiver, a different public API, an internal tracker, a sensor network. It walks through
the pieces a feed touches, gives a complete worked example you can copy, and lists the conventions that keep
the rest of the system (rules, CoT output, the console, the watch officer) working without changes.

Read [ARCHITECTURE.md](ARCHITECTURE.md) first if you have not: it explains the data flow that a feed plugs
into. The short version:

```
your adapter ──► Track | Event ──► pipeline.ingest_*() ──► store ──► rules ──► alerts
                                        │
                                        └──► bus ──► console (WebSocket/SSE) · TAK publisher (CoT) · watch officer tools
```

A feed's only job is to turn whatever its source speaks into `Track` or `Event` objects and hand them to the
pipeline. Everything downstream is generic.

## 1. Decide what the feed produces

| Your source reports… | Build | Notes |
| --- | --- | --- |
| Moving things with a position that changes over time (aircraft, vessels) | `Track` | Needs a stable identity per thing so updates replace earlier reports. Dead-reckoned and trailed on the map. |
| Things that happen or exist at a place (a detection, a warning, an area) | `Event` | Point or polygon. Has an expiry. Not dead-reckoned. |

The models live in [`kestrelcop/models.py`](../kestrelcop/models.py). The fields that matter most:

**Track**

| Field | Required | What to put there |
| --- | --- | --- |
| `uid` | yes | `"<source>-<stable id>"`, e.g. `adsb-a1b2c3`, `ais-367123456`. The same thing must always get the same uid; this is how updates merge. |
| `domain` | yes | `"air"` or `"sea"`. These are the only two domains today (see section 8 for adding one). |
| `source` | yes | The feed's `name`. |
| `lat`, `lon` | yes | Decimal degrees, WGS84. |
| `sidc`, `cot_type` | yes | Get them from `air_codes()` / `sea_codes()` in `kestrelcop/cot/sidc.py`; never hand-write them. |
| `affiliation` | no | `friend`, `neutral` (default), `unknown`, `suspect`, `hostile`. |
| `function` | no | A key of `AIR_FUNCTIONS` or `SEA_FUNCTIONS` in `sidc.py`: `fixed_wing`, `rotary`, `mil_fixed_wing`, `merchant`, `cargo`, `tanker`, `fishing`, `law_enforcement`, … Drives the 2525 symbol. |
| `callsign` | no | What the label on the map shows. Falls back to the uid. |
| `alt_m`, `speed_kts`, `course_deg`, `heading_deg` | no | Metres, knots, degrees true. Speed and course drive dead reckoning and leader lines. |
| `flags` | no | `MILITARY` is set by feeds; `EMERGENCY`, `DARK`, `IN_ZONE:<name>` are set by the rules engine, leave them alone. |
| `attrs` | no | Any JSON-serialisable extras. Shown on the track card and passed to the watch officer. |
| `ts` | no | UTC time of the report. Default is now; set it from the record when the source gives one. |

**Event**

| Field | Required | What to put there |
| --- | --- | --- |
| `uid` | yes | `"<source>-<stable id>"`. Repeated reports of the same thing update it rather than duplicating it. |
| `kind` | yes | `fire`, `quake`, `alert` (weather-style warning with an area) or `zone`. Adding a kind touches several files, see section 8. |
| `source`, `title`, `lat`, `lon` | yes | Title is what the map label and the alert text use. |
| `geometry` | no | A GeoJSON geometry for areas (polygons are drawn as shapes in the console and in ATAK). |
| `severity` | no | Free text, but the rules and the console look for `high` / `moderate` / `low` on points and `extreme` / `severe` / `moderate` on `alert` areas. |
| `magnitude` | no | Used by the quake rule and the quake label. |
| `expires` | no | When the store should drop it. Set this; otherwise the event stays until the process restarts. |
| `cot_type` | no | Defaults per kind from `EVENT_COT_TYPES`. |

## 2. Polled or streaming?

- **Polled** (an HTTP endpoint you fetch every N seconds): subclass `Feed` and implement `poll()`. The base class
  gives you an `httpx.AsyncClient` as `self.http`, the loop, exponential backoff on errors, and health reporting.
  ADS-B, NWS, FIRMS and USGS all work this way.
- **Streaming** (WebSocket, TCP socket, MQTT): subclass `Feed` and override `run()` instead. You own the
  reconnect loop and must call the health methods yourself. `AisFeed` in
  [`kestrelcop/feeds/ais.py`](../kestrelcop/feeds/ais.py) is the template; section 6 summarises the contract.

## 3. The checklist

Every feed touches these places. Do them in order and the feed appears in the console, the health API, the
rules and the watch officer's `feed_health` tool with no further wiring.

1. **Config class** in `kestrelcop/config.py`: a pydantic model with at least `enabled`, plus whatever the source
   needs (URL, key, poll interval). Add it as a field on `FeedsConfig`.
2. **YAML template** in `config.py` (the `EXAMPLE_YAML` string that `kestrel init-config` writes) and
   `config/kestrel.example.yaml`: a commented block for the new feed. Secrets are written as `${ENV_VAR}`;
   the loader expands them from the environment, so keys never live in the file.
3. **Adapter module** `kestrelcop/feeds/<name>.py` with
   - a pure parser `track_from_<x>()` or `event_from_<x>()` that takes one raw record and returns a model or
     `None`. No I/O in here; this is what the tests exercise.
   - a `Feed` subclass with `name`, `label`, and `poll()` (or `run()`).
4. **Export** it from `kestrelcop/feeds/__init__.py`.
5. **Register** it in `Pipeline.build_feeds()` in `kestrelcop/pipeline.py`, following the existing pattern: append the
   feed when enabled, otherwise call `self.feed_off(name, label, "disabled in config")` so the console shows the row
   as off instead of omitting it.
6. **Console order**: add the feed `name` to the `order` list in `renderFeeds()` in `kestrelcop/web/static/app.js`.
   Unlisted feeds still render, at the end of the panel.
7. **Demo world** (optional): if you want the feed row to exist in `kestrel demo`, add `(name, label)` to the tuple in
   `DemoFeed.run()` in `kestrelcop/feeds/demo.py`, and generate synthetic records in `DemoWorld` if the demo should
   show data for it. Skipping this is fine; the row simply does not appear in demo mode.
8. **Tests** in `tests/test_feeds.py`: one real record from the source through the parser, asserting uid, domain or
   kind, position and the derived symbol codes. Include a malformed record that must return `None`.
9. **Docs**: a row in the README's configuration table and a line under "Five open feeds" if it is public data.

## 4. Worked example: a polled track feed

Suppose you run your own AIS receiver (AIS-catcher, rtl_ais behind a small web server, a commercial unit) that
exposes the current ship list as JSON over HTTP, and you want it on the picture alongside aisstream.io. The
record shape below is illustrative; adapt the field names to what your receiver actually emits.

```json
{"mmsi": 367123456, "shipname": "USCGC FORWARD", "lat": 36.95, "lon": -76.33,
 "speed": 11.2, "course": 182.0, "heading": 181, "shiptype": 35, "last_signal": 4}
```

### 4.1 Config

```python
# kestrelcop/config.py

class LocalAisConfig(BaseModel):
    enabled: bool = False
    url: str = "http://127.0.0.1:8100/ships.json"   # your receiver's JSON endpoint
    poll_seconds: float = 5.0
    max_age_seconds: float = 600.0                   # ignore ships last heard longer ago than this


class FeedsConfig(BaseModel):
    adsb: AdsbConfig = AdsbConfig()
    ais: AisConfig = AisConfig()
    local_ais: LocalAisConfig = LocalAisConfig()     # new
    nws: NwsConfig = NwsConfig()
    firms: FirmsConfig = FirmsConfig()
    usgs: UsgsConfig = UsgsConfig()
```

And in the YAML template (both `EXAMPLE_YAML` in `config.py` and `config/kestrel.example.yaml`):

```yaml
  local_ais:
    enabled: false
    url: http://127.0.0.1:8100/ships.json   # a local AIS receiver's ship list
    poll_seconds: 5
    max_age_seconds: 600
```

### 4.2 Adapter

```python
# kestrelcop/feeds/local_ais.py
"""Vessels from a local AIS receiver that publishes its ship list as JSON over HTTP."""

from __future__ import annotations

from datetime import timedelta

from ..cot.sidc import sea_codes
from ..models import Track, utcnow
from .ais import classify_vessel
from .base import Feed


def track_from_ship(rec: dict, now=None, max_age_seconds: float = 600.0) -> Track | None:
    """One receiver record in, one Track out. Pure: no I/O, so it is unit-testable on a saved record."""
    now = now or utcnow()
    lat, lon, mmsi = rec.get("lat"), rec.get("lon"), rec.get("mmsi")
    if lat is None or lon is None or not mmsi:
        return None
    age = float(rec.get("last_signal") or 0)
    if age > max_age_seconds:
        return None
    ship_type = rec.get("shiptype")
    name = (rec.get("shipname") or "").strip() or None
    affiliation, function = classify_vessel(ship_type, name)   # reuse the AIS feed's ship-type and name rules
    sidc, cot_type = sea_codes(affiliation, function)
    return Track(
        uid=f"ais-{int(mmsi)}",            # same uid scheme as aisstream: the two sources merge per vessel
        domain="sea", source="local_ais", callsign=name,
        lat=float(lat), lon=float(lon),
        speed_kts=float(rec["speed"]) if rec.get("speed") is not None else None,
        course_deg=float(rec["course"]) if rec.get("course") is not None else None,
        heading_deg=float(rec["heading"]) if rec.get("heading") not in (None, 511) else None,
        affiliation=affiliation, function=function, sidc=sidc, cot_type=cot_type,
        attrs={"mmsi": int(mmsi), "ship_type": ship_type, "receiver": "local"},
        ts=now - timedelta(seconds=age),
    )


class LocalAisFeed(Feed):
    name = "local_ais"
    label = "AIS (local receiver)"

    def __init__(self, ctx, settings) -> None:
        super().__init__(ctx, settings, poll_seconds=settings.feeds.local_ais.poll_seconds)
        self.cfg = settings.feeds.local_ais

    async def poll(self) -> int:
        resp = await self.http.get(self.cfg.url)
        resp.raise_for_status()                      # any exception here is logged, reported, and retried with backoff
        now = utcnow()
        count = 0
        for rec in resp.json().get("ships") or []:
            t = track_from_ship(rec, now, self.cfg.max_age_seconds)
            if t:
                self.emit_track(t)
                count += 1
        return count                                 # becomes "items" in the health panel and the feed rate
```

Two design choices worth copying:

- **Reuse the classification helpers.** `classify_vessel()` already encodes the ITU ship type table (through
  `ais_function()`) and the name heuristics for government and law-enforcement vessels. A new ADS-B source should reuse
  `classify_aircraft()` and `adsb_function()` the same way, so symbols stay consistent across sources.
- **Share the uid scheme when it is the same thing.** Using `ais-<mmsi>` means a vessel heard by both your
  receiver and aisstream.io is one track, updated by whichever reported last. Use a distinct prefix only when the
  identities really are different.

### 4.3 Register and export

```python
# kestrelcop/feeds/__init__.py
from .local_ais import LocalAisFeed
__all__ = [..., "LocalAisFeed"]

# kestrelcop/pipeline.py, in build_feeds(), next to the AIS block
        if f.local_ais.enabled:
            feeds.append(LocalAisFeed(self, self.settings))
        else:
            self.feed_off("local_ais", "AIS (local receiver)", "disabled in config")
```

```js
// kestrelcop/web/static/app.js, in renderFeeds()
const order = ['adsb', 'ais', 'local_ais', 'nws', 'firms', 'usgs'];
```

### 4.4 Test

```python
# tests/test_feeds.py  (add `timedelta` to the datetime import at the top of the file)
from kestrelcop.feeds.local_ais import track_from_ship

SHIP = {"mmsi": 367123456, "shipname": "USCGC FORWARD", "lat": 36.95, "lon": -76.33,
        "speed": 11.2, "course": 182.0, "heading": 181, "shiptype": 35, "last_signal": 4}


def test_local_ais_record_becomes_a_track():
    t = track_from_ship(SHIP, now=NOW)
    assert t.uid == "ais-367123456" and t.domain == "sea" and t.callsign == "USCGC FORWARD"
    assert t.affiliation == "friend" and t.function == "law_enforcement"   # the USCGC name rule beats ship type 35
    assert t.sidc == "SFSPXL---------" and t.cot_type == "a-f-S-X-L"
    assert t.ts == NOW - timedelta(seconds=4)
    assert track_from_ship({"mmsi": 1, "lat": None}, now=NOW) is None
    assert track_from_ship({**SHIP, "last_signal": 9999}, now=NOW) is None
```

Run `pytest -q`, then `kestrel serve` with the feed enabled and check:

- the row in the Feeds panel goes `starting` then `ok` and shows a rate;
- `GET /api/feeds` lists it; `GET /api/tracks` contains entries whose `source` is `local_ais`;
- `GET /api/cot/ais-367123456` returns well-formed CoT;
- ask the watch officer "which feeds are live?" and it names yours.

## 5. Event feeds

The USGS adapter in [`kestrelcop/feeds/usgs.py`](../kestrelcop/feeds/usgs.py) is the smallest complete example
of an event feed and is worth reading in full. Points that differ from tracks:

- **Expiry.** Set `expires`. The store's sweep drops events past it; tracks instead age out by domain
  (`store.stale_seconds_air` / `stale_seconds_sea` in the config).
- **Area filtering is yours to do.** The base class offers `bbox(center, radius_km)` for sources that accept a
  bounding box, and `kestrelcop.geo.haversine_km` for filtering after the fact. `settings.area.center` and
  `settings.area.radius_km` describe the configured picture.
- **Severity conventions.** The console colours point events by `high` / `moderate` / `low` and area events by
  `extreme` / `severe` / `moderate`. The rules engine raises alerts for quakes above `rules.quake_alert_magnitude`,
  for `alert` events with severity severe or extreme, and for fires with `attrs["frp"]` at or above 50. If your
  source should raise alerts on different criteria, add a rule (see ARCHITECTURE.md, "A new rule").
- **Polygons.** Put a GeoJSON geometry in `geometry` and keep `lat`/`lon` as a representative point (centroid or
  the first vertex). The CoT encoder turns polygons into ATAK drawn shapes with `<link>` vertices automatically.

## 6. Streaming feeds

When you override `run()`, you take over the contract the base class normally fulfils. Follow `AisFeed`:

1. If a required key or optional package is missing, call `self.ctx.feed_off(name, label, reason)` and return.
   The reason is shown verbatim in the console, so say what to set.
2. Call `self.ctx.feed_starting(name, label)` once before connecting.
3. In the connect loop: on connect, `feed_ok(name, label, 0, detail="connected")`. While messages flow, call
   `feed_ok(name, label, n)` about every five seconds with the count received since the last call; this is what
   refreshes the "last message" age and the rate. Do not call it per message.
4. On any error or stream close, `feed_error(name, label, message)`, sleep with exponential backoff, reconnect.
   Re-raise `asyncio.CancelledError`; swallow everything else so a bad source never kills the picture.
5. Parse each message in a pure `handle()` / `track_from_*()` function that the tests can call directly.

For a streaming feed `feed_starting` gets no `poll_seconds`, so the global `rules.stale_feed_seconds` (120 s by
default) is its stale window. If your stream is legitimately quiet for longer than that, pass
`poll_seconds=<expected gap>` to `feed_starting`; the pipeline derives a per-feed window from it.

## 7. Health, staleness and what the numbers mean

- `feed_starting` → the row shows `starting`. For polled feeds the base class passes `poll_seconds`, and the
  pipeline sets the feed's stale window to `max(rules.stale_feed_seconds, 2 × poll_seconds + 30)`. A feed polled
  every 15 minutes is therefore not called stale after two.
- `feed_ok(items)` → status `ok`, "last message" set to now, `items` = what that poll returned (shown to the watch
  officer as `items_last_poll`; it is not a total). `messages_total` accumulates and `rate_per_min` is a rolling
  one-minute sum.
- `feed_error(message)` → `down` if the feed has never delivered, `degraded` otherwise. The base class keeps
  polling with backoff up to five minutes.
- A feed in `ok` with nothing delivered for longer than its stale window is marked `degraded` by the pipeline, and
  the rules engine raises a MEDIUM "Feed stale" alert once. It clears on the next successful poll.

A poll that succeeds with zero items is a healthy poll: return `0`, do not raise.

## 8. Going beyond the current model

Some sources need the model itself extended. Each of these is a small, mechanical change, but it touches more
than one file.

**A new track function** (say `glider` or `tug`): add a row to `AIR_FUNCTIONS` or `SEA_FUNCTIONS` in
`kestrelcop/cot/sidc.py` with its 2525C function id and CoT suffix, and map to it from your parser. milsymbol
draws the symbol from the SIDC, so no console change is needed. Add a case to `tests/test_cot.py`.

**A new event kind** (say `volcano` or `outage`):

1. `EventKind` literal in `kestrelcop/models.py`.
2. `EVENT_COT_TYPES` in `kestrelcop/cot/sidc.py` (what TAK clients receive).
3. The `kind` enum in `TOOL_SPECS` for `list_events` in `kestrelcop/ai/tools.py`, so the watch officer can filter on it.
4. The keyword table in `mock_ask()` in `kestrelcop/ai/mock.py` if the keyless officer should recognise it.
5. The console: an icon branch in `drawEventIcon()` and a card body line in the event card builder in
   `kestrelcop/web/static/app.js`; a legend entry in `index.html` if it deserves one.
6. A rule in `kestrelcop/rules/engine.py` if the kind should raise alerts.

**A new domain** (say `ground` for vehicles) is the largest change: the `Domain` literal, a `ground_codes()`
function in `sidc.py` with its own function table (2525 battle dimension `G`), a stale window in `StoreConfig` and
`Store`, the `domain` enum in `TOOL_SPECS`, the console's layer toggle and count, and the dead-reckoning
assumptions in `app.js`. Plan it as its own change with tests at each layer.

## 9. Conventions and pitfalls

- **Pure parsers.** Everything that turns a raw record into a model is a plain function with no network, no
  clock access except an injectable `now`, and no settings object. That is what makes one saved record a test.
- **Never invent symbol codes.** Always derive `sidc` and `cot_type` from `air_codes()` / `sea_codes()`.
- **Timestamps are UTC** `datetime` objects. If the source gives an age ("seen 4 s ago"), subtract it from `now`;
  if it gives an epoch, convert with `tz=timezone.utc`.
- **Keep `attrs` JSON-serialisable** (strings, numbers, booleans, lists, dicts) and drop `None` values; they are
  sent to the browser and the model as-is.
- **Secrets come from the environment.** Reference them as `${VAR}` in YAML, read them from the config object,
  and when they are missing call `feed_off` with a message naming the variable.
- **Be a good client.** `self.http` already sends a Kestrel user agent and follows redirects. Respect the
  source's rate limits with `poll_seconds`; the backoff on errors is automatic. Public services such as adsb.lol
  and api.weather.gov will block abusive pollers.
- **Filter to the area.** Sources that return the whole world should be trimmed with the configured centre and
  radius before emitting, or the picture, the TAK stream and the watch officer's snapshot fill with noise.
- **Return `None` early** from parsers for records without a position; the pipeline has nothing to do with them.
- **The feed `name` is an identifier**: lowercase, no spaces, and stable, because it is the uid prefix, the
  `source` field, a config key and the console's sort key all at once. The `label` is the human-readable one.

## 10. Verification checklist

- [ ] `pytest -q` passes, including the new parser test with a real record and a malformed one.
- [ ] `kestrel serve` shows the feed `ok` with a plausible rate; `kestrel serve` with the feed disabled shows it `off · disabled in config`.
- [ ] With the key or URL unset, the row says what to set (not a traceback in the log).
- [ ] Stopping the source makes the row `degraded` and raises one "Feed stale" alert; restoring it clears the alert.
- [ ] `GET /api/cot/<uid>` for one of its items is accepted by a TAK client if TAK output is in use.
- [ ] `kestrel ask "what is the newest <thing> on the picture?"` cites a uid from the feed.
- [ ] README configuration table and `config/kestrel.example.yaml` document the new block.
