#!/usr/bin/env python3
"""Scrape Sinclair free-streaming channels and build sinclair.m3u + sinclair.xml.gz.

Strategy per channel:
  1. Plain HTTP: fetch the page (plus its iframes/scripts) and regex for .m3u8 URLs.
  2. Fallback: headless Chromium (Playwright) and sniff network requests for .m3u8.
  3. Last resort: reuse the URL from the previous sinclair.m3u so a bad run
     never wipes a working playlist.
"""
from __future__ import annotations

import gzip
import html
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import requests

GROUP = "EN - Entertainment"
M3U_FILE = Path("sinclair.m3u")
XML_FILE = Path("sinclair.xml.gz")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

CHANNELS = [
    {"id": "CometTV.us", "name": "Comet TV", "url": "https://comettv.com/watch-live/"},
    {"id": "Charge.us", "name": "Charge!", "url": "https://watchcharge.com/watch-live/"},
    {"id": "Roar.us", "name": "Roar", "url": "https://watchroar.com/watch-live/"},
    {"id": "TheNest.us", "name": "The Nest", "url": "https://watchthenest.com/watch-live/"},
    {"id": "TennisChannel.us", "name": "Tennis Channel", "url": "https://www.tennischannel.com/live"},
]

M3U8_RE = re.compile(r"https?:[^\s\"'<>\\)]+?\.m3u8[^\s\"'<>\\)]*", re.I)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def log(msg: str) -> None:
    print(msg, flush=True)


def clean(text: str) -> str:
    return text.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")


def find_m3u8(text: str) -> list[str]:
    seen, out = set(), []
    for m in M3U8_RE.findall(clean(text)):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def pick_best(urls: list[str]) -> str | None:
    """Prefer master/manifest playlists over single-rendition or chunk lists."""
    if not urls:
        return None
    bad = ("chunk", "segment", "/audio", "thumbnail")
    good = [u for u in urls if not any(b in u.lower() for b in bad)] or urls
    for u in good:
        if any(k in u.lower() for k in ("master", "playlist.m3u8", "manifest")):
            return u
    return good[0]


def meta(page: str, prop: str) -> str:
    for pat in (
        rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']*)',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{prop}',
    ):
        m = re.search(pat, page, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def get(url: str) -> str:
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        return r.text
    except requests.RequestException as exc:
        log(f"    ! fetch failed {url}: {exc}")
        return ""


def scrape_static(url: str) -> tuple[str | None, str]:
    page = get(url)
    if not page:
        return None, ""
    stream = pick_best(find_m3u8(page))
    if stream:
        return stream, page
    # Follow iframes and same-page scripts/JSON a few levels shallow.
    extras = re.findall(r'<(?:iframe|script)[^>]+src=["\']([^"\']+)', page, re.I)
    extras += re.findall(r'["\']((?:https?:)?//[^"\']+?\.json[^"\']*)["\']', page, re.I)
    for ref in list(dict.fromkeys(extras))[:12]:
        full = urljoin(url, ref)
        stream = pick_best(find_m3u8(get(full)))
        if stream:
            return stream, page
    return None, page


def scrape_browser(url: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("    (playwright not installed, skipping browser fallback)")
        return None
    found: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(user_agent=UA, locale="en-US")
            page = ctx.new_page()
            page.on("request", lambda r: found.append(r.url) if ".m3u8" in r.url else None)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            for _ in range(20):  # up to ~20s for the player to request a manifest
                if found:
                    break
                page.wait_for_timeout(1000)
            if not found:
                found.extend(find_m3u8(page.content()))
            browser.close()
    except Exception as exc:  # noqa: BLE001
        log(f"    ! browser fallback failed: {exc}")
    return pick_best(found)


def load_previous() -> dict[str, str]:
    prev: dict[str, str] = {}
    if not M3U_FILE.exists():
        return prev
    lines = M3U_FILE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines[:-1]):
        m = re.search(r'tvg-id="([^"]+)"', line)
        if line.startswith("#EXTINF") and m and lines[i + 1].startswith("http"):
            prev[m.group(1)] = lines[i + 1].strip()
    return prev


def write_m3u(entries: list[dict]) -> None:
    out = ['#EXTM3U x-tvg-url="sinclair.xml.gz"']
    for e in entries:
        out.append(
            f'#EXTINF:-1 tvg-id="{e["id"]}" tvg-name="{e["name"]}" '
            f'tvg-logo="{e["logo"]}" group-title="{GROUP}",{e["name"]}'
        )
        out.append(e["stream"])
    M3U_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def write_xmltv(entries: list[dict]) -> None:
    """XMLTV with channel metadata and 6-hour placeholder programmes (48h ahead)."""
    fmt = "%Y%m%d%H%M%S +0000"
    now = datetime.now(timezone.utc)
    start = now.replace(hour=now.hour - now.hour % 6, minute=0, second=0, microsecond=0)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<tv generator-info-name="sinclair-scraper">']
    for e in entries:
        parts.append(f'<channel id="{escape(e["id"])}"><display-name>{escape(e["name"])}</display-name>')
        if e["logo"]:
            parts.append(f'<icon src="{escape(e["logo"])}"/>')
        parts.append(f'<url>{escape(e["page"])}</url></channel>')
    for e in entries:
        for i in range(8):
            s = start + timedelta(hours=6 * i)
            t = s + timedelta(hours=6)
            parts.append(
                f'<programme start="{s.strftime(fmt)}" stop="{t.strftime(fmt)}" channel="{escape(e["id"])}">'
                f'<title lang="en">{escape(e["name"])}</title>'
                f'<desc lang="en">{escape(e["desc"] or e["name"] + " live stream")}</desc>'
                f'<category lang="en">Entertainment</category></programme>'
            )
    parts.append("</tv>")
    with gzip.open(XML_FILE, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


def main() -> int:
    previous = load_previous()
    entries: list[dict] = []
    for ch in CHANNELS:
        log(f"==> {ch['name']} ({ch['url']})")
        stream, page = scrape_static(ch["url"])
        if not stream:
            log("    no stream in static HTML, trying browser…")
            stream = scrape_browser(ch["url"])
        if not stream and ch["id"] in previous:
            stream = previous[ch["id"]]
            log("    using previous URL")
        if not stream:
            log("    ✗ no stream found, skipping")
            continue
        log(f"    ✓ {stream}")
        entries.append({
            **ch,
            "page": ch["url"],
            "stream": stream,
            "logo": meta(page, "og:image"),
            "desc": meta(page, "og:description") or meta(page, "description"),
        })
    if not entries:
        log("No channels found; leaving existing files untouched.")
        return 1
    write_m3u(entries)
    write_xmltv(entries)
    log(f"Wrote {M3U_FILE} and {XML_FILE} ({len(entries)}/{len(CHANNELS)} channels)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
