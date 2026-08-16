#!/usr/bin/env python3
import argparse
import html
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "sources" / "approved_inbox"
INBOX.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)


def is_instagram_url(url: str):
    return bool(re.match(r"^https?://(www\.)?instagram\.com/(reel|reels|p)/", url.strip(), re.I))


def ytdlp_download(url: str, token: str):
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp is not installed. Run: python3 -m pip install -r requirements.txt")

    template = str(INBOX / f"instagram_{token}.%(ext)s")
    cmd = [
        exe,
        "--no-playlist",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", template,
        url,
    ]
    subprocess.run(cmd, check=True)

    matches = sorted(
        INBOX.glob(f"instagram_{token}.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise RuntimeError("yt-dlp finished but no Instagram video file was found.")
    return matches[0]


def _meta_video_url(page_text: str):
    patterns = [
        r'<meta[^>]+property=["\']og:video:secure_url["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video:secure_url["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video["\']',
        r'"video_url"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, page_text, re.I | re.S)
        if not match:
            continue
        value = html.unescape(match.group(1))
        value = value.replace("\\u0026", "&").replace("\\/", "/")
        return value
    return None


def public_page_download(url: str, token: str):
    session = requests.Session()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }

    candidates = [url]
    match = re.search(r"instagram\.com/(?:reel|reels|p)/([^/?#]+)", url, re.I)
    if match:
        code = match.group(1)
        candidates.append(f"https://www.instagram.com/reel/{code}/embed/")

    last_error = None
    for page_url in candidates:
        try:
            response = session.get(page_url, headers=headers, timeout=60, allow_redirects=True)
            response.raise_for_status()
            video_url = _meta_video_url(response.text)
            if not video_url:
                last_error = "No public video URL was exposed on the Instagram page."
                continue

            video = session.get(
                video_url,
                headers={"User-Agent": USER_AGENT, "Referer": "https://www.instagram.com/"},
                stream=True,
                timeout=120,
                allow_redirects=True,
            )
            video.raise_for_status()
            content_type = (video.headers.get("content-type") or "").lower()
            if "video" not in content_type and "octet-stream" not in content_type:
                last_error = f"Instagram returned {content_type or 'unknown content'} instead of video."
                continue

            target = INBOX / f"instagram_{token}.mp4"
            with target.open("wb") as f:
                for chunk in video.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

            if target.stat().st_size < 10000:
                target.unlink(missing_ok=True)
                last_error = "Downloaded Instagram file was too small to be a usable video."
                continue
            return target
        except Exception as exc:
            last_error = str(exc)

    raise RuntimeError(last_error or "Instagram public-page download failed.")


def download_instagram_reel(url: str):
    url = url.strip()
    if not is_instagram_url(url):
        raise ValueError("Use a public Instagram Reel/Post URL.")

    token = uuid.uuid4().hex[:10]
    print("Trying Instagram download with yt-dlp...")
    try:
        path = ytdlp_download(url, token)
        print(f"Downloaded: {path}")
        return path
    except Exception as first_error:
        print(f"yt-dlp could not download it: {first_error}")

    print("Trying Instagram public-page fallback...")
    try:
        path = public_page_download(url, token)
        print(f"Downloaded: {path}")
        return path
    except Exception as second_error:
        raise RuntimeError(
            "Instagram did not expose this Reel as a public downloadable video. "
            "Private, login-only, age/region-restricted, or otherwise blocked Reels are not bypassed. "
            f"Fallback error: {second_error}"
        )


def main():
    ap = argparse.ArgumentParser(description="Download a public Instagram Reel into Reaction Factory approved_inbox.")
    ap.add_argument("url", help="Public Instagram Reel/Post URL")
    args = ap.parse_args()
    path = download_instagram_reel(args.url)
    print("\nINSTAGRAM DOWNLOAD SUCCESS")
    print(f"Saved to: {path}")
    print("Next: run python3 auto_watch.py to build the reaction Reel.")


if __name__ == "__main__":
    main()
