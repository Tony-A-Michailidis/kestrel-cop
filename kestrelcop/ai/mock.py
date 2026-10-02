"""The keyless watch officer. Same tools, same output shapes, templated prose instead of a model.

It exists so `kestrel demo` works on a laptop with no API key and so tests are deterministic. Every
output is labelled MOCK; nothing here pretends to be Claude.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Alert
from .tools import PictureTools

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(km|nm|nautical|miles?)", re.IGNORECASE)


def _fmt_track(t: dict[str, Any]) -> str:
    bits = [t.get("callsign") or t["uid"]]
    if t.get("function"):
        bits.append(t["function"].replace("_", " "))
    pos = f"{t['lat']:.3f}, {t['lon']:.3f}"
    extra = []
    if t.get("alt_m") is not None and t.get("domain") == "air":
        extra.append(f"{t['alt_m']} m")
    if t.get("speed_kts") is not None:
        extra.append(f"{t['speed_kts']:.0f} kt")
    if t.get("course_deg") is not None:
        extra.append(f"{t['course_deg']:03d}°")
    if t.get("distance_km") is not None:
        extra.append(f"{t['distance_km']} km away")
    if t.get("flags"):
        extra.append("flags " + ",".join(t["flags"]))
    return f"{' '.join(bits)} at {pos}" + (f" ({', '.join(extra)})" if extra else "") + f" [{t['uid']}]"


def mock_sitrep(summary: dict[str, Any], tools: PictureTools) -> str:
    counts = summary["counts"]
    feeds = summary["feeds"]
    air = tools.tool_search_tracks(domain="air", limit=50)["tracks"]
    sea = tools.tool_search_tracks(domain="sea", limit=50)["tracks"]
    events = tools.tool_list_events(limit=50)["events"]
    alerts = tools.tool_list_alerts(limit=15)["alerts"]

    def flagged(rows, flag):
        return [t for t in rows if any(f.startswith(flag) for f in t["flags"])]

    lines = [f"KESTREL SITREP — DTG {summary['dtg']}  [MOCK WATCH OFFICER — set ANTHROPIC_API_KEY for Claude]"]
    lines.append(
        f"1. SITUATION — {summary['area']['name']}: {counts['by_domain'].get('air', 0)} aircraft and "
        f"{counts['by_domain'].get('sea', 0)} vessels on the picture, {counts['events']} environmental events, "
        f"{counts['alerts']} alerts raised. "
        + ("Feeds are synthetic (demo mode). " if any(f['mode'] == 'demo' for f in feeds) else "")
        + f"{counts['flagged']} track(s) carry flags."
    )
    air_bits = []
    listed: set[str] = set()

    def add_air(prefix: str, t: dict, suffix: str = "") -> None:
        if t["uid"] in listed:
            return
        listed.add(t["uid"])
        air_bits.append(f"{prefix}: {_fmt_track(t)}{suffix}")

    for t in flagged(air, "EMERGENCY"):
        add_air("EMERGENCY", t, f" squawking {t.get('squawk', '?')}")
    for t in [t for t in air if any(f.startswith('IN_ZONE') for f in t['flags'])][:3]:
        add_air("Zone entry", t)
    for t in flagged(air, "MILITARY")[:3]:
        add_air("Military/government", t)
    for t in sorted(air, key=lambda t: -(t.get("speed_kts") or 0))[:2]:
        add_air("Fastest", t)
    lines.append("2. AIR — " + (" ".join(air_bits) if air_bits else "insufficient data."))
    sea_bits = []
    for t in flagged(sea, "DARK"):
        sea_bits.append(f"AIS GAP: {_fmt_track(t)} last report {t['age_s']} s ago")
    for t in [t for t in sea if t.get("function") == "law_enforcement"][:2]:
        sea_bits.append(f"Law enforcement: {_fmt_track(t)}")
    anchored = [t for t in sea if (t.get("speed_kts") or 0) < 1]
    moving = [t for t in sea if (t.get("speed_kts") or 0) >= 1]
    sea_bits.append(f"{len(moving)} vessels under way, {len(anchored)} stopped/anchored.")
    for t in moving[:2]:
        sea_bits.append(f"e.g. {_fmt_track(t)}")
    lines.append("3. MARITIME — " + " ".join(sea_bits))
    env_bits = []
    for e in events:
        if e["kind"] == "alert":
            env_bits.append(f"NWS {e['title']} ({e.get('severity')}) [{e['uid']}]")
        elif e["kind"] == "quake":
            env_bits.append(f"M{e.get('magnitude')} earthquake, {e['title']} [{e['uid']}]")
        elif e["kind"] == "fire" and e.get("frp"):
            env_bits.append(f"fire detection FRP {e['frp']} MW at {e['lat']:.3f}, {e['lon']:.3f} [{e['uid']}]")
    fires = [e for e in events if e["kind"] == "fire"]
    if fires:
        env_bits.append(f"{len(fires)} fire detections total.")
    lines.append("4. ENVIRONMENT — " + ("; ".join(env_bits[:8]) if env_bits else "no environmental events on the picture."))
    if alerts:
        lines.append("5. ALERTS — " + " ".join(f"[{a['id']}] {a['severity']} {a['title']} ({a['age_min']} min ago)." for a in alerts[:8]))
    else:
        lines.append("5. ALERTS — none raised.")
    conf = []
    for f in feeds:
        age = f.get("last_message_age_s")
        conf.append(f"{f['name']}: {f['mode']}/{f['status']}" + (f", last message {age} s ago" if age is not None else "")
                    + (f", error: {f['error']}" if f.get('error') else ""))
    lines.append("6. DATA CONFIDENCE — " + "; ".join(conf) + ". Mock officer: template output, no reasoning applied.")
    watch = []
    if flagged(air, "EMERGENCY"):
        watch.append("- Track the emergency aircraft until it is on the ground; confirm destination with the rules engine zone list.")
    if flagged(sea, "DARK"):
        watch.append("- Re-acquire the dark vessel: check for a transponder restart and cross-check neighbouring AIS traffic.")
    in_zone = [t for t in air + sea if any(f.startswith("IN_ZONE") for f in t["flags"])]
    if in_zone:
        watch.append("- " + ", ".join(t.get("callsign") or t["uid"] for t in in_zone[:4]) + " inside a restricted zone: confirm authorisation or coordination.")
    if not watch:
        watch.append("- No flagged tracks; continue routine watch and re-check feed health at the next cycle.")
    watch.append("- Regenerate this SITREP with a live model for assessment rather than enumeration.")
    lines.append("7. WATCH OFFICER ASSESSMENT\n" + "\n".join(watch))
    return "\n".join(lines)


def mock_ask(question: str, tools: PictureTools) -> tuple[str, list[dict[str, Any]]]:
    trace: list[dict[str, Any]] = []

    def call(name: str, **args):
        result = tools.call(name, args)
        trace.append({"tool": name, "input": args, "result_summary": tools.summarize(name, result)})
        return result

    q = question.lower()
    summary = call("picture_summary")
    counts = summary["counts"]
    domain = "air" if any(w in q for w in ("aircraft", "plane", "flight", "air ")) else ("sea" if any(w in q for w in ("ship", "vessel", "boat", "maritime")) else None)

    # a UID or callsign mentioned verbatim? (single tokens, then two-word vessel names)
    ref = None
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", question)
    for token in tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]:
        hit = tools._find(token)
        if hit is not None:
            ref = token
            break

    if any(w in q for w in ("emergenc", "7700", "7500", "7600", "squawk", "mayday")):
        res = call("search_tracks", flag="EMERGENCY", limit=10)
        if res["tracks"]:
            return ("Emergency traffic on the picture: " + "; ".join(_fmt_track(t) + f" squawk {t.get('squawk')}" for t in res["tracks"]) + ".", trace)
        return ("No aircraft is squawking an emergency code right now.", trace)
    if any(w in q for w in ("dark", "ais gap", "stopped transmitting", "went quiet")):
        res = call("search_tracks", flag="DARK", limit=10)
        if res["tracks"]:
            return ("Vessels with an AIS gap: " + "; ".join(_fmt_track(t) + f", last report {t['age_s']} s ago" for t in res["tracks"]) + ".", trace)
        return ("No vessel is currently flagged DARK.", trace)
    if "military" in q or "government" in q:
        res = call("search_tracks", flag="MILITARY", limit=10)
        if res["tracks"]:
            return ("Military/government tracks: " + "; ".join(_fmt_track(t) for t in res["tracks"]) + ".", trace)
        return ("No tracks are classified military/government.", trace)
    if any(w in q for w in ("alert", "warning", "what's wrong", "whats wrong", "problems")) and "weather" not in q:
        res = call("list_alerts", limit=10)
        if res["alerts"]:
            return ("Active alerts, newest first: " + " ".join(f"[{a['id']}] {a['severity']} {a['title']} ({a['age_min']} min ago)." for a in res["alerts"]), trace)
        return ("No alerts have been raised.", trace)
    for kind, words in (("fire", ("fire", "firms", "burn")), ("quake", ("quake", "earthquake", "seismic", "usgs")), ("alert", ("weather", "storm", "advisory", "nws"))):
        if any(w in q for w in words):
            res = call("list_events", kind=kind, limit=10)
            if not res["events"]:
                return (f"No {kind} events on the picture.", trace)
            if kind == "quake":
                return ("Seismic events: " + "; ".join(f"M{e.get('magnitude')} {e['title']}, {e.get('age_min')} min ago [{e['uid']}]" for e in res["events"]) + ".", trace)
            if kind == "alert":
                return ("Weather alerts: " + "; ".join(f"{e['title']} ({e.get('severity')}) — {e.get('headline', '')} [{e['uid']}]" for e in res["events"]) + ".", trace)
            return (f"{res['total']} fire detections. Strongest: " + "; ".join(f"FRP {e.get('frp')} MW at {e['lat']:.3f}, {e['lon']:.3f} [{e['uid']}]" for e in sorted(res["events"], key=lambda e: -(e.get('frp') or 0))[:3]) + ".", trace)
    if ref and any(w in q for w in ("near", "around", "within", "close to", "vicinity")):
        m = _NUM_RE.search(question)
        km = float(m.group(1)) if m else 50.0
        if m and m.group(2).lower().startswith(("nm", "nautical", "mile")):
            km *= 1.852
        res = call("search_tracks", near_uid=ref, near={"lat": 0, "lon": 0, "km": km}, domain=domain, limit=10)
        tr = tools.call("get_track", {"uid": ref}).get("track", {})
        if not res["tracks"]:
            return (f"Nothing within {km:.0f} km of {tr.get('callsign', ref)} [{tr.get('uid', ref)}].", trace)
        return (f"Within {km:.0f} km of {tr.get('callsign', ref)} [{tr.get('uid', ref)}]: " + "; ".join(_fmt_track(t) for t in res["tracks"]) + ".", trace)
    if ref:
        res = call("get_track", uid=ref)
        t = res.get("track")
        if t:
            return (f"{_fmt_track(t)}. Source {t['source']}, affiliation {t['affiliation']}, last report {t['age_s']} s ago, "
                    f"{t['trail_points']} trail points. Attributes: "
                    + ", ".join(f"{k}={v}" for k, v in t["attrs"].items() if k not in ("hex",)) + ".", trace)
    if any(w in q for w in ("how many", "count", "number of")):
        if domain:
            return (f"{counts['by_domain'].get(domain, 0)} {'aircraft' if domain == 'air' else 'vessels'} are on the picture "
                    f"({counts['flagged']} tracks flagged overall).", trace)
        return (f"{counts['by_domain'].get('air', 0)} aircraft, {counts['by_domain'].get('sea', 0)} vessels, "
                f"{counts['events']} events and {counts['alerts']} alerts.", trace)
    if any(w in q for w in ("fastest", "highest", "lowest", "slowest")):
        res = call("search_tracks", domain=domain or "air", limit=50)
        rows = res["tracks"]
        if not rows:
            return ("No tracks match.", trace)
        key = "alt_m" if any(w in q for w in ("highest", "lowest")) else "speed_kts"
        rev = not any(w in q for w in ("lowest", "slowest"))
        best = sorted(rows, key=lambda t: (t.get(key) or 0), reverse=rev)[0]
        return (f"{_fmt_track(best)}.", trace)
    res = call("list_alerts", limit=5)
    head = (f"The picture holds {counts['by_domain'].get('air', 0)} aircraft, {counts['by_domain'].get('sea', 0)} vessels and "
            f"{counts['events']} events. ")
    if res["alerts"]:
        head += "Latest alerts: " + " ".join(f"[{a['id']}] {a['severity']} {a['title']}." for a in res["alerts"][:3])
    head += " (Mock officer: ask about emergencies, dark vessels, military tracks, alerts, fires, quakes, weather, a callsign, or 'what is near <callsign> within 20 km'.)"
    return (head, trace)


def mock_triage(alert: Alert, context: dict[str, Any]) -> str:
    subject = context.get("subject") or {}
    nearby = context.get("nearby") or []
    where = f"{alert.lat:.3f}, {alert.lon:.3f}" if alert.lat is not None else "unknown"
    who = _fmt_track(subject) if subject.get("uid") else (alert.subject_uid or "unknown")
    ctx = "; ".join(_fmt_track(t) for t in nearby[:4]) if nearby else "no tracks within 30 km"
    return "\n".join([
        f"SPOTREP {alert.id}  [MOCK]",
        f"1. WHAT: {alert.title} — rule {alert.rule}.",
        f"2. WHERE: {where}.",
        f"3. WHEN: {alert.ts.strftime('%d%H%MZ %b %y').upper()}.",
        f"4. WHO/EQUIPMENT: {who}.",
        f"5. CONTEXT: {ctx}.",
        f"6. ASSESSMENT: severity {alert.severity}; automated rule, no human review yet.",
        "7. RECOMMENDED: acknowledge on the watch floor; monitor the subject track; escalate per SOP if the condition persists.",
    ])
