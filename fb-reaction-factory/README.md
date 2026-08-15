# Facebook Reaction Reel Factory - MVP

Creates vertical reaction Reels with this layout:

- Top 30%: one of your reaction clips
- Bottom 70%: approved funny/source clip
- 1080x1920, H.264/AAC, max 60 seconds
- Optional AI title/description/hashtags
- Facebook Page Reels publishing through Meta Graph API

## Safety gate

Only process clips you own or have permission/license/rights to reuse. Every source job requires `--rights-ok`.

## 1. Add your reaction library

```bash
python3 reaction_factory.py add-reaction /path/laugh.mp4 --label laugh
python3 reaction_factory.py add-reaction /path/shock.mp4 --label shock
python3 reaction_factory.py add-reaction /path/smile.mp4 --label smile
```

Add 5-10 clips. Labels can be `laugh`, `shock`, `smile`, `cringe`, `confused`, etc.

## 2. Make one Reel now

Local source:

```bash
python3 reaction_factory.py make --source /path/funny.mp4 --caption "funny dog fail" --reaction auto --rights-ok
```

URL/Instagram source requires `yt-dlp` installed:

```bash
python3 -m pip install yt-dlp
python3 reaction_factory.py make --source "PASTE_URL" --caption "funny fail" --reaction auto --rights-ok
```

## 3. Queue a Reel

```bash
python3 reaction_factory.py queue --source "PASTE_URL" --caption "funny fail" --rights-ok --publish-at "2026-08-15T11:00:00"
python3 worker.py
```

Without Facebook credentials, the worker renders the Reel and creates metadata, then marks it `ready`.

## 4. AI metadata

Set `OPENAI_API_KEY` and optionally `OPENAI_MODEL`. Without a key, the system uses a simple fallback title/description/hashtags generator.

## 5. Facebook auto-publish

Copy `.env.example` values into your shell/environment:

- `META_PAGE_ID`
- `META_PAGE_ACCESS_TOKEN`
- `META_GRAPH_VERSION`

Then:

```bash
python3 worker.py --publish
```

For continuous queue processing on a machine/server:

```bash
python3 worker.py --publish --loop --interval 60
```

## Today MVP path

1. Add 5-10 reaction clips.
2. Test one approved source clip.
3. Confirm 30/70 edit looks right.
4. Connect Facebook Page credentials.
5. Queue 3-5 daily slots.
