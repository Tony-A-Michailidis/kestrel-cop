## 🚀 Quick start

Two ways to run Kestrel: the **demo world** (synthetic traffic, no keys, no internet) and **live feeds** (real
data, free keys). Both start with the same one-time install.

### Install once

```bash
git clone https://github.com/Tony-A-Michailidis/kestrel-cop
cd kestrel-cop
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

On WSL, clone into the Linux filesystem (`~/kestrel-cop`) rather than `/mnt/c/...`: startup and tests are many
times faster there.

### Procedure 1: the demo world

Every cold start, from a fresh terminal:

```bash
cd kestrel-cop
source .venv/bin/activate                # Windows: .venv\Scripts\activate
export ANTHROPIC_API_KEY=sk-ant-...      # optional: without it a clearly-labelled mock watch officer runs
kestrel demo
```

Wait for `Uvicorn running on http://127.0.0.1:8000`, then open http://127.0.0.1:8000/. Within the first few
minutes an airliner squawks 7700 and diverts, a light aircraft blunders into ROZ ALPHA, and a cargo ship goes
dark on AIS. Watch the alerts arrive, click one for its SPOTREP, then ask the watch officer *"What is within
25 km of the emergency aircraft?"* and click the citations. With the key set, the badge in the top bar shows
the model name instead of *AI mock*, SITREPs become real assessments, and the tool trace under each answer
shows the model working the picture. Stop with `Ctrl+C`.

### Procedure 2: live feeds

One-time preparation:

```bash
kestrel init-config                      # writes config/kestrel.yaml (never committed; keys stay out of it)
```

Then get the free keys: [aisstream.io](https://aisstream.io) for vessels and
[NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/api/map_key/) for fires, and set `enabled: true` under
`feeds.firms` in `config/kestrel.yaml`. ADS-B (adsb.lol), NWS weather and USGS earthquakes need no key.

Every cold start, from a fresh terminal:

```bash
cd kestrel-cop
source .venv/bin/activate                # Windows: .venv\Scripts\activate
export ANTHROPIC_API_KEY=sk-ant-...      # the word export matters on every line
export AISSTREAM_API_KEY=...
export FIRMS_MAP_KEY=...
kestrel serve --config config/kestrel.yaml
```

The startup line lists the feeds that are on and says `AI: Claude (...)`; if it says *mock*, an `export` is
missing. Wait for `Uvicorn running on http://127.0.0.1:8000`, then open http://127.0.0.1:8000/ (press
`Ctrl+Shift+R` once if the page looks stale after an upgrade). Stop with `Ctrl+C`. To avoid retyping the keys,
put the three `export` lines in `~/.bashrc`, or copy `.env.example` to `.env` and `set -a; source .env; set +a`.

The default area is Hampton Roads / the Chesapeake approaches (Norfolk, VA): dense civil and military air
traffic, a major port, NWS marine zones, and the Great Dismal Swamp for the occasional fire. Click the area
name in the top bar (or right-click the map) to move the picture anywhere else, or edit `area` in the config;
[docs/CHANGING_AREA.md](docs/CHANGING_AREA.md) covers both and what each feed does outside the US. A second
screenshot shows live data from the Ottawa, Ontario, Canada area.

