#!/usr/bin/env python3
import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FEEDS_FILE = DATA / "source_feeds.txt"
URLS_FILE = DATA / "approved_urls.txt"
STATE_FILE = DATA / "discovery_state.json"
MIN_SECONDS = 4.0
KEYWORDS = (
    "funny", "comedy", "fail", "fails", "prank", "lol", "laugh",
    "unexpected", "crazy", "funniest", "try not to laugh", "viral",
    "dog", "cat", "baby", "reaction", "oops", "meme", "memes"
)

DATA.mkdir(parents=True, exist_ok=True)


def load_state():
    if not STATE_FILE.exists():
        return {"seen": [], "last_selected": None}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("seen", [])
        return data
    except Exception:
        return {"seen": [], "last_selected": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def feeds():
    if not FEEDS_FILE.exists():
        FEEDS_FILE.write_text(
            "# Put one approved source account/feed/collection URL per line.\n"
            "# Only list sources whose videos you own or have permission/license to reuse.\n"
            "# AutoPilot discovers only from these approved sources.\n",
            encoding="utf-8",
        )
        return []
    out = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def ensure_queue_file():
    if not URLS_FILE.exists():
        URLS_FILE.write_text(
            "# AutoPilot queue. One approved source URL per line.\n",
            encoding="utf-8",
        )


def canonical_url(url):
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return url
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "instagram.com" in host:
        match = re.search(r"/(reel|reels|p)/([^/?#]+)", parsed.path, re.I)
        if match:
            kind = "reel" if match.group(1).lower() in ("reel", "reels") else "p"
            return f"https://www.instagram.com/{kind}/{match.group(2)}/"
    return url


def ytdlp():
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp is not installed. Run: python3 -m pip install -r requirements.txt")
    return exe


def run_json(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip() or "yt-dlp failed")
    return json.loads(p.stdout)


def is_direct_post(url):
    return bool(re.search(r"instagram\.com/(?:reel|reels|p)/[^/?#]+", url, re.I))


def normalize_url(item):
    for key in ("webpage_url", "original_url", "url"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return canonical_url(value)
    return None


def collect_entries(feed_url, max_items=20):
    # A direct Reel/Post URL can itself be an approved source entry.
    if is_direct_post(feed_url):
        return [{"webpage_url": canonical_url(feed_url)}]

    data = run_json([
        ytdlp(),
        "--flat-playlist",
        "--playlist-end", str(max_items),
        "--dump-single-json",
        feed_url,
    ])
    entries = data.get("entries")
    if isinstance(entries, list):
        return [x for x in entries if isinstance(x, dict)]
    return [data]


def full_info(url):
    return run_json([
        ytdlp(),
        "--skip-download",
        "--no-playlist",
        "--dump-single-json",
        url,
    ])


def safe_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def score(info):
    text = " ".join(str(info.get(k) or "") for k in ("title", "description", "tags")).lower()
    points = sum(2 for word in KEYWORDS if word in text)
    try:
        duration = float(info.get("duration") or 0)
    except Exception:
        duration = 0
    if 4 <= duration <= 90:
        points += 5
    elif duration > 90:
        points += 2
    views = safe_int(info.get("view_count"))
    if views >= 1000000:
        points += 4
    elif views >= 100000:
        points += 2
    elif views >= 10000:
        points += 1
    return points


def discover_candidates(max_items=20):
    state = load_state()
    seen = set(state.get("seen", []))
    candidates = []

    for feed in feeds():
        print(f"Scanning approved source: {feed}")
        try:
            items = collect_entries(feed, max_items=max_items)
        except Exception as exc:
            print(f"SKIP source: {exc}")
            continue

        for item in items:
            url = normalize_url(item)
            if not url or url in seen:
                continue
            try:
                info = full_info(url)
            except Exception as exc:
                # A direct approved Reel can still be queued even if yt-dlp cannot
                # read metadata; the main downloader will try its public-page fallback.
                if is_direct_post(url):
                    candidates.append({
                        "url": url,
                        "title": "approved Instagram Reel",
                        "duration": 0.0,
                        "score": 1,
                        "view_count": 0,
                    })
                    continue
                print(f"SKIP candidate metadata: {url} ({exc})")
                continue

            try:
                duration = float(info.get("duration") or 0)
            except Exception:
                duration = 0.0
            if 0 < duration < MIN_SECONDS:
                seen.add(url)
                continue
            candidates.append({
                "url": url,
                "title": info.get("title") or "",
                "duration": duration,
                "score": score(info),
                "view_count": safe_int(info.get("view_count")),
            })

    state["seen"] = sorted(seen)
    save_state(state)
    candidates.sort(key=lambda x: (x["score"], x["view_count"]), reverse=True)
    return candidates


def queue_candidate(candidate):
    ensure_queue_file()
    url = canonical_url(candidate["url"])
    existing = [
        canonical_url(line.strip())
        for line in URLS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if url not in existing:
        with URLS_FILE.open("a", encoding="utf-8") as f:
            if URLS_FILE.stat().st_size and not URLS_FILE.read_text(encoding="utf-8").endswith("\n"):
                f.write("\n")
            f.write(url + "\n")

    state = load_state()
    seen = set(state.get("seen", []))
    seen.add(url)
    state["seen"] = sorted(seen)
    state["last_selected"] = candidate
    save_state(state)

    print(f"AUTO-DISCOVERED: {candidate.get('title') or 'source video'}")
    print(f"QUEUED URL: {url}")
    return url


def discover_and_queue_one(max_items=20):
    source_list = feeds()
    if not source_list:
        print(f"No approved discovery sources. Add account/feed URLs to: {FEEDS_FILE}")
        return None
    candidates = discover_candidates(max_items=max_items)
    if not candidates:
        print("No new eligible candidates found in approved sources.")
        return None
    return queue_candidate(candidates[0])


def run_once(max_items=20):
    url = discover_and_queue_one(max_items=max_items)
    return bool(url)


def main():
    ap = argparse.ArgumentParser(description="Discover new videos from approved source feeds and add them to the AutoPilot queue.")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=900, help="Seconds between discovery scans")
    ap.add_argument("--max-items", type=int, default=20)
    args = ap.parse_args()

    print(f"Approved source list: {FEEDS_FILE}")
    print(f"Queue: {URLS_FILE}")
    print(f"Minimum source duration: {MIN_SECONDS:.0f} seconds")
    if args.loop:
        while True:
            try:
                run_once(max_items=args.max_items)
            except Exception as exc:
                print(f"ERROR: {exc}")
            time.sleep(max(60, args.interval))
    else:
        run_once(max_items=args.max_items)


if __name__ == "__main__":
    main()
