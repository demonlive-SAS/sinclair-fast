#!/usr/bin/env python3
"""Scrape https://fast-channels.sinclairstoryline.com/ and build:
  - sinclair.m3u       (all channels in group "EN - Entertainment")
  - sinclair.xml.gz    (XMLTV guide)

The scraper is defensive: it looks for channel data in embedded JSON
(__NEXT_DATA__, ld+json, inline JSON), in JSON/API URLs referenced by the page
and its scripts, and finally falls back to raw .m3u8 links. Run with
DEBUG=1 to see what was found.
"""
import gzip
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urljoin
from xml.sax.saxutils import quoteattr

import requests

BASE_URL = os.getenv("SINCLAIR_URL", "https://fast-channels.sinclairstoryline.com/")
EXTRA_API_URLS = [u.strip() for u in os.getenv("SINCLAIR_API_URLS", "").split(",") if u.strip()]
GROUP = "EN - Entertainment"
M3U_FILE = "sinclair.m3u"
XML_FILE = "sinclair.xml.gz"
EPG_URL = os.getenv("EPG_URL", "")  # optional raw URL to sinclair.xml.gz for url-tvg
PLACEHOLDER_HOURS = 48  # used only when no schedule data is found

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
}
M3U8_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.m3u8[^\s\"'<>\\]*", re.I)
NAME_KEYS = ("name", "title", "channelName", "channel_name", "displayName")
URL_KEYS = ("streamUrl", "stream_url", "url", "hls", "hlsUrl", "src", "source", "playbackUrl", "manifest")
LOGO_KEYS = ("logo", "logoUrl", "logo_url", "image", "imageUrl", "thumbnail", "icon")
SCHEDULE_KEYS = ("programs", "programmes", "schedule", "listings", "epg", "airings", "events")

logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG") else logging.INFO,
    format="%(levelname)s %(message)s",
)
log = logging.getLogger("sinclair")
session = requests.Session()
session.headers.update(HEADERS)


def get(url, **kw):
    try:
        r = session.get(url, timeout=30, **kw)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        log.debug("GET failed %s: %s", url, e)
        return None


def slugify(text):
    return re.sub(r"[^a-z0-9]+", ".", str(text).lower()).strip(".") or "channel"


