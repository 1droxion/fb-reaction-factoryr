#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FEEDS_FILE = DATA / "source_feeds.txt"
URLS_FILE = DATA / "approved_urls.txt"
STATE_FILE = DATA / "discovery_state.json"
ENV_FILE = ROOT / ".env"
MIN_SECONDS = 35.0
MAX_SECONDS = 60.0
FUNNY_KEYWORDS = (
    "funny", "comedy", "comedian", "joke", "jokes", "prank", "lol", "laugh",
    "laughing", "funniest", "meme", "memes", "roast", "humor", "humour",
    "fail", "fails", "reaction", "try not to laugh", "comedyreels", "funnyreels"
)
HINDI_KEYWORDS = (
    "hindi", "hindicomedy", "desi", "desicomedy", "indian", "india", "indiancomedy",
    "hindustani", "bollywood", "kapil", "sharma", "bhai", "bhaiya", "yaar", "dost",
    "mummy", "papa", "shaadi", "biwi", "pati", "patni", "saas", "bahu", "ladka", "ladki"
)
DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

DATA.mkdir(parents=True, exist_ok=True)


def load_env_file():
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


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


def instagram_profile_username(url):
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    if parsed.netloc.lower() not in ("instagram.com", "www.instagram.com"):
        return None
    parts = [x for x in parsed.path.split("/") if x]
    if len(parts) != 1:
        return None
    username = parts[0].strip()
    if username.lower() in ("reel", "reels", "p", "explore"):
        return None
    return username or None


def own_instagram_username():
    load_env_file()
    return os.getenv("META_IG_USERNAME", "").strip().lstrip("@").lower()


def feeds():
    if not FEEDS_FILE.exists():
        FEEDS_FILE.write_text(
            "# Put one approved Hindi comedy source account/feed URL per line.\n"
            "# Only list sources whose videos you own or have permission/license to reuse.\n"
            "# AutoPilot only selects funny Hindi candidates and enforces 35-60 seconds.\n"
            "# Your own Instagram profile is automatically excluded.\n",
            encoding="utf-8",
        )
        return []

    own = own_instagram_username()
    out = []
    for raw in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        feed = raw.strip()
        if not feed or feed.startswith("#"):
            continue
        username = instagram_profile_username(feed)
        if username and own and username.lower() == own:
            print(f"SKIP own Instagram profile: @{username}")
            continue
        out.append(feed)
    return out


def ensure_queue_file():
    if not URLS_FILE.exists():
        URLS_FILE.write_text("# AutoPilot queue. One approved source URL per line.\n", encoding="utf-8")


def canonical_url(url):
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return url
    parsed = urlparse(url)
    if "instagram.com" in parsed.netloc.lower():
        match = re.search(r"/(reel|reels|p)/([^/?#]+)", parsed.path, re.I)
        if match:
            kind = "reel" if match.group(1).lower() in ("reel", "reels") else "p"
            return f"https://www.instagram.com/{kind}/{match.group(2)}/"
    return url


def ytdlp():
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp is not installed.")
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
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value.startswith(("http://", "https://")):
            return canonical_url(value)
        if value.startswith("/") and "/" in value:
            return canonical_url("https://www.instagram.com" + value)
    return None


def text_blob(info):
    values = []
    for key in ("title", "description", "caption", "tags"):
        value = info.get(key)
        if isinstance(value, (list, tuple)):
            values.extend(str(x) for x in value)
        elif value:
            values.append(str(value))
    return " ".join(values).lower()


def looks_funny(text):
    text = (text or "").lower()
    return any(word in text for word in FUNNY_KEYWORDS)


def looks_hindi(text):
    text = text or ""
    lowered = text.lower()
    return bool(DEVANAGARI_RE.search(text)) or any(word in lowered for word in HINDI_KEYWORDS)


def looks_hindi_funny(text):
    return looks_funny(text) and looks_hindi(text)


def duration_allowed(duration):
    try:
        value = float(duration or 0)
    except Exception:
        return False
    return MIN_SECONDS <= value <= MAX_SECONDS


def meta_error(response):
    try:
        data = response.json()
    except Exception:
        return f"HTTP {response.status_code}: {response.text[:300]}"
    error = data.get("error") or {}
    msg = error.get("message") or f"HTTP {response.status_code}"
    code = error.get("code")
    subcode = error.get("error_subcode")
    if code is not None:
        msg += f" (code {code}"
        if subcode is not None:
            msg += f", subcode {subcode}"
        msg += ")"
    return msg


