#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def find_youtube_factory():
    configured = os.getenv("YOUTUBE_FACTORY_DIR", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([
        ROOT.parent.parent / "youtube-auto-publisher",
        ROOT.parent / "youtube-auto-publisher",
        Path.home() / "youtube-auto-publisher",
    ])
    for candidate in candidates:
        if (candidate / "main.py").exists() and (candidate / "pipeline.py").exists():
            return candidate.resolve()
    raise RuntimeError(
        "YouTube Factory is not installed on this computer yet. From C:\\Users\\sgree run: "
        "git clone https://github.com/1droxion/youtube-auto-publisher.git"
    )


def run_youtube_factory(url, rights_ok, privacy="private", music_policy="stop", on_line=None):
    if not rights_ok:
        raise RuntimeError("Confirm that you own or have permission/license to reuse this video.")
    if privacy not in {"private", "unlisted", "public"}:
        raise RuntimeError("YouTube privacy must be private, unlisted, or public.")
    if music_policy not in {"stop", "mute", "ignore"}:
        raise RuntimeError("YouTube music policy must be stop, mute, or ignore.")

    factory = find_youtube_factory()
    cmd = [
        sys.executable,
        "main.py",
        "--url", url,
        "--rights-ok",
        "--music-policy", music_policy,
        "--privacy", privacy,
    ]
    env = os.environ.copy()
    process = subprocess.Popen(
        cmd,
        cwd=str(factory),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    tail = []
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.rstrip()
        print(f"[YouTube Factory] {line}")
        tail.append(line)
        tail = tail[-25:]
        if on_line:
            on_line(line)

    code = process.wait()
    if code != 0:
        detail = "\n".join(x for x in tail[-12:] if x).strip()
        raise RuntimeError(detail or f"YouTube Factory stopped with exit code {code}.")

    result_path = factory / "output" / "result.json"
    if not result_path.exists():
        raise RuntimeError("YouTube Factory finished but output/result.json was not found.")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not result.get("uploaded"):
        raise RuntimeError("YouTube Factory finished without uploading the video.")
    return result