def parse_time(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            v = float(value)
            if v > 1e11:  # milliseconds
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def first(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def extract_programs(node):
    """Find a schedule list inside a channel dict."""
    for key in SCHEDULE_KEYS:
        items = node.get(key)
        if not isinstance(items, list):
            continue
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            start = parse_time(it.get("start") or it.get("startTime") or it.get("start_time") or it.get("begin"))
            end = parse_time(it.get("end") or it.get("endTime") or it.get("end_time") or it.get("stop"))
            title = first(it, ("title", "name", "programTitle", "show"))
            if start and end and title:
                out.append({
                    "start": start,
                    "end": end,
                    "title": title,
                    "desc": first(it, ("description", "desc", "synopsis", "summary")) or "",
                })
        if out:
            return out
    return []


def walk(node, found):
    """Recursively collect channel-like dicts (name + stream URL)."""
    if isinstance(node, dict):
        name = first(node, NAME_KEYS)
        url = None
        for k in URL_KEYS:
            v = node.get(k)
            if isinstance(v, str) and v.startswith("http") and ".m3u8" in v:
                url = v
                break
        if name and url:
            found.append({
                "name": name,
                "url": url,
                "logo": first(node, LOGO_KEYS) or "",
                "id": str(node.get("id") or node.get("slug") or slugify(name)),
                "programs": extract_programs(node),
            })
        for v in node.values():
            walk(v, found)
    elif isinstance(node, list):
        for v in node:
            walk(v, found)


def json_blobs(html):
    """Yield JSON documents embedded in an HTML page."""
    for m in re.finditer(
        r"<script[^>]*(?:type=\"application/(?:ld\+)?json\"|id=\"__NEXT_DATA__\")[^>]*>(.*?)</script>",
        html, re.S | re.I,
    ):
        try:
            yield json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    for m in re.finditer(r"(?:window\.[\w$]+|__[\w]+__)\s*=\s*(\{.*?\});?\s*</script>", html, re.S):
        try:
            yield json.loads(m.group(1))
        except json.JSONDecodeError:
            continue


def candidate_api_urls(html, base):
    urls = set(EXTRA_API_URLS)
    for m in re.finditer(r"https?://[^\s\"'<>\\]+", html):
        u = m.group(0).rstrip(",;)")
        if re.search(r"(channel|schedule|epg|lineup|api|\.json)", u, re.I) and not re.search(
            r"\.(js|css|png|jpe?g|svg|woff2?|ico|m3u8)(\?|$)", u, re.I
        ):
            urls.add(u)
    for m in re.finditer(r"[\"'](/(?:api|_next/data)/[^\"']+)[\"']", html):
        urls.add(urljoin(base, m.group(1)))
    return list(urls)[:25]


def script_urls(html, base):
    return [urljoin(base, m) for m in re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", html, re.I)][:20]


def scrape():
    page = get(BASE_URL)
    if page is None:
        log.error("Could not load %s (blocked or down?)", BASE_URL)
        sys.exit(1)
    html = page.text
    found = []

    for blob in json_blobs(html):
        walk(blob, found)
    log.debug("Embedded JSON channels: %d", len(found))

    if not found:
        texts = [html]
        for s in script_urls(html, BASE_URL):
            r = get(s)
            if r is not None:
                texts.append(r.text)
        for t in texts:
            for u in candidate_api_urls(t, BASE_URL):
                r = get(u)
                if r is None:
                    continue
                try:
                    walk(r.json(), found)
                except ValueError:
                    continue
            if found:
                break
        log.debug("API channels: %d", len(found))

    if not found:  # last resort: raw m3u8 links
        for t in [html]:
            for u in sorted(set(M3U8_RE.findall(t))):
                found.append({
                    "name": slugify(u.split("/")[-2] if "/" in u else u).replace(".", " ").title(),
                    "url": u, "logo": "", "id": slugify(u), "programs": [],
                })
        log.debug("Regex channels: %d", len(found))

    seen, channels = set(), []
    for ch in found:
        if ch["url"] in seen:
            continue
        seen.add(ch["url"])
        channels.append(ch)
    channels.sort(key=lambda c: c["name"].lower())
    return channels


def write_m3u(channels):
    header = "#EXTM3U"
    if EPG_URL:
        header += f' url-tvg="{EPG_URL}"'
    lines = [header]
    for ch in channels:
        name = ch["name"].replace('"', "'")
        lines.append(
            f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-name="{name}" '
            f'tvg-logo="{ch["logo"]}" group-title="{GROUP}",{name}'
        )
        lines.append(ch["url"])
    with open(M3U_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def xmltv_time(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def write_xml(channels):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="sinclair-scraper">']
    for ch in channels:
        out.append(f"  <channel id={quoteattr(ch['id'])}>")
        out.append(f"    <display-name>{escape(ch['name'])}</display-name>")
        if ch["logo"]:
            out.append(f"    <icon src={quoteattr(ch['logo'])} />")
        out.append("  </channel>")
    for ch in channels:
        programs = ch["programs"]
        if not programs:  # placeholder blocks so players still show something
            programs = [
                {"start": now + timedelta(hours=h), "end": now + timedelta(hours=h + 1),
                 "title": ch["name"], "desc": ""}
                for h in range(PLACEHOLDER_HOURS)
            ]
        for p in programs:
            out.append(
                f'  <programme start="{xmltv_time(p["start"])}" stop="{xmltv_time(p["end"])}" '
                f'channel={quoteattr(ch["id"])}>'
            )
            out.append(f"    <title>{escape(p['title'])}</title>")
            if p["desc"]:
                out.append(f"    <desc>{escape(p['desc'])}</desc>")
            out.append("  </programme>")
    out.append("</tv>")
    with gzip.open(XML_FILE, "wt", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def main():
    channels = scrape()
    if not channels:
        log.error("No channels found. Re-run with DEBUG=1 and check the site's network calls "
                  "(set SINCLAIR_API_URLS to the JSON endpoint you find).")
        sys.exit(1)
    write_m3u(channels)
    write_xml(channels)
    log.info("Wrote %d channels to %s and %s", len(channels), M3U_FILE, XML_FILE)


if __name__ == "__main__":
    main()
