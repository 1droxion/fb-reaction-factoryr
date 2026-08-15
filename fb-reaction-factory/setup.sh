#!/usr/bin/env bash
set -e
python3 -m pip install -r requirements.txt
command -v ffmpeg >/dev/null || { echo "ffmpeg is required"; exit 1; }
echo "Ready. Add your reaction clips next."
