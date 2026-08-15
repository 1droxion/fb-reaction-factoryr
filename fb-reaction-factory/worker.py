#!/usr/bin/env python3
import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

from facebook import publish_reel
from metadata import generate_metadata
from reaction_factory import QUEUE_FILE, load_json, save_json, make_reel


def due(job):
    if job.get("status") != "queued":
        return False
    when = job.get("publish_at")
    if not when:
        return True
    try:
        target = datetime.fromisoformat(when)
        now = datetime.now(target.tzinfo) if target.tzinfo else datetime.now()
        return target <= now
    except Exception:
        return True


def process_one(publish=False):
    jobs = load_json(QUEUE_FILE, [])
    for job in jobs:
        if not due(job):
            continue
        try:
            job["status"] = "processing"
            save_json(QUEUE_FILE, jobs)
            out, reaction = make_reel(
                job["source"], job.get("caption", ""), job.get("reaction", "auto"), True
            )
            metadata = generate_metadata(job.get("caption", ""))
            job["output"] = str(out)
            job["metadata"] = metadata
            job["reaction_used"] = reaction
            if publish:
                desc = metadata.get("description", "")
                hashtags = " ".join(metadata.get("hashtags", []))
                title = metadata.get("title", "")
                result = publish_reel(out, f"{title}\n\n{desc}\n\n{hashtags}".strip())
                job["facebook"] = result
                job["status"] = "published"
            else:
                job["status"] = "ready"
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
        save_json(QUEUE_FILE, jobs)
        return job
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish", action="store_true", help="Publish ready job to Facebook")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()
    if args.loop:
        while True:
            job = process_one(args.publish)
            if job:
                print(json.dumps(job, indent=2))
            time.sleep(args.interval)
    else:
        job = process_one(args.publish)
        print(json.dumps(job, indent=2) if job else "No due queued job.")


if __name__ == "__main__":
    main()
