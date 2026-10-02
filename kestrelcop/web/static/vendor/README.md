# Vendored browser libraries

Kestrel's page loads MapLibre GL JS and milsymbol from `/static/vendor/`. When a file is missing here the
server answers with a redirect to the pinned copy on unpkg, so an internet-connected deployment needs
nothing in this folder. For an offline or air-gapped deployment run `tools/vendor.sh` once on a connected
machine and ship the folder with the code; everything else the page needs (fonts, label glyphs, coastlines)
is already in the repository.

Files fetched by `tools/vendor.sh`:

| File | Source | Licence |
| --- | --- | --- |
| `maplibre-gl.js`, `maplibre-gl.css` | https://unpkg.com/maplibre-gl@5.8.0/dist/ | BSD-3-Clause |
| `milsymbol.js` | https://unpkg.com/milsymbol@3.0.4/dist/ | MIT |
