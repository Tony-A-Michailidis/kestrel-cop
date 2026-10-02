# Changing the area of interest

Kestrel draws one picture around one centre point. This guide is for an administrator who wants to move that
picture somewhere else: it explains the three settings that define the area, what every feed, rule and screen
does with them, and what else you should look at after a move. A worked example at the end moves the picture
from Norfolk, Virginia to Ottawa, Canada.

## 1. The area settings

Everything hangs off one block in `config/kestrel.yaml` (written by `kestrel init-config`; the defaults live in
`AreaConfig` in [`kestrelcop/config.py`](../kestrelcop/config.py)):

```yaml
area:
  name: Hampton Roads / Chesapeake approaches   # free text, shown in the top bar and told to the watch officer
  center: [36.95, -76.30]                       # latitude, longitude in decimal degrees (WGS84)
  radius_nm: 150                                # nautical miles from the centre
```

| Key | What it is | Notes |
| --- | --- | --- |
| `name` | A label | Appears in the console top bar, in the health API, and in every snapshot the watch officer sees, so it also shapes how SITREPs describe the picture. |
| `center` | `[lat, lon]` | Latitude first. West longitudes and south latitudes are negative. Use four decimals; more buys nothing. |
| `radius_nm` | Reach, in nautical miles | Converted to kilometres internally (`radius_km = radius_nm × 1.852`). Everything below uses one of the two. |

Changing the file requires a restart: `Ctrl+C` then `kestrel serve` again. The console reads the new area from
the server's hello message, so reload the browser afterwards as well. Section 1b shows the no-restart way.

## 1b. Changing the area from the console

You do not have to edit the file and restart. In the console, click the area name in the top bar, or
right-click anywhere on the map to use that point as the new centre. The dialog takes a name, the centre,
the radius in nautical miles, and a checkbox to keep the configured zones (off by default, because zones are
fixed coordinates and almost never make sense after a move). **Move picture** applies it at once:

- the feeds are restarted against the new centre and radius, so AIS resubscribes with the new bounding box and
  the next ADS-B, FIRMS and USGS polls use it;
- tracks outside the new area and events far outside it are dropped, so the old picture does not linger;
- every open console is told the new settings, shows a toast and flies home to the new centre;
- the watch officer's next SITREP describes the new area.

The same thing is available to scripts as `POST /api/area` with a JSON body of `name`, `center` (`[lat, lon]`),
`radius_nm` and optional `keep_zones`; `GET /api/area` returns the current values. Bad input gets a 400 and
changes nothing.

**Persistence.** In live mode the change is written next to your config as `config/kestrel.area.yaml` (the
override file; it is gitignored). At startup Kestrel applies that file's `area` and `zones` on top of
`kestrel.yaml`, so your hand-written config keeps its comments and the console's choice survives a restart.
Delete the override file to go back to what the config says. In demo mode there is no config file, so the
change lasts for that run only and the dialog says so.

**Access.** The endpoint has no authentication, like the rest of the API. Anyone who can reach the console can
move the picture. Keep the server bound to localhost or behind an authenticating reverse proxy if that matters.

Everything else in this guide still applies: NWS areas do not follow the move, the home zoom is fixed, and
the suggestion chips still mention Norfolk until you edit them.

## 2. What uses the area

| Component | How it uses the centre and radius |
| --- | --- |
| ADS-B via adsb.lol | Asks for aircraft within `radius_nm` of the centre. adsb.lol caps the distance at 250 nm; a larger radius is silently clamped. |
| ADS-B via OpenSky | Converts the circle to a bounding box and asks for that box. |
| ADS-B via your own receiver (`provider: url`) | Ignores the area; your receiver returns what it hears. |
| AIS via aisstream.io | Subscribes with a bounding box derived from the circle. Vessels outside it never arrive. |
| NWS weather alerts | Does **not** use the area. It uses `feeds.nws.areas`, a list of US state codes (and NWS marine area codes such as `AN` for the western North Atlantic). Change this list by hand when you move. |
| NASA FIRMS | Requests detections inside the bounding box of the circle. |
| USGS earthquakes | Keeps events within three times the radius if they are at or above `feeds.usgs.min_magnitude`, plus anything on earth at or above `feeds.usgs.global_min_magnitude`. |
| Rules: zones | Zones are absolute coordinates in `rules.zones`. They do not move with the centre; see section 3. |
| Console | Opens the map on the centre at a fixed zoom (`HOME_ZOOM` in `app.js`, 7.4, which frames about 150 nm on a typical screen). The home button returns there. |
| Watch officer | Sees the name, centre and radius in every snapshot and in the `picture_summary` tool, and the standing orders tell it to describe the picture in those terms. |
| CoT / TAK | Not affected. Positions are absolute; your TAK clients show whatever Kestrel publishes. |
| Demo world | Has its own centre: `kestrel demo --center LAT,LON`. The scripted scenario (emergency squawk, dark ship, zone entry) is laid out relative to that centre, so it still plays anywhere, over whatever geography is there. |

