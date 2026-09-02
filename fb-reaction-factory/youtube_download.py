#!/usr/bin/env python3
import base64
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)


def ytdlp():
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp is not installed.")
    return exe


def is_youtube_url(url):
    value = str(url or "").lower()
    return "youtube.com/" in value or "youtu.be/" in value


def _cookie_path():
    configured = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.exists() and path.is_file():
            return path

    raw = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if raw:
        target = DATA / "youtube_cookies.txt"
        try:
            target.write_bytes(base64.b64decode(raw))
        except Exception as exc:
            raise RuntimeError(f"YOUTUBE_COOKIES_B64 could not be decoded: {exc}")
        if target.exists() and target.stat().st_size > 0:
            return target
    return None


def _base_args():
    args = [
        ytdlp(), "--no-playlist", "--socket-timeout", "30",
        "--retries", "3", "--fragment-retries", "3",
        "--merge-output-format", "mp4",
    ]
    cookies = _cookie_path()
    if cookies:
        args += ["--cookies", str(cookies)]
    return args


def metadata_json(url, timeout=90):
    cmd = _base_args() + ["--skip-download", "--dump-single-json", url]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout
    return None


def download_youtube(url, template, timeout=1800):
    strategies = [
        ("default", [], "bv*+ba/b"),
        ("web_safari", ["--extractor-args", "youtube:player_client=web_safari"], "bv*+ba/b"),
        ("android_vr_360p", ["--extractor-args", "youtube:player_client=android_vr"], "18/best[height<=720]/best"),
        ("web_embedded", ["--extractor-args", "youtube:player_client=web_embedded"], "bv*+ba/b"),
    ]

    errors = []
    for label, extra, fmt in strategies:
        print(f"YouTube download attempt: {label}")
        cmd = _base_args() + extra + ["-f", fmt, "-o", template, url]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0:
            return True
        detail = (proc.stderr or proc.stdout or "yt-dlp failed").strip()
        if len(detail) > 1800:
            detail = detail[-1800:]
        errors.append(f"{label}: {detail}")
        print(f"YouTube attempt {label} failed: {detail[-700:]}")

    joined = "\n\n".join(errors)
    lower = joined.lower()
    if "sign in to confirm" in lower or "not a bot" in lower or "cookies" in lower:
        raise RuntimeError(
            "YouTube blocked the cloud downloader and requested browser authentication. "
            "Reaction Factory tried multiple YouTube clients automatically. "
            "A one-time YouTube cookies connection is required for this source, or use an owned Google Drive/video file URL. "
            f"Last downloader detail: {errors[-1][-1200:]}"
        )
    raise RuntimeError(f"YouTube download failed after {len(strategies)} methods. Last detail: {errors[-1][-1500:]}")