def meta_get(path, params):
    load_env_file()
    version = os.getenv("META_GRAPH_VERSION", "v26.0").strip() or "v26.0"
    tokens = []
    for key in ("META_USER_ACCESS_TOKEN", "META_PAGE_ACCESS_TOKEN"):
        token = os.getenv(key, "").strip()
        if token and token not in tokens:
            tokens.append(token)
    if not tokens:
        raise RuntimeError("Missing Meta token.")

    last_error = None
    for token in tokens:
        call_params = dict(params)
        call_params["access_token"] = token
        response = requests.get(
            f"https://graph.facebook.com/{version}/{path}",
            params=call_params,
            timeout=60,
        )
        if response.ok:
            data = response.json()
            if "error" not in data:
                return data
        last_error = meta_error(response)
    raise RuntimeError(f"Meta discovery error: {last_error or 'unknown Meta error'}")


def meta_instagram_media(username, max_items=20):
    load_env_file()
    ig_id = os.getenv("META_IG_USER_ID", "").strip()
    own = own_instagram_username()
    if own and username.lower() == own:
        print(f"SKIP own Instagram profile: @{username}")
        return []
    if not ig_id:
        raise RuntimeError("META_IG_USER_ID is missing.")

    fields = "id,caption,media_type,media_url,permalink,timestamp"
    expanded = (
        f"business_discovery.username({username})"
        "{username,media.limit(" + str(max_items) + "){" + fields + "}}"
    )
    data = meta_get(ig_id, {"fields": expanded})
    media = ((data.get("business_discovery") or {}).get("media") or {}).get("data") or []

    candidates = []
    for item in media:
        if not isinstance(item, dict):
            continue
        if str(item.get("media_type") or "").upper() != "VIDEO":
            continue
        media_url = str(item.get("media_url") or "").strip()
        permalink = canonical_url(str(item.get("permalink") or "").strip())
        if not permalink and not media_url:
            continue
        caption = str(item.get("caption") or "")
        if not looks_hindi_funny(caption):
            continue
        media_id = str(item.get("id") or "").strip()
        points = sum(2 for word in FUNNY_KEYWORDS if word in caption.lower())
        points += sum(2 for word in HINDI_KEYWORDS if word in caption.lower())
        if DEVANAGARI_RE.search(caption):
            points += 5
        candidates.append({
            "url": permalink or media_url,
            "permalink": permalink,
            "seen_key": media_id or permalink or media_url,
            "title": caption[:120] or f"Hindi funny candidate from @{username}",
            "duration": 0.0,
            "score": points + 3,
            "view_count": 0,
            "timestamp": str(item.get("timestamp") or ""),
            "source_account": username,
            "discovery_method": "meta_api",
        })
    return candidates


def collect_entries(feed_url, max_items=20):
    if is_direct_post(feed_url):
        return [{"webpage_url": canonical_url(feed_url)}]
    data = run_json([
        ytdlp(), "--flat-playlist", "--playlist-end", str(max_items),
        "--dump-single-json", feed_url,
    ])
    entries = data.get("entries")
    if isinstance(entries, list):
        return [x for x in entries if isinstance(x, dict)]
    return [data]


def full_info(url):
    return run_json([ytdlp(), "--skip-download", "--no-playlist", "--dump-single-json", url])


def scrape_instagram_profile_links(username, max_items=20):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    found = []
    seen = set()
    for page_url in (
        f"https://www.instagram.com/{username}/",
        f"https://www.instagram.com/{username}/reels/",
    ):
        try:
            response = requests.get(page_url, headers=headers, timeout=30)
            if not response.ok:
                continue
            text = response.text.replace("\\/", "/")
        except Exception:
            continue

        for match in re.finditer(r"/(reel|p)/([A-Za-z0-9_-]{5,})/?", text, re.I):
            kind = "reel" if match.group(1).lower() == "reel" else "p"
            url = f"https://www.instagram.com/{kind}/{match.group(2)}/"
            if url in seen:
                continue
            seen.add(url)
            found.append({"webpage_url": url})
            if len(found) >= max_items:
                return found
    return found


def public_instagram_entries(feed_url, username, max_items=20):
    errors = []
    targets = [feed_url, f"https://www.instagram.com/{username}/reels/"]
    for target in targets:
        try:
            items = collect_entries(target, max_items=max_items)
            if items:
                print(f"Public Instagram fallback succeeded with yt-dlp: {target}")
                return items
        except Exception as exc:
            errors.append(str(exc))

    items = scrape_instagram_profile_links(username, max_items=max_items)
    if items:
        print(f"Public Instagram fallback found {len(items)} post links from the profile page.")
        return items

    if errors:
        raise RuntimeError(errors[-1])
    raise RuntimeError("No public Instagram post links were discoverable.")


