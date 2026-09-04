#!/usr/bin/env python3
import getpass
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v26.0")
EXPECTED_PAGE_NAME = "D6x8 Gamer"

MANAGED_KEYS = {
    "META_PAGE_ID_GAMING",
    "META_SYSTEM_USER_ACCESS_TOKEN_GAMING",
    "META_PAGE_ACCESS_TOKEN_GAMING",
    "META_USER_ACCESS_TOKEN_GAMING",
}


def graph_get(path, token, fields=None):
    params = {"access_token": token}
    if fields:
        params["fields"] = fields
    response = requests.get(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{path}",
        params=params,
        timeout=60,
    )
    try:
        data = response.json()
    except Exception:
        data = {"error": {"message": response.text[:500]}}
    if not response.ok or "error" in data:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("message") or f"HTTP {response.status_code}"
        code = error.get("code")
        detail = f"Meta API error: {message}"
        if code is not None:
            detail += f" (code {code})"
        raise RuntimeError(detail)
    return data


def write_env(values):
    existing = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    kept = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            kept.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key not in MANAGED_KEYS:
            kept.append(line)
    if kept and kept[-1].strip():
        kept.append("")
    kept.append("# Gaming Meta connection - non-expiring System User token")
    for key, value in values.items():
        kept.append(f"{key}={value}")
    kept.append("")
    ENV_FILE.write_text("\n".join(kept), encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)
    except OSError:
        pass


def main():
    print("D6x8 Gamer Facebook permanent connection")
    print("Use a Meta Business System User token generated with expiration: Never.")
    print("Assign the D6x8 Gamer Page to that System User before continuing.")
    print("The token is hidden while you paste it and is never printed.")

    page_id = input("Paste D6x8 Gamer Facebook Page ID (numbers only): ").strip()
    if not page_id.isdigit():
        raise SystemExit("Invalid Page ID. Use the numeric Facebook Page ID from Business Settings.")

    system_token = getpass.getpass("Paste System User token (Never expires): ").strip()
    if not system_token:
        raise SystemExit("No System User token entered.")

    check = graph_get(page_id, system_token, fields="id,name")
    returned_name = str(check.get("name") or "").strip()
    returned_id = str(check.get("id") or "").strip()
    if returned_id != page_id:
        raise SystemExit("Meta returned an unexpected Page ID.")
    if returned_name.lower() != EXPECTED_PAGE_NAME.lower():
        raise SystemExit(f"Wrong Page. Expected '{EXPECTED_PAGE_NAME}', Meta returned '{returned_name or '?'}'.")

    write_env({
        "META_PAGE_ID_GAMING": page_id,
        "META_SYSTEM_USER_ACCESS_TOKEN_GAMING": system_token,
    })

    print("\nGAMING FACEBOOK PERMANENT CONNECTION SUCCESS")
    print(f"Page: {returned_name} ({page_id})")
    print("Saved as META_SYSTEM_USER_ACCESS_TOKEN_GAMING.")
    print("TV Mind and all existing Facebook credentials were left unchanged.")


if __name__ == "__main__":
    main()
