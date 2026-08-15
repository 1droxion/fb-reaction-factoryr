#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SOURCES = ROOT / "sources"
INBOX = SOURCES / "approved_inbox"
FEEDS_FILE = DATA / "source_feeds.txt"
STATE_FILE = DATA / "discovery_state.json"
MIN_SECONDS = 60.0
KEYWORDS = (
    "funny", "comedy", "fail", "fails", "prank", "lol", "laugh",
    "unexpected", "crazy", "funniest", "try not to laugh", "viral",
    "dog", "cat", "baby", "reaction", "oops"
)

for folder in (DATA, SOURCES, INBOX):
    folder.mkdir(parents=True, exist_ok=True)


def load_state():
    if not STATE_FILE.exists():
        return {"seen": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def feeds():
    if not FEEDS_FILE.exists():
        FEEDS_FILE.write_text(
            "# Put one source/feed/account/collection URL per line.\n"
            "# Adding a source here means you approve the system to use it.\n",
            encoding="utf-8",
        )
        return []
    out = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def ytdlp():
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp is not installed. Run: python3 -m pip install yt-dlp")
    return exe


def run_json(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip() or "yt-dlp failed")
    return json.loads(p.stdout)


def normalize_url(item):
    for key in ("webpage_url", "original_url", "url"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def collect_entries(feed_url, max_items=20):
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


def score(info):
    text = " ".join(str(info.get(k) or "") for k in ("title", "description", "tags")).lower()
    points = sum(2 for word in KEYWORDS if word in text)
    duration = float(info.get("duration") or 0)
    if 60 <= duration <= 95:
        points += 5
    elif duration >= 60:
        points += 2
    view_count = int(info.get("view_count") or 0)
    if view_count >= 1000000:
        points += 4
    elif view_count >= 100000:
        points += 2
    elif view_count >= 10000:
        points += 1
    return points


def discover_candidates(max_items=20):
    state = load_state()
    seen = set(state.get("seen", []))
    candidates = []

    for feed in feeds():
        print(f"Scanning source: {feed}")
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
                print(f"SKIP candidate metadata: {url} ({exc})")
                continue
            duration = float(info.get("duration") or 0)
            if duration < MIN_SECONDS:
                seen.add(url)
                continue
            candidates.append({
                "url": url,
                "title": info.get("title") or "",
                "duration": duration,
                "score": score(info),
                "view_count": info.get("view_count") or 0,
            })

    state["seen"] = sorted(seen)
    save_state(state)
    candidates.sort(key=lambda x: (x["score"], x["view_count"]), reverse=True)
    return candidates


def download_candidate(candidate):
    token = uuid.uuid4().hex[:10]
    template = str(INBOX / f"auto_{token}.%(ext)s")
    cmd = [
        ytdlp(), "--no-playlist",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", template,
        candidate["url"],
    ]
    print(f"SELECTED: {candidate['title']}")
    print(f"URL: {candidate['url']}")
    print(f"Duration: {candidate['duration']:.1f}s | Score: {candidate['score']}")
    subprocess.run(cmd, check=True)

    state = load_state()
    seen = set(state.get("seen", []))
    seen.add(candidate["url"])
    state["seen"] = sorted(seen)
    state["last_selected"] = candidate
    save_state(state)


def run_once(max_items=20):
    source_list = feeds()
    if not source_list:
        print(f"No sources configured. Add account/feed URLs to: {FEEDS_FILE}")
        return False
    candidates = discover_candidates(max_items=max_items)
    if not candidates:
        print("No new 60+ second candidates found.")
        return False
    download_candidate(candidates[0])
    print(f"Downloaded into: {INBOX}")
    print("The auto watcher can now edit it automatically.")
    return True


def main():
    ap = argparse.ArgumentParser(description="Automatically discover and select 60+ second source videos from approved source feeds.")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=900, help="Seconds between discovery scans")
    ap.add_argument("--max-items", type=int, default=20)
    args = ap.parse_args()

    print(f"Source list: {FEEDS_FILE}")
    print(f"Target: videos at least {MIN_SECONDS:.0f} seconds")
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
