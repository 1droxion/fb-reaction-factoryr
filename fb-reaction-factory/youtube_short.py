import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parent
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def _profile_name(profile):
    clean = str(profile or "personal").strip().lower()
    return clean if clean in {"personal", "tvmind", "kids"} else "personal"


def _token_info(profile="personal"):
    profile = _profile_name(profile)
    suffix = profile.upper()
    raw = os.getenv(f"YOUTUBE_TOKEN_JSON_{suffix}", "").strip()
    if raw:
        return json.loads(raw)

    configured = os.getenv(f"YOUTUBE_TOKEN_FILE_{suffix}", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())

    if profile == "personal":
        legacy_raw = os.getenv("YOUTUBE_TOKEN_JSON", "").strip()
        if legacy_raw:
            return json.loads(legacy_raw)
        legacy_file = os.getenv("YOUTUBE_TOKEN_FILE", "").strip()
        if legacy_file:
            candidates.append(Path(legacy_file).expanduser())
        candidates.extend([
            ROOT / "youtube_token.json",
            Path.home() / "youtube-auto-publisher" / "youtube_token.json",
            ROOT.parent.parent / "youtube-auto-publisher" / "youtube_token.json",
        ])
    else:
        candidates.extend([
            ROOT / f"youtube_token_{profile}.json",
            Path.home() / "youtube-auto-publisher" / f"youtube_token_{profile}.json",
        ])

    for path in candidates:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))

    if profile == "personal":
        raise RuntimeError("YouTube Personal is not connected on this worker. Upload/restore the Personal YouTube OAuth token first.")
    raise RuntimeError(f"YouTube profile '{profile}' is not connected. Set YOUTUBE_TOKEN_JSON_{suffix} or YOUTUBE_TOKEN_FILE_{suffix}.")


def _credentials(profile="personal"):
    creds = Credentials.from_authorized_user_info(_token_info(profile), YOUTUBE_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise RuntimeError(f"YouTube login for '{_profile_name(profile)}' is not valid. Reconnect that channel.")
    return creds


def _connected_channel(youtube):
    response = youtube.channels().list(part="snippet", mine=True).execute()
    items = response.get("items") or []
    if items:
        return items[0].get("snippet", {}).get("title")
    return None


def publish_short(video_path, title, description, tags=None, privacy="public", profile="personal", made_for_kids=False):
    video_path = Path(video_path)
    if not video_path.exists() or not video_path.is_file():
        raise RuntimeError(f"YouTube video file not found: {video_path}")
    if privacy not in {"public", "unlisted", "private"}:
        raise RuntimeError("YouTube privacy must be public, unlisted, or private.")

    profile = _profile_name(profile)
    clean_title = (title or "Reaction Short 😂").strip()[:100]
    clean_description = (description or "").strip()
    if "#shorts" not in clean_description.lower():
        clean_description = (clean_description + "\n\n#Shorts").strip()

    clean_tags = []
    for tag in tags or []:
        value = str(tag).strip().lstrip("#")
        if value and value.lower() not in {x.lower() for x in clean_tags}:
            clean_tags.append(value[:30])
    if "shorts" not in {x.lower() for x in clean_tags}:
        clean_tags.append("Shorts")

    youtube = build("youtube", "v3", credentials=_credentials(profile), cache_discovery=False)
    channel = _connected_channel(youtube)
    body = {
        "snippet": {
            "title": clean_title,
            "description": clean_description[:5000],
            "tags": clean_tags[:30],
            "categoryId": os.getenv(f"YOUTUBE_CATEGORY_ID_{profile.upper()}", os.getenv("YOUTUBE_CATEGORY_ID", "24")),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"YouTube Shorts upload ({profile}): {int(status.progress() * 100)}%")

    video_id = response["id"]
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "channel": channel,
        "profile": profile,
        "privacy": privacy,
        "made_for_kids": bool(made_for_kids),
    }


def video_views(video_id, profile="personal"):
    if not video_id:
        return 0
    youtube = build("youtube", "v3", credentials=_credentials(profile), cache_discovery=False)
    response = youtube.videos().list(part="statistics", id=str(video_id)).execute()
    items = response.get("items") or []
    if not items:
        return 0
    try:
        return int((items[0].get("statistics") or {}).get("viewCount") or 0)
    except Exception:
        return 0
