"""Command line: `kestrel demo`, `kestrel serve`, `kestrel init-config`, `kestrel sitrep`, `kestrel ask`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import EXAMPLE_YAML, Settings


def _serve(settings: Settings, host: str | None, port: int | None) -> None:
    import uvicorn

    from .web import create_app

    app = create_app(settings)
    uvicorn.run(app, host=host or settings.web.host, port=port or settings.web.port, log_level="info")


def cmd_demo(args: argparse.Namespace) -> None:
    center = None
    if args.center:
        lat, lon = (float(x) for x in args.center.split(","))
        center = (lat, lon)
    settings = Settings.for_demo(center=center, tak_url=args.tak)
    settings.demo.seed = args.seed
    settings.demo.scenario = not args.no_scenario
    if args.ai_off:
        settings.ai.enabled = False
    mode = "Claude (" + settings.ai.model + ")" if os.environ.get("ANTHROPIC_API_KEY") else "mock watch officer (set ANTHROPIC_API_KEY for Claude)"
    print(f"Kestrel COP {__version__} — demo world, seed {settings.demo.seed}, AI: {mode}")
    print(f"  open http://{args.host or settings.web.host}:{args.port or settings.web.port}/")
    _serve(settings, args.host, args.port)


def cmd_serve(args: argparse.Namespace) -> None:
    path = Path(args.config)
    if not path.exists():
        sys.exit(f"config not found: {path} (run `kestrel init-config` to create one)")
    settings = Settings.load(path)
    print(f"Kestrel COP {__version__} — {settings.area.name}, feeds: "
          + ", ".join(n for n, f in (("adsb", settings.feeds.adsb), ("ais", settings.feeds.ais), ("nws", settings.feeds.nws),
                                     ("firms", settings.feeds.firms), ("usgs", settings.feeds.usgs)) if f.enabled))
    print(f"  open http://{args.host or settings.web.host}:{args.port or settings.web.port}/")
    _serve(settings, args.host, args.port)


def cmd_init_config(args: argparse.Namespace) -> None:
    path = Path(args.path)
    if path.exists() and not args.force:
        sys.exit(f"{path} exists; use --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_YAML, encoding="utf-8")
    print(f"wrote {path}")


def _client_get(url: str, path: str, method: str = "GET", body: dict | None = None) -> dict:
    """Tiny HTTP client for the CLI (standard library only, so `kestrel ask` works anywhere)."""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        sys.exit(f"{method} {path} failed: HTTP {exc.code} {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {url}: {exc.reason} (is `kestrel demo` or `kestrel serve` running?)")


def cmd_sitrep(args: argparse.Namespace) -> None:
    data = _client_get(args.url, "/api/ai/sitrep", "POST")
    print(data["text"])
    if args.json:
        print(json.dumps(data, indent=2))


def cmd_ask(args: argparse.Namespace) -> None:
    data = _client_get(args.url, "/api/ai/ask", "POST", {"question": " ".join(args.question)})
    for step in data.get("trace", []):
        print(f"  -> {step['tool']}({json.dumps(step['input'])}) -> {step['result_summary']}")
    print()
    print(data["text"])
    print(f"\n[{data['mode']}; citations verified: {len(data['citations'])}, unverified: {len(data['unverified'])}]")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kestrel", description="Kestrel COP — open-feeds common operational picture with an AI watch officer")
    parser.add_argument("--version", action="version", version=f"kestrel-cop {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("demo", help="run with the synthetic demo world (no keys, no internet needed)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--center", help="lat,lon of the picture centre (default Norfolk, VA)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--tak", help="also publish CoT to this TAK server, e.g. tcp://127.0.0.1:8087")
    p.add_argument("--no-scenario", action="store_true", help="skip the scripted emergency / dark ship / zone entry")
    p.add_argument("--ai-off", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("serve", help="run with live feeds from a config file")
    p.add_argument("-c", "--config", default="config/kestrel.yaml")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("init-config", help="write an example config file")
    p.add_argument("path", nargs="?", default="config/kestrel.yaml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init_config)

    p = sub.add_parser("sitrep", help="ask a running Kestrel for a fresh SITREP")
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sitrep)

    p = sub.add_parser("ask", help="ask the watch officer a question about the picture")
    p.add_argument("question", nargs="+")
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.set_defaults(func=cmd_ask)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
