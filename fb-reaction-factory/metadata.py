import json
import os
import re
import requests


def fallback_metadata(caption: str):
    raw = (caption or "").strip()
    raw = re.sub(r"\s+", " ", raw)

    # Uploaded files often have random UUID/hash names. Never expose those as a Reel title.
    compact = re.sub(r"[^A-Za-z0-9]", "", raw)
    looks_random = bool(
        re.fullmatch(r"[0-9a-fA-F]{16,}", compact)
        or re.fullmatch(r"[A-Za-z0-9]{24,}", compact)
    )

    if not raw or looks_random or raw.lower() in {"funny reaction", "reaction"}:
        title = "Wait for the ending 😂"
    else:
        title = raw[:70]

    return {
        "title": title,
        "description": "This reaction gets better at the end 😂 Follow for more daily funny reactions.",
        "hashtags": ["#FunnyVideos", "#ReactionVideo", "#ComedyReels", "#WaitForIt", "#FunnyReels"],
    }


def generate_metadata(caption: str):
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return fallback_metadata(caption)

    model = os.getenv("OPENAI_MODEL", "gpt-5")
    prompt = f"""Create Facebook Reel metadata for a funny reaction video aimed at a US audience.
Source context: {caption or 'funny short-form clip'}
Return ONLY JSON with keys title, description, hashtags.
Rules: catchy but not misleading; title under 70 characters; description one short sentence plus a light follow CTA; 5 relevant hashtags; never repeat the title inside the description; never use raw filenames, UUIDs, hashes, or file IDs as title text; do not claim ownership of the source clip."""
    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "input": prompt, "store": False},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    text_parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text_parts.append(content.get("text", ""))
    text = "\n".join(text_parts).strip()
    try:
        return json.loads(text)
    except Exception:
        return fallback_metadata(caption)
