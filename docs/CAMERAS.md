# Traffic cameras on the picture

Kestrel draws open traffic and roadside cameras on the map, shows their current still image on demand, and
lets the watch officer look at a camera and tell you what it sees. This page explains where the cameras come
from, what the licences allow, how the imagery flows, and how to ask the officer about them.

The feeds are **still images refreshed by the agencies every one to fifteen minutes**, not video. A few
sources (Caltrans) also publish a live HLS stream, which the camera card links to.

## Sources

| Source | Coverage | Key | Refresh | Licence |
| --- | --- | --- | --- | --- |
| DriveBC | British Columbia highways, about 1,100 cameras | none | typically 15 min | Open Government Licence, British Columbia |
| Caltrans CWWP2 | California state highways, all 12 districts | none | 1 to 5 min; many with an HLS stream | Caltrans: "no charge for the use of this data" |
| City of Toronto | city streets, about 340 cameras | none | 2 to 3 min | Open Government Licence, Toronto |
| City of Ottawa | city streets plus Ontario MTO cameras around Ottawa, about 430 | none | 60 s | City of Ottawa open data terms |
| WSDOT | Washington State highways | free access code | a few minutes, snapshots only | WSDOT Traveler Information terms |
| 511 sites on the CARS platform | Ontario, Alberta, Wisconsin, Georgia, Arizona and others | free developer key per site | varies | each site's developer agreement; ten calls a minute |

Only cameras inside the configured area are kept ([CAMERA_AREAS.md](CAMERA_AREAS.md) lists ready-made areas with
centre and radius for every source), so a picture over Norfolk shows none of these until you add
a Virginia source, while one over Ottawa gets the city and MTO cameras immediately. Move the picture (see
[CHANGING_AREA.md](CHANGING_AREA.md)) and the camera set follows on the next poll.

Sources that were looked at and left out: Florida 511 (no public API; the mirror's terms allow personal use
only), the NYC DOT camera API (unreachable from the development network, so the
NYC adapter was not written), the FAA weather cameras (no
published API), and commercial aggregators such as Windy, whose terms forbid redistribution.

## How the imagery flows

1. Every 15 minutes the `cameras` feed fetches each enabled source's camera list concurrently, filters to the
   area, and emits each camera as an event of kind `camera` with the agency's image URL in its attributes.
   One source failing is logged and skipped; the feed only reports an error when all of them fail. The Feeds
   panel shows the per-source counts.
2. The **console** hotlinks the image from the agency when you open a camera, with a cache-busting
   parameter, and refreshes it on the source's cadence. If the agency refuses the browser (referer or
   mixed-content rules), the card falls back to Kestrel's proxy.
3. The **proxy**, `GET /api/camera/{uid}/image`, fetches the current still server-side, with a 20 second
   cache so a burst of viewers is one request to the agency, a 4 MB size cap and a content-type check.
   Images are never written to disk or kept beyond that cache.
4. **TAK** does not receive cameras unless `tak.publish_cameras` is true, because thousands of static markers
   are rarely wanted in ATAK. When enabled they go out as spot markers whose remarks carry the image URL.

The licences above permit display of the agencies' imagery in your own console. Kestrel does not re-host or
redistribute it, which is the line most of them draw.

## The watch officer and cameras

Two tools:

- `find_cameras` returns the nearest cameras to a track, event, callsign, place name or point, with distance
  and bearing.
- `view_camera` fetches one camera's current still and gives it to the model as an image, so Claude can say
  what is visible: traffic density, weather, visibility, light, anything unusual. The frame the officer saw
  appears as a thumbnail in the tool trace under the answer, and clicking it flies the map to that camera.

Standing orders tell the officer to look at one or two well-placed cameras rather than many, to describe only
what is actually visible, to say when a frame is dark or stale, and to cite the camera as `[cam-...]`, which
the citation check verifies like any other uid.

Ways to ask:

- Open a camera card and press **Ask the watch officer what it shows**.
- "What does the camera nearest the emergency aircraft show?"
- "Is it snowing on the 417 right now?"
- "Compare the two cameras closest to UPS556."

Each viewed image costs roughly a thousand to fifteen hundred input tokens, so a question that views four
cameras is still cheap, but a SITREP never views cameras on its own.

Without an API key the mock officer lists the nearest cameras with their uids and says it cannot look at
imagery; the card still shows the still.

## Configuration

```yaml
feeds:
  cameras:
    enabled: true
    poll_seconds: 900
    max_cameras: 4000           # safety cap on what one picture carries
    drivebc: true
    caltrans: true
    caltrans_districts: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    toronto: true
    ottawa: true
    wsdot_access_code: ${WSDOT_ACCESS_CODE}
    cars:
      - { name: ontario, host: https://511on.ca, key: ${ONTARIO_511_KEY} }
      - { name: alberta, host: https://511.alberta.ca, key: ${ALBERTA_511_KEY} }
```

Keys come from the environment like every other secret. A 511 entry with an empty key is skipped, so the
example list costs nothing until you register. To add another CARS site, add a line with its host; the uid
prefix becomes `cam-<name>-`.

Getting the keys: WSDOT at https://wsdot.wa.gov/traffic/api/ (instant), Ontario at https://511on.ca/developers,
Alberta at https://511.alberta.ca/developers, and the same `/developers` path on the other 511 sites.

## Console

- **Cameras** has its own layer toggle and its own count in the top bar. Camera markers thin out automatically
  when zoomed out and never hide tracks.
- Hover a marker for the name and road; click it for the **camera card**: the still with a fetched-at stamp and a
  refresh button, road, direction, place, source and owner, the refresh cadence, and buttons to ask the
  officer, open the live stream when the source has one, and open the image at its source.
- `Esc` closes the card. Citations such as `[cam-ott-49]` in answers and SITREPs fly the map to the camera
  and open its card.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Feeds panel: cameras `ok` but `0` items | No source covers your area. Add a 511 site or WSDOT for it, or widen the radius. |
| Feeds panel: cameras `degraded` | Every enabled source failed; the detail names them. Usually network or an agency outage. |
| Card says "image unavailable" | The agency returned an error for that camera, or it is offline. Other cameras from the same source usually work. The proxy has already been tried. |
| Officer answers "could not fetch the image" | Same as above, seen from the server. The officer reports it rather than guessing. |
| Thousands of cameras slow the map | Lower `max_cameras`, disable a source, or reduce the area radius. |

## Adding a camera source

Write a pure `camera_from_<source>()` parser that returns `_camera(...)` from
[`kestrelcop/feeds/cameras.py`](../kestrelcop/feeds/cameras.py), add a fetch coroutine on `CamerasFeed`, list
it in `_sources()`, and add a config flag. The existing six parsers are each ten to twenty lines, and
[ADDING_FEEDS.md](ADDING_FEEDS.md) covers the general shape.
