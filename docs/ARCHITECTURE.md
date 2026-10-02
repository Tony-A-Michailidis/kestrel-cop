# Kestrel COP architecture

## Data flow

Everything is one process, one event loop, in memory. There is no database by design: the flight recorder
(persistence, replay) is a separate project that will subscribe to the same bus.

```
feed.poll()  ──► ctx.ingest_track(Track) ──► store.upsert_track ──► rules.on_track ──► bus.publish("track")
             ──► ctx.ingest_event(Event) ──► store.upsert_event ──► rules.on_event ──► bus.publish("event")
                                                                     │
pipeline._periodic (5 s) ──► store.sweep()  (stale tracks, expired events) ──► bus.publish("remove")
                         ──► rules.periodic (AIS gaps, stale feeds)          ──► bus.publish("alert")

bus subscribers:  web.Broadcaster  (coalesces tracks/events/feeds into ~300 ms batches for browsers)
                  cot.TakPublisher (encodes each message to CoT and hands it to PyTAK's tx queue)
```

The `Pipeline` object is the `FeedContext` every adapter talks to. Feeds know nothing about the store, the
rules, the web or TAK; they emit models and report their own health.

## Models (`kestrelcop/models.py`)

| Model | Identity | Notes |
| --- | --- | --- |
| `Track` | `adsb-<icao hex>` or `ais-<mmsi>` | Position, kinematics, `affiliation` + `function` → `sidc` + `cot_type`, source attributes (`squawk`, `mmsi`…), `flags` (`EMERGENCY`, `DARK`, `MILITARY`, `IN_ZONE:<name>`), a trail |
| `Event` | `nws-<hash>`, `firms-<lat>-<lon>-<date>-<time>`, `usgs-<id>`, `zone-<slug>` | Points or GeoJSON polygons with severity, magnitude, expiry |
| `Alert` | `alt-<n>` | Raised once per (rule, subject) by the rules engine; carries the SPOTREP once triaged |
| `FeedStatus` | feed name | mode (live/demo/off), status, rate, last message, error |

Flags are owned by the rules engine. The store carries them forward across updates; the engine clears them
when the condition ends (a track that reports again is no longer `DARK`; a squawk change clears `EMERGENCY`).

## Symbology and CoT (`kestrelcop/cot/sidc.py`, `encode.py`)

Both schemes encode affiliation, battle dimension and function, so one table drives both:

| Kestrel function | 2525C SIDC (neutral) | CoT type |
| --- | --- | --- |
| air / fixed_wing | `SNAPCF---------` | `a-n-A-C-F` |
| air / rotary | `SNAPCH---------` | `a-n-A-C-H` |
| air / mil_fixed_wing (friend) | `SFAPMF---------` | `a-f-A-M-F` |
| sea / cargo | `SNSPXMC--------` | `a-n-S-X-M-C` |
| sea / tanker | `SNSPXMO--------` | `a-n-S-X-M-O` |
| sea / fishing | `SNSPXF---------` | `a-n-S-X-F` |
| sea / law_enforcement (friend) | `SFSPXL---------` | `a-f-S-X-L` |
| weather alert polygon | — | `u-d-f` with one `<link point="lat,lon,0"/>` per vertex, `strokeColor`/`fillColor` (signed ARGB), plus the classic `<shape><polyline>` |
| configured circular zone | — | `u-d-c-c` with `<shape><ellipse>` |
| fire / quake / alert marker | — | `b-m-p-s-m`, remarks carry the details |

Affiliation comes from simple, conservative heuristics: the US military ICAO block and a short list of
government callsign prefixes for aircraft; AIS ship type 35/51/55 and coast-guard names for vessels; everything
else is `neutral`, and tracks with no callsign and no category are `unknown`. Change the tables, not the code.

The browser asks milsymbol for symbols with `civilianColor: false`, so civil traffic takes the affiliation
colour instead of 2525's civilian purple; that keeps the handful of military tracks visually distinct.

## Rules (`kestrelcop/rules/engine.py`)

