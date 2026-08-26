#!/usr/bin/env python3
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
URLS_FILE = DATA / "approved_urls.txt"
DISCOVERY_STATE_FILE = DATA / "discovery_state.json"
MIN_SECONDS = 35.0
MAX_SECONDS = 60.0

SEARCH_QUERIES = [
    "Hindi funny comedy shorts",
    "Hindi comedy funny short video",
    "Desi Hindi comedy shorts funny",
]

FUNNY_KEYWORDS = (
    "funny", "comedy", "comedian", "joke", "jokes", "prank", "lol", "laugh",
    "funniest", "meme", "roast", "humor", "humour", "desi comedy", "comedy shorts"
)
HINDI_KEYWORDS = (
    "hindi", "hindicomedy", "desi", "indian", "india", "hindustani", "bollywood",
    "bhai", "bhaiya", "yaar", "dost", "mummy", "papa", "shaadi", "biwi", "pati",
    "patni", "saas", "bahu", "ladka", "ladki"
)
DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

DATA.mkdir(parents=True, exist_ok=True)


def ytdlp():
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp is not installed.")
    return exe


def run_json(args, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "yt-dlp failed")
    return json.loads(result.stdout)


def load_discovery_state():
    if not DISCOVERY_STATE_FILE.exists():
        return {"seen": [], "last_selected": None}
    try:
        state = json.loads(DISCOVERY_STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("seen", [])
        return state
    except Exception:
        return {"seen": [], "last_selected": None}


def save_discovery_state(state):
    DISCOVERY_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def ensure_queue_file():
    if not URLS_FILE.exists():
        URLS_FILE.write_text(
            "# AutoPilot queue. Only owned, licensed, permissioned, or Creative Commons sources.\n",
            encoding="utf-8",
        )


def text_blob(info):
    parts = []
    for key in ("title", "description", "tags", "categories"):
        value = info.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts)


def looks_hindi_funny(info):
    text = text_blob(info)
    lower = text.lower()
    funny = any(word in lower for word in FUNNY_KEYWORDS)
    hindi = bool(DEVANAGARI_RE.search(text)) or any(word in lower for word in HINDI_KEYWORDS)
    return funny and hindi


def duration_allowed(info):
    try:
        duration = float(info.get("duration") or 0)
    except Exception:
        return False
    return MIN_SECONDS <= duration <= MAX_SECONDS


def creative_commons_allowed(info):
    license_text = str(info.get("license") or "").strip()
    lower = license_text.lower()
    return bool(license_text) and "creative commons" in lower


def search_entries(query, max_items=25):
    data = run_json([
        ytdlp(),
        "--flat-playlist",
        "--playlist-end", str(max_items),
        "--dump-single-json",
        f"ytsearch{max_items}:{query}",
    ])
    entries = data.get("entries") or []
    return [item for item in entries if isinstance(item, dict)]


def entry_url(item):
    for key in ("webpage_url", "original_url"):
        value = str(item.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    value = str(item.get("url") or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    video_id = str(item.get("id") or value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        return f"https://www.youtube.com/watch?v={video_id}"
    return None


def full_info(url):
    return run_json([
        ytdlp(), "--skip-download", "--no-playlist", "--dump-single-json", url
    ])


def candidate_from_url(url):
    info = full_info(url)
    if not creative_commons_allowed(info):
        license_text = str(info.get("license") or "not marked Creative Commons")
        print(f"SKIP rights: {license_text}: {url}")
        return None
    if not duration_allowed(info):
        print(f"SKIP duration {info.get('duration')}s; need 35-60s: {url}")
        return None
    if not looks_hindi_funny(info):
        print(f"SKIP non-Hindi/non-funny: {url}")
        return None

    creator = str(
        info.get("uploader_id")
        or info.get("channel")
        or info.get("uploader")
        or "YouTube creator"
    ).strip().lstrip("@")
    title = str(info.get("title") or "Hindi funny video").strip()[:100]
    license_text = str(info.get("license") or "Creative Commons").strip()
    try:
        views = int(info.get("view_count") or 0)
    except Exception:
        views = 0

    attribution = f"{creator} | {title} | {license_text} | Source: {url}"
    return {
        "url": url,
        "permalink": url,
        "seen_key": url,
        "title": title,
        "duration": float(info.get("duration") or 0),
        "view_count": views,
        "timestamp": str(info.get("timestamp") or info.get("upload_date") or ""),
        "source_account": attribution,
        "license": license_text,
        "discovery_method": "youtube_creative_commons_search",
    }


def discover_one(max_items=25):
    state = load_discovery_state()
    seen = set(state.get("seen", []))
    candidates = []

    for query in SEARCH_QUERIES:
        print(f"Searching YouTube Creative Commons candidates: {query}")
        try:
            entries = search_entries(query, max_items=max_items)
        except Exception as exc:
            print(f"YouTube search unavailable for query: {exc}")
            continue

        for item in entries:
            url = entry_url(item)
            if not url or url in seen:
                continue
            try:
                candidate = candidate_from_url(url)
            except Exception as exc:
                print(f"SKIP YouTube candidate: {url} ({exc})")
                continue
            seen.add(url)
            if candidate:
                candidates.append(candidate)

    state["seen"] = sorted(seen)
    save_discovery_state(state)
    if not candidates:
        print("No new licensed Creative Commons Hindi funny 35-60s YouTube candidate found.")
        return None

    candidates.sort(
        key=lambda item: (item.get("view_count", 0), item.get("timestamp", "")),
        reverse=True,
    )
    selected = candidates[0]
    url = selected["url"]

    ensure_queue_file()
    existing = {
        line.strip()
        for line in URLS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    if url not in existing:
        current = URLS_FILE.read_text(encoding="utf-8")
        with URLS_FILE.open("a", encoding="utf-8") as handle:
            if current and not current.endswith("\n"):
                handle.write("\n")
            handle.write(url + "\n")

    state = load_discovery_state()
    seen = set(state.get("seen", []))
    seen.add(url)
    state["seen"] = sorted(seen)
    state["last_selected"] = selected
    save_discovery_state(state)

    print(f"YOUTUBE CC SELECTED: {selected['title']}")
    print(f"Duration: {selected['duration']:.1f}s")
    print(f"License: {selected['license']}")
    print(f"Queued: {url}")
    return url


if __name__ == "__main__":
    discover_one()
