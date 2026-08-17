#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cloud_sync import load_env, health, push_reactions, push_state

ROOT = Path(__file__).resolve().parent
REPO = "1droxion/fb-reaction-factoryr"
WORKFLOW = "Reaction Factory Cloud"


def require(command):
    path = shutil.which(command)
    if not path:
        raise RuntimeError(f"{command} is not installed in this Codespace.")
    return path


def set_secret(name, value):
    if not value:
        return False
    gh = require("gh")
    proc = subprocess.run(
        [gh, "secret", "set", name, "--repo", REPO],
        input=value,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "GitHub secret command failed").strip()
        raise RuntimeError(detail)
    print(f"GitHub secret saved securely: {name}")
    return True


def trigger_test_run():
    gh = require("gh")
    proc = subprocess.run(
        [gh, "workflow", "run", WORKFLOW, "--repo", REPO],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Could not start workflow").strip()
        raise RuntimeError(detail)
    print("Cloud workflow test started.")


def main():
    load_env()
    page_token = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
    if not page_token:
        raise RuntimeError("META_PAGE_ACCESS_TOKEN is missing. Run python3 meta_connect.py first.")

    print("Checking private Reaction Factory cloud storage...")
    health()

    print("\nUploading private reaction clips...")
    push_reactions()

    print("\nUploading AutoPilot state/config...")
    push_state()

    print("\nSaving Meta credential as an encrypted GitHub Actions secret...")
    set_secret("META_PAGE_ACCESS_TOKEN", page_token)

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key:
        set_secret("OPENAI_API_KEY", openai_key)

    print("\nStarting one cloud test run...")
    trigger_test_run()

    print("\nCLOUD AUTOPILOT SETUP SUCCESS")
    print("Your laptop and Codespace may be closed after the cloud workflow test succeeds.")
    print("GitHub will wake the worker hourly; the worker only posts when the saved 5-hour window is due.")
    print("If Meta later expires/revokes the access token, refresh it and run this setup again to update the GitHub secret.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"CLOUD SETUP ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