| Rule | Trigger | Severity |
| --- | --- | --- |
| `emergency_squawk` | squawk 7500 / 7600 / 7700 | HIGH |
| `zone_entry` | a track enters a configured circle or polygon | zone's severity |
| `ais_gap` | a vessel that was making > 3 kt has not reported for `ais_gap_minutes` | MEDIUM |
| `earthquake` | magnitude ≥ `quake_alert_magnitude` | MEDIUM / HIGH ≥ 6 |
| `weather_alert` | NWS severity Severe / Extreme | MEDIUM / HIGH |
| `fire_detection` | FIRMS FRP ≥ 50 MW, one alert per ~10 km cell | MEDIUM |
| `feed_stale` | a live feed delivered nothing for `stale_feed_seconds` | MEDIUM |

Every rule fires once per (rule, subject) key until the condition clears. This file is deliberately boring:
it is the standard library of the rules DSL that comes next.

## The watch officer (`kestrelcop/ai/`)

- `tools.py`: the only window the model has onto the picture. `picture_summary`, `search_tracks` (filters,
  radius searches, nearest-first), `get_track`, `list_alerts`, `list_events`, `measure`, `feed_health`. The
  same functions serve the mock officer, so the tool trace is real in both modes.
- `prompts.py`: the standing orders. SITREP shape (seven numbered sections, DATA CONFIDENCE mandatory),
  question answering (tools first, cite every claim, say "insufficient data"), SPOTREP shape.
- `watch_officer.py`: `ask_stream()` is the agentic loop — stream a turn, run the tools it asked for, feed
  results back, repeat up to `max_tool_iterations`. `sitrep_stream()` sends a server-built snapshot (flagged
  and military tracks first) and streams the report. `triage()` writes a SPOTREP per alert with the subject,
  nearby tracks, zones and feed health as context. Every output passes through `verify_citations()`.
- `mock.py`: templated answers from the same tools, labelled MOCK everywhere, so a laptop without a key still
  exercises the entire system (and so the tests are deterministic).

Streaming reaches the browser as server-sent events: `tool` (a call started), `tool_result` (its summary and
duration), `text` (a delta), `done` (the verified `Answer`/`Sitrep`), `error`.

## Web (`kestrelcop/web/`)

Starlette (the layer FastAPI sits on; the surface is small enough that hand-written handlers are clearer).
The `Broadcaster` subscribes to the bus once and fans out to every connected browser, coalescing track
updates into ~300 ms batches; slow clients drop their oldest messages rather than slowing the others. The
same messages go out over `/ws` and `/api/stream` (SSE), and the page falls back from one to the other.

The console is plain HTML/CSS/JS with no build step. Browser libraries come from `/static/vendor/`, which the
server serves locally when present and otherwise redirects to the pinned CDN copy (`tools/vendor.sh` fills the
folder for air-gapped use). Label glyphs (`static/glyphs`, generated by `tools/make_glyphs.py` from the
Inter typeface), UI fonts (`static/fonts`) and the Natural Earth fallback basemap (`static/basemap`, built by
`tools/make_basemap.py`) ship in the repository, so the only external dependency at runtime is the Esri
raster basemap, and the picture survives without it.

## Extending Kestrel

**A new feed.** Subclass `feeds.base.Feed`, implement `poll()` (or override `run()` for a streaming source,
as `AisFeed` does), build `Track`/`Event` objects in a pure `track_from_*` function, call `self.emit_track`,
register the feed in `pipeline.build_feeds()` and add a config block. Write a parser test with one real record.
[ADDING_FEEDS.md](ADDING_FEEDS.md) walks through every step with a complete example.

**A new rule.** Add a method to `RuleEngine` that calls `self._alert(key, severity, rule, title, text, subject)`;
the key deduplicates. Per-track and per-event rules run on ingest, `periodic()` runs every five seconds.

**A new tool for the watch officer.** Add a spec to `TOOL_SPECS` and a `tool_<name>` method on `PictureTools`.
Both officers can call it immediately; teach the mock officer about it in `mock_ask` if it should use it.

**Another consumer of the picture.** `bus.subscribe()` returns a queue of `(topic, payload)` tuples: `track`,
`event`, `alert`, `feed`, `remove`, `sitrep`. The TAK publisher is forty lines; a Kafka or MQTT bridge would be
about the same.
