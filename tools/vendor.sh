#!/usr/bin/env bash
# Fetch the browser libraries into kestrelcop/web/static/vendor/ for offline / air-gapped deployments.
# Without these files the server redirects to the same pinned CDN URLs, so this step is optional online.
set -euo pipefail
cd "$(dirname "$0")/../kestrelcop/web/static/vendor"
MAPLIBRE=5.8.0
MILSYMBOL=3.0.4
curl -fsSL -o maplibre-gl.js  "https://unpkg.com/maplibre-gl@${MAPLIBRE}/dist/maplibre-gl.js"
curl -fsSL -o maplibre-gl.css "https://unpkg.com/maplibre-gl@${MAPLIBRE}/dist/maplibre-gl.css"
curl -fsSL -o milsymbol.js    "https://unpkg.com/milsymbol@${MILSYMBOL}/dist/milsymbol.js"
ls -la maplibre-gl.js maplibre-gl.css milsymbol.js
echo "vendored maplibre-gl ${MAPLIBRE} and milsymbol ${MILSYMBOL}; Kestrel will now serve them locally."
