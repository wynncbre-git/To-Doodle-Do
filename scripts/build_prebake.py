#!/usr/bin/env python3
"""
Build prebake.json for the To Doodle Do dashboard.

Fetches five public feeds and merges them into a single JSON file
that the Cowork artifact's scheduled task syndicates into the dashboard.

Designed to fail gracefully — if any one feed fails, the others still
publish, and the failed feed is omitted (the scheduled task on the
Cowork side will keep the previous block in the artifact).

Run from a GitHub Actions workflow on cron 0 21,7 * * * UTC (06:00 + 16:00 JST).
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
from typing import Any, Optional


JMA_AREAS = {
    "saitama": {"forecast_url": "https://www.jma.go.jp/bosai/forecast/data/forecast/110000.json", "area": "110020", "city": "43056"},
    "tokyo":   {"forecast_url": "https://www.jma.go.jp/bosai/forecast/data/forecast/130000.json", "area": "130010", "city": "44132"},
}

BBC_URL = "https://feeds.bbci.co.uk/news/world/rss.xml"

GITHUB_TOPICS = [
    ("claude-code",   "https://api.github.com/search/repositories?q=topic:claude-code&sort=updated&order=desc&per_page=5"),
    ("vscode",        "https://api.github.com/search/repositories?q=topic:vscode&sort=stars&order=desc&per_page=5"),
    ("design-system", "https://api.github.com/search/repositories?q=topic:design-system&sort=stars&order=desc&per_page=5"),
]

FX_URL = "https://api.frankfurter.dev/v1/latest?from=JPY&to=USD,EUR,GBP"

JR_URL = "https://traininfo.jreast.co.jp/delay_data/data/delay.json"


def http_get(url: str, timeout: int = 20, headers: Optional[dict] = None) -> bytes:
    """GET helper. Adds a User-Agent because some endpoints reject default Python UA."""
    req_headers = {"User-Agent": "todoodledo-prebake/1.0 (+https://github.com/vector-vibe-code/todoodledo)"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_jma() -> Optional[dict]:
    """Fetch JMA forecasts for Saitama + Tokyo and parse into the artifact's expected shape."""
    cities = {}
    for key, cfg in JMA_AREAS.items():
        try:
            raw = http_get(cfg["forecast_url"])
            arr = json.loads(raw)
            parsed = parse_jma_payload(arr, cfg["area"], cfg["city"])
            if parsed:
                cities[key] = parsed
        except Exception as e:
            print(f"[jma:{key}] FAIL — {e}", file=sys.stderr)
    if not cities:
        return None
    return {"cities": cities, "fetchedAt": int(time.time() * 1000)}


def parse_jma_payload(arr: list, area_code: str, city_code: str) -> Optional[dict]:
    """Mirror the parsing logic from the original scheduled task SKILL.md."""
    if not arr or not isinstance(arr, list):
        return None
    short_term = arr[0] if len(arr) >= 1 else None
    if not short_term or "timeSeries" not in short_term:
        return None

    ts = short_term["timeSeries"]
    wx = ts[0] if len(ts) >= 1 else None
    pop = ts[1] if len(ts) >= 2 else None
    temps = ts[2] if len(ts) >= 3 else None

    wx_area = next((a for a in (wx["areas"] if wx else []) if a.get("area", {}).get("code") == area_code), None)
    if not wx_area:
        return None
    pop_area = next((a for a in (pop["areas"] if pop else []) if a.get("area", {}).get("code") == area_code), None)
    temp_area = next((a for a in (temps["areas"] if temps else []) if a.get("area", {}).get("code") == city_code), None)

    pops_list = pop_area.get("pops", []) if pop_area else []
    pop_defines = pop.get("timeDefines", []) if pop else []
    temps_list = temp_area.get("temps", []) if temp_area else []

    # Top-level: first non-empty PoP
    top_pop = next((p for p in pops_list if p), "")

    # Find next morning (06:00) and evening (12:00) windows.
    # The pop blocks come in 6h windows; window-start hour is in timeDefines.
    morning_pop, morning_code = "", ""
    evening_pop, evening_code = "", ""

    weather_codes = wx_area.get("weatherCodes", []) or []
    today_code = weather_codes[0] if len(weather_codes) >= 1 else ""
    tomorrow_code = weather_codes[1] if len(weather_codes) >= 2 else ""

    now_ms = int(time.time() * 1000)
    for i, td in enumerate(pop_defines):
        try:
            # timeDefines come ISO-8601 with offset, e.g. "2026-05-07T06:00:00+09:00"
            # We can simplify: parse hour from the string directly
            hour_match = re.search(r"T(\d{2}):", td)
            if not hour_match:
                continue
            hour = int(hour_match.group(1))
            # Determine if this slot is today or tomorrow by date
            day_match = re.search(r"(\d{4}-\d{2}-\d{2})T", td)
            slot_day = day_match.group(1) if day_match else ""
            today_str = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000 + 9 * 3600))  # JST date
            day_code = today_code if slot_day == today_str else tomorrow_code

            if hour == 6 and not morning_pop:
                morning_pop = pops_list[i] if i < len(pops_list) else ""
                morning_code = day_code
            elif hour == 12 and not evening_pop:
                evening_pop = pops_list[i] if i < len(pops_list) else ""
                evening_code = day_code
        except Exception:
            continue

    weathers = wx_area.get("weathers", []) or []
    today_text = weathers[0] if len(weathers) >= 1 else ""
    tomorrow_text = weathers[1] if len(weathers) >= 2 else ""

    return {
        "code": today_code,
        "text": today_text,
        "temp": (temps_list[0] if len(temps_list) >= 1 else ""),
        "pop": top_pop,
        "publishedAt": short_term.get("reportDatetime", ""),
        "morning": {"pop": morning_pop, "code": morning_code},
        "evening": {"pop": evening_pop, "code": evening_code},
        "tomorrow": {
            "code": tomorrow_code,
            "text": tomorrow_text,
            "high": (temps_list[3] if len(temps_list) >= 4 else ""),
            "low":  (temps_list[2] if len(temps_list) >= 3 else ""),
        },
    }


