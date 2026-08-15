import os
from pathlib import Path
import requests


def _config():
    page_id = os.getenv("META_PAGE_ID", "").strip()
    token = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
    version = os.getenv("META_GRAPH_VERSION", "v24.0").strip()
    if not page_id or not token:
        raise RuntimeError("Set META_PAGE_ID and META_PAGE_ACCESS_TOKEN first.")
    return page_id, token, version


def publish_reel(video_path, description, state="PUBLISHED"):
    page_id, token, version = _config()
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    start_url = f"https://graph.facebook.com/{version}/{page_id}/video_reels"
    start = requests.post(start_url, data={"upload_phase": "start", "access_token": token}, timeout=60)
    start.raise_for_status()
    start_data = start.json()
    video_id = start_data["video_id"]

    upload_url = f"https://rupload.facebook.com/video-upload/{version}/{video_id}"
    size = video_path.stat().st_size
    with video_path.open("rb") as f:
        up = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(size),
                "Content-Type": "application/octet-stream",
            },
            data=f,
            timeout=900,
        )
    up.raise_for_status()

    finish = requests.post(
        start_url,
        data={
            "upload_phase": "finish",
            "video_state": state,
            "allow_video_remixing": "false",
            "video_id": video_id,
            "access_token": token,
            "description": description,
        },
        timeout=60,
    )
    finish.raise_for_status()
    return {"video_id": video_id, "response": finish.json()}