def safe_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def score(info):
    text = text_blob(info)
    points = sum(2 for word in FUNNY_KEYWORDS if word in text)
    points += sum(2 for word in HINDI_KEYWORDS if word in text)
    if DEVANAGARI_RE.search(text):
        points += 5
    try:
        duration = float(info.get("duration") or 0)
    except Exception:
        duration = 0
    if duration_allowed(duration):
        points += 8
    views = safe_int(info.get("view_count"))
    if views >= 1000000:
        points += 4
    elif views >= 100000:
        points += 2
    elif views >= 10000:
        points += 1
    return points


def candidates_from_entries(items, seen, *, source_account=None, method="public_feed"):
    candidates = []
    for item in items:
        url = normalize_url(item)
        if not url or url in seen:
            continue

        try:
            info = full_info(url)
        except Exception as exc:
            print(f"SKIP candidate because duration/language cannot be verified: {url} ({exc})")
            continue

        try:
            duration = float(info.get("duration") or 0)
        except Exception:
            duration = 0.0
        if not duration_allowed(duration):
            seen.add(url)
            print(f"SKIP duration {duration:.1f}s; need 35-60s: {url}")
            continue

        candidate_text = text_blob(info)
        if not looks_hindi_funny(candidate_text):
            seen.add(url)
            print(f"SKIP non-Hindi/non-funny candidate: {url}")
            continue

        candidates.append({
            "url": url,
            "seen_key": url,
            "title": info.get("title") or "",
            "duration": duration,
            "score": score(info),
            "view_count": safe_int(info.get("view_count")),
            "timestamp": str(info.get("timestamp") or info.get("upload_date") or ""),
            "source_account": source_account,
            "discovery_method": method,
        })
    return candidates


def discover_candidates(max_items=20):
    state = load_state()
    seen = set(state.get("seen", []))
    candidates = []

    for feed in feeds():
        username = instagram_profile_username(feed)
        if username:
            print(f"Scanning approved Hindi comedy Instagram source: @{username}")
            try:
                items = meta_instagram_media(username, max_items=max_items)
                for candidate in items:
                    key = candidate.get("seen_key") or candidate.get("permalink") or candidate.get("url")
                    if key and key not in seen:
                        candidates.append(candidate)
                if items:
                    continue
            except Exception as exc:
                print(f"Meta Instagram discovery unavailable: {exc}")
                print("Trying public-profile fallback instead...")

            try:
                public_items = public_instagram_entries(feed, username, max_items=max_items)
                candidates.extend(
                    candidates_from_entries(
                        public_items,
                        seen,
                        source_account=username,
                        method="public_instagram_fallback",
                    )
                )
            except Exception as exc:
                print(f"SKIP Instagram source after fallback: {exc}")
            continue

        print(f"Scanning approved Hindi funny-video source: {feed}")
        try:
            items = collect_entries(feed, max_items=max_items)
            candidates.extend(candidates_from_entries(items, seen))
        except Exception as exc:
            print(f"SKIP source: {exc}")

    state["seen"] = sorted(seen)
    save_state(state)
    candidates.sort(
        key=lambda x: (x.get("score", 0), x.get("view_count", 0), x.get("timestamp", "")),
        reverse=True,
    )
    return candidates


def queue_candidate(candidate):
    ensure_queue_file()
    url = str(candidate["url"]).strip()
    existing = [
        line.strip() for line in URLS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if url not in existing:
        current = URLS_FILE.read_text(encoding="utf-8")
        with URLS_FILE.open("a", encoding="utf-8") as f:
            if current and not current.endswith("\n"):
                f.write("\n")
            f.write(url + "\n")

    state = load_state()
    seen = set(state.get("seen", []))
    key = candidate.get("seen_key") or candidate.get("permalink") or url
    seen.add(key)
    state["seen"] = sorted(seen)
    state["last_selected"] = candidate
    save_state(state)

    print(f"AUTO-DISCOVERED HINDI FUNNY: {candidate.get('title') or 'approved source video'}")
    print(f"SOURCE: {candidate.get('permalink') or url}")
    print("Queued approved 35-60 second source for Instagram processing.")
    return url


def discover_and_queue_one(max_items=20):
    source_list = feeds()
    if not source_list:
        print(f"No approved Hindi comedy discovery sources. Add licensed/approved source feeds to: {FEEDS_FILE}")
        return None
    candidates = discover_candidates(max_items=max_items)
    if not candidates:
        print("No new eligible Hindi funny 35-60 second candidates found in approved sources.")
        return None
    return queue_candidate(candidates[0])


def run_once(max_items=20):
    return bool(discover_and_queue_one(max_items=max_items))


def main():
    ap = argparse.ArgumentParser(description="Discover Hindi funny videos 35-60 seconds from approved sources, excluding your own Instagram profile.")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=900)
    ap.add_argument("--max-items", type=int, default=20)
    args = ap.parse_args()

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
