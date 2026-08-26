import json
import os
import re
import requests


def _clean_context(value: str):
    raw = (value or "").strip()
    raw = re.sub(r"https?://\S+", " ", raw)
    raw = re.sub(r"@([A-Za-z0-9._]+)", r"\1", raw)
    raw = re.sub(r"#([A-Za-z0-9_]+)", r"\1", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" -|:.,")
    return raw


def _looks_random(value: str):
    compact = re.sub(r"[^A-Za-z0-9]", "", value or "")
    return bool(
        re.fullmatch(r"[0-9a-fA-F]{16,}", compact)
        or re.fullmatch(r"[A-Za-z0-9]{24,}", compact)
    )


def _normalize_hashtags(values):
    if isinstance(values, str):
        values = values.split()
    out = []
    for value in values or []:
        tag = re.sub(r"[^A-Za-z0-9_]", "", str(value).lstrip("#"))
        if not tag:
            continue
        formatted = "#" + tag[:30]
        if formatted.lower() not in {x.lower() for x in out}:
            out.append(formatted)
        if len(out) == 5:
            break
    defaults = ["#FunnyReels", "#ReactionVideo", "#ComedyReels", "#WaitForIt", "#FunnyVideos"]
    for tag in defaults:
        if len(out) == 5:
            break
        if tag.lower() not in {x.lower() for x in out}:
            out.append(tag)
    return out[:5]


def fallback_metadata(caption: str):
    raw = _clean_context(caption)
    generic = (
        not raw
        or _looks_random(raw)
        or raw.lower() in {"funny reaction", "reaction", "instagram reel", "approved video"}
    )

    if generic:
        title = "I was not ready for that ending 😂"
    else:
        first = re.split(r"[.!?]|\s[-|]\s", raw, maxsplit=1)[0].strip()
        first = first or raw
        title = first[:67].rstrip(" ,.-")
        if len(title) < 58 and not re.search(r"[😂🤣😳😅]", title):
            title += " 😂"

    return {
        "title": title[:70],
        "description": "My reaction says it all 😂 Follow for more daily reaction Reels.",
        "hashtags": ["#FunnyReels", "#ReactionVideo", "#ComedyReels", "#WaitForIt", "#FunnyVideos"],
    }


def _validate_metadata(value, fallback):
    if not isinstance(value, dict):
        return fallback
    title = _clean_context(str(value.get("title") or ""))[:70].strip()
    description = re.sub(r"\s+", " ", str(value.get("description") or "")).strip()[:220]
    hashtags = _normalize_hashtags(value.get("hashtags"))
    if not title:
        title = fallback["title"]
    if not description:
        description = fallback["description"]
    return {"title": title, "description": description, "hashtags": hashtags}


def generate_metadata(caption: str):
    fallback = fallback_metadata(caption)
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return fallback

    model = os.getenv("OPENAI_MODEL", "gpt-5")
    prompt = f"""Create Instagram Reel metadata for a funny reaction video aimed at an English-speaking US audience.
Source context: {caption or 'funny short-form clip'}
Return ONLY JSON with keys title, description, hashtags.
Rules:
- catchy but truthful, no fake claims or misleading clickbait
- title under 70 characters and natural for Instagram
- description is one short sentence plus a light follow CTA
- exactly 5 relevant hashtags
- do not repeat the title in the description
- never expose filenames, UUIDs, hashes, file IDs, or technical text
- do not claim ownership of the source clip
- focus on the reaction/entertainment angle
"""
    try:
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
        return _validate_metadata(json.loads(text), fallback)
    except Exception:
        return fallback
