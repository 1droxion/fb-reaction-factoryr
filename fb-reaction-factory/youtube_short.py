import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parent
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def _token_info():
    raw = os.getenv("YOUTUBE_TOKEN_JSON", "").strip()
    if raw:
        return json.loads(raw)

    configured = os.getenv("YOUTUBE_TOKEN_FILE", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([
        ROOT / "youtube_token.json",
        Path.home() / "youtube-auto-publisher" / "youtube_token.json",
        ROOT.parent.parent / "youtube-auto-publisher" / "youtube_token.json",
    ])
    for path in candidates:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))

    raise RuntimeError(
        "YouTube is not connected on this computer. Open C:\\Users\\sgree\\youtube-auto-publisher "
        "and run: python connect_youtube.py"
    )


def _credentials():
    creds = Credentials.from_authorized_user_info(_token_info(), [YOUTUBE_SCOPE])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise RuntimeError("YouTube login is not valid. Run python connect_youtube.py again.")
    return creds


def _connected_channel(youtube):
    try:
        response = youtube.channels().list(part="snippet", mine=True).execute()
        items = response.get("items") or []
        if items:
            return items[0].get("snippet", {}).get("title")
    except Exception:
        pass
    return None


def publish_short(video_path, title, description, tags=None, privacy="public"):
    video_path = Path(video_path)
    if not video_path.exists() or not video_path.is_file():
        raise RuntimeError(f"YouTube video file not found: {video_path}")
    if privacy not in {"public", "unlisted", "private"}:
        raise RuntimeError("YouTube privacy must be public, unlisted, or private.")

    clean_title = (title or "Reaction Short 😂").strip()[:100]
    clean_description = (description or "").strip()
    if "#shorts" not in clean_description.lower():
        clean_description = (clean_description + "\n\n#Shorts").strip()

    clean_tags = []
    for tag in tags or []:
        value = str(tag).strip().lstrip("#")
        if value and value.lower() not in {x.lower() for x in clean_tags}:
            clean_tags.append(value[:30])
    if "Shorts".lower() not in {x.lower() for x in clean_tags}:
        clean_tags.append("Shorts")

    youtube = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)
    channel = _connected_channel(youtube)
    body = {
        "snippet": {
            "title": clean_title,
            "description": clean_description[:5000],
            "tags": clean_tags[:30],
            "categoryId": os.getenv("YOUTUBE_CATEGORY_ID", "24"),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"YouTube Shorts upload: {int(status.progress() * 100)}%")

    video_id = response["id"]
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "channel": channel,
        "privacy": privacy,
    }
