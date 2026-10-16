# Where the cameras are

The quickest way to any of these is the **Quick select** list at the top of the console's area dialog (click the area
name in the top bar): it carries the same entries as the tables below and fills in the name, centre and radius.

Kestrel only keeps the cameras inside the configured area, so the picture has to be over a covered region to
show any. This page lists ready-made areas for every camera source, with a centre and radius you can paste into
the console's area dialog (click the area name in the top bar, or right-click the map) or into `area` in
`config/kestrel.yaml`. [CHANGING_AREA.md](CHANGING_AREA.md) explains the move itself; [CAMERAS.md](CAMERAS.md)
explains the sources.

Camera counts are approximate and change as agencies add and retire cameras.

## No key needed

| Area | Centre `[lat, lon]` | Radius (nm) | Source | Cameras |
| --- | --- | --- | --- | --- |
| Ottawa / National Capital Region | `[45.42, -75.69]` | 60 | City of Ottawa and Ontario MTO | about 430 |
| Toronto / Golden Horseshoe | `[43.65, -79.38]` | 60 | City of Toronto | about 340 |
| Ottawa and Toronto in one picture | `[44.60, -77.50]` | 150 | both cities | about 760 |
| San Francisco Bay Area | `[37.80, -122.27]` | 60 | Caltrans District 4 | the densest district; many cameras also offer a live HLS stream |
| Los Angeles basin | `[34.05, -118.25]` | 70 | Caltrans Districts 7 and 8 | several hundred |
| Sacramento / Central Valley | `[38.58, -121.49]` | 80 | Caltrans Districts 3 and 10 | several hundred |
| San Diego | `[32.72, -117.16]` | 40 | Caltrans District 11 | over a hundred |
| Whole of California | `[37.00, -119.50]` | 350 | all twelve Caltrans districts | several thousand; lower `max_cameras` if the map feels heavy |
| Vancouver / Lower Mainland | `[49.25, -123.10]` | 60 | DriveBC | Lower Mainland highways and the Sea to Sky |
| Kamloops / Coquihalla / Okanagan | `[50.20, -120.00]` | 90 | DriveBC | the mountain passes, the classic winter cameras |
| Whole of British Columbia | `[54.00, -124.00]` | 400 | DriveBC | about 1,100 |

## With a free key

| Area | Centre `[lat, lon]` | Radius (nm) | Source | Key |
| --- | --- | --- | --- | --- |
| Seattle / Puget Sound | `[47.61, -122.33]` | 60 | WSDOT | access code from https://wsdot.wa.gov/traffic/api/ (instant) |
| Washington State | `[47.40, -120.50]` | 200 | WSDOT | as above |
| Ontario, province-wide | `[46.50, -80.00]` | 400 | Ontario 511 (MTO) | developer key from https://511on.ca/developers |
| Greater Toronto highways (401, 427, 404, QEW) | `[43.75, -79.40]` | 40 | Ontario 511 | as above; the City of Toronto source covers city streets only |
| Calgary | `[51.05, -114.07]` | 60 | Alberta 511 | developer key from https://511.alberta.ca/developers |
| Edmonton | `[53.55, -113.49]` | 60 | Alberta 511 | as above |
| Alberta, province-wide | `[54.50, -115.00]` | 350 | Alberta 511 | as above |
| Milwaukee / Madison | `[43.30, -88.50]` | 70 | Wisconsin 511 | key from https://511wi.gov/developers |
| Atlanta | `[33.75, -84.39]` | 50 | Georgia 511 | key from https://511ga.org/developers |
| Phoenix | `[33.45, -112.07]` | 50 | Arizona 511 | key from https://az511.gov/developers |

Put the key in the environment and reference it from the `cars` list in `config/kestrel.yaml`; entries with an
empty key are skipped, so the example list in `config/kestrel.example.yaml` is safe to copy as is.

## Things to keep in mind

- **The radius governs every feed**, not just cameras. A 400 nm picture over British Columbia also pulls all the
  ADS-B traffic in it. Use the smaller areas for day-to-day watching and the province-wide ones to explore.
- **Zones are cleared on a move** unless you tick "keep the configured zones" in the dialog; they are fixed
  coordinates and rarely make sense elsewhere.
- **NWS weather alerts are US-only.** Over Canadian areas the row shows zero, which is correct, not a fault.
- **Cameras arrive on the next poll**, up to 15 minutes after a move. Restarting the server fetches them at once.
- **A source showing 0 in the Feeds panel** usually means the area is simply outside it, for example DriveBC
  reporting 0 while the picture is over Ottawa. A source showing *failed* means its site was unreachable; the
  other sources still load.
- **No keyless source covers the US East Coast yet.** Hampton Roads, the default demo area, has no cameras
  until a Virginia or Maryland source is added; [CAMERAS.md](CAMERAS.md) describes how to add one.