def fetch_bbc() -> Optional[dict]:
    """Fetch BBC World News RSS and extract up to 12 items."""
    try:
        raw = http_get(BBC_URL).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[bbc] FAIL — {e}", file=sys.stderr)
        return None

    items = []
    for m in re.finditer(r"<item>([\s\S]*?)</item>", raw):
        block = m.group(1)
        def grab(tag: str) -> str:
            mm = re.search(rf"<{tag}>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</{tag}>", block)
            return (mm.group(1).strip() if mm else "")
        items.append({
            "title": grab("title"),
            "link": grab("link"),
            "pubDate": grab("pubDate"),
            "description": grab("description"),
        })
        if len(items) >= 12:
            break

    if not items:
        return None
    return {"items": items, "fetchedAt": int(time.time() * 1000)}


def fetch_github() -> Optional[dict]:
    """Fetch GitHub trending repos for the three configured topics."""
    by_topic = {}
    keep_fields = ("name", "html_url", "description", "language", "stargazers_count", "pushed_at")
    for key, url in GITHUB_TOPICS:
        try:
            raw = http_get(url, headers={"Accept": "application/vnd.github+json"})
            data = json.loads(raw)
            items = data.get("items", []) if isinstance(data, dict) else []
            slim = []
            for it in items:
                obj = {k: it.get(k) for k in keep_fields}
                owner = it.get("owner") or {}
                obj["owner"] = {"login": owner.get("login", "")}
                slim.append(obj)
            by_topic[key] = slim
        except Exception as e:
            print(f"[github:{key}] FAIL — {e}", file=sys.stderr)
        time.sleep(1.5)  # gentle pacing for GitHub's unauthenticated rate limit (10 req/min)
    if not by_topic:
        return None
    return {"byTopic": by_topic, "fetchedAt": int(time.time() * 1000)}


def fetch_fx() -> Optional[dict]:
    """Fetch JPY FX rates."""
    try:
        raw = http_get(FX_URL)
        data = json.loads(raw)
    except Exception as e:
        print(f"[fx] FAIL — {e}", file=sys.stderr)
        return None
    rates = data.get("rates", {}) if isinstance(data, dict) else {}
    if not rates:
        return None
    return {
        "rates": {
            "USD": rates.get("USD"),
            "EUR": rates.get("EUR"),
            "GBP": rates.get("GBP"),
        },
        "date": data.get("date", ""),
        "fetchedAt": int(time.time() * 1000),
    }


def fetch_jr() -> dict:
    """Fetch JR East delay feed. Always returns a JR object — null raw if fetch fails."""
    try:
        raw = http_get(JR_URL)
        data = json.loads(raw)
        return {"raw": data, "fetchedAt": int(time.time() * 1000)}
    except Exception as e:
        print(f"[jr] FAIL — {e}", file=sys.stderr)
        # Per the original SKILL.md: on JR failure, write raw=null with a fresh timestamp
        # so the artifact's renderer can show its cached state without a stale "checked" age.
        return {"raw": None, "fetchedAt": int(time.time() * 1000)}


def main() -> int:
    start = time.time()
    out: dict[str, Any] = {
        "schema": 1,
        "source": "https://github.com/vector-vibe-code/todoodledo",
        "builtAt": int(start * 1000),
        "builtAtIso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
    }

    # Run each fetch independently so one failure doesn't kill the whole run
    out["jma"]    = fetch_jma()
    out["bbc"]    = fetch_bbc()
    out["github"] = fetch_github()
    out["fx"]     = fetch_fx()
    out["jr"]     = fetch_jr()

    # Summarise success/failure for the workflow log
    feeds_ok = [k for k in ("jma", "bbc", "github", "fx", "jr") if out.get(k)]
    feeds_failed = [k for k in ("jma", "bbc", "github", "fx", "jr") if not out.get(k)]
    print(f"[summary] OK: {feeds_ok}  FAILED: {feeds_failed}", file=sys.stderr)

    # Always write the file — even partial data is useful
    with open("prebake.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    # Exit non-zero only if EVERY feed failed (catastrophic)
    if not feeds_ok:
        print("[fatal] All feeds failed — aborting commit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