The one hard dependency on the United States is the NWS feed. Everything else is global.

## 3. After the move: things that do not follow automatically

**Zones.** `rules.zones` holds fixed shapes. A zone from the old area will sit in the ocean or in another
country and never fire. Delete it or redraw it:

```yaml
rules:
  zones:
    - name: ROZ ALPHA          # circle: centre + radius
      type: circle
      center: [45.42, -75.70]
      radius_km: 8
      severity: MEDIUM
    - name: RANGE BRAVO        # polygon: ring of [lon, lat] vertices, longitude first (GeoJSON order)
      type: polygon
      ring: [[-75.95, 45.60], [-75.80, 45.60], [-75.80, 45.70], [-75.95, 45.70], [-75.95, 45.60]]
      severity: HIGH
```

Note the two coordinate orders: `center` is `[lat, lon]` like the area, while polygon `ring` vertices are
`[lon, lat]` because they are passed straight through as GeoJSON.

**NWS areas.** Set `feeds.nws.areas` to the state codes that overlap your new circle, or disable the feed
outside the US so the Feeds panel shows it as off instead of a feed that is forever empty:

```yaml
  nws:
    enabled: false
```

**Radius.** A radius that suited a coastal port may be wrong inland. Shrink it and the console's home zoom
will look too far out; if that bothers you, lower `HOME_ZOOM` in `kestrelcop/web/static/app.js` (one zoom
level halves the width shown). Grow it beyond 250 nm and adsb.lol stops growing with it.

**Watch officer examples.** The suggestion chips under the question box and the placeholder text in
`kestrelcop/web/static/index.html` mention the Norfolk picture ("near the cutter", "What is inside ROZ ALPHA
right now?"). They are only examples, but an operator will trust the console more if they match the area.

**The README screenshot and names.** Purely cosmetic, but the README describes Hampton Roads as the default.

## 4. Expectations by region

What a feed shows depends on what exists where you point it, not on Kestrel:

- **Air** coverage from adsb.lol depends on volunteer receivers. Dense around cities and airports worldwide;
  thin over oceans, deserts and high latitudes. A military field nearby produces the `MILITARY` flag often.
- **Sea** traffic only exists on water. An inland centre with a small radius shows nothing from AIS even
  though the feed is healthy; widen the radius to reach a seaway, a lake or a coast, or disable the feed.
- **Fires** cluster by season and land cover. Zero detections for days is normal in winter or in a city.
- **Earthquakes** follow the seismic map. The regional threshold of 2.5 produces regular events in active zones
  and almost none in stable ones; the global threshold still brings the large ones in.
- **Weather** needs a feed for the local service outside the US. Environment Canada, the Met Office and
  MeteoAlarm all publish CAP alerts; [ADDING_FEEDS.md](ADDING_FEEDS.md) is the recipe, and the NWS adapter is
  a close template because it already parses CAP-style GeoJSON.

## 5. Worked example: Norfolk to Ottawa

Edit `config/kestrel.yaml`:

```yaml
area:
  name: Ottawa / National Capital Region
  center: [45.42, -75.69]      # Parliament Hill
  radius_nm: 120               # reaches Montreal, Kingston and the upper St. Lawrence

feeds:
  nws:
    enabled: false             # US-only service; see ADDING_FEEDS.md for a Canadian alerts feed

rules:
  zones:
    - name: PARLIAMENT ROZ
      type: circle
      center: [45.4236, -75.7009]
      radius_km: 5
      severity: HIGH
```

Then:

```bash
# Ctrl+C the running server
kestrel serve
# reload the browser once "Uvicorn running" appears
```

What to expect:

- ADS-B shows CYOW traffic, Gatineau, and military movements from Petawawa and Trenton within minutes.
- AIS is quiet until the Seaway is inside the radius; at 120 nm you see Montreal harbour and lakers on the
  St. Lawrence. At 40 nm you would see nothing.
- FIRMS works unchanged (export `FIRMS_MAP_KEY`).
- USGS shows the Western Quebec seismic zone: small events every few weeks, occasionally felt.
- The first SITREP names Ottawa and the new radius, because it reads them from the snapshot.

For a demo over the same geography: `kestrel demo --center 45.42,-75.69`.

## 6. Checklist

- [ ] `area.name`, `area.center`, `area.radius_nm` updated; latitude first, west and south negative.
- [ ] `rules.zones` deleted or redrawn for the new area (remember `[lon, lat]` order for polygon rings).
- [ ] `feeds.nws.areas` updated, or the feed disabled outside the US.
- [ ] Radius sensible for the feeds you care about (water inside it for AIS; at most 250 nm for adsb.lol).
- [ ] Server restarted and browser reloaded; top bar shows the new name; home button centres correctly.
- [ ] `GET /api/health` shows every enabled feed `ok` after one poll interval.
- [ ] A SITREP describes the new area.
