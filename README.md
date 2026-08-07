# olake-slack-archive

Automated archive of OLake's Slack community, working around the free plan's
90-day message retention limit.

## How it works

1. **Export** — [`slackdump`](https://github.com/rusq/slackdump) pulls new
   messages from Slack every 6 hours via GitHub Actions (`.github/workflows/archive.yml`),
   authenticated with a bot token (`SLACK_BOT_TOKEN` repo secret).
2. **Store** — raw export snapshots land in `data/raw/run-<timestamp>/` and get
   committed back to the repo. `data/.last_run` tracks the last successful
   export so each run only pulls new messages (1h overlap buffer to avoid gaps).
3. **Render** — `scripts/render.py` dedups messages across all snapshots (by
   channel + `ts`) and builds a static HTML site into `public/` — one page per
   channel plus an index.
4. **Search** — [Pagefind](https://pagefind.app/) builds a static full-text
   search index over `public/` at build time. No backend, pure client-side JS.
5. **Publish** — the built site deploys to GitHub Pages via
   `actions/deploy-pages`.

## Setup (one-time)

- Slack app `olake-slack-archive-bot` created in the OLake Open-Source
  Community workspace with bot scopes: `channels:history`, `channels:read`,
  `channels:join`, `groups:history`, `groups:read`, `users:read`.
- Bot token stored as the `SLACK_BOT_TOKEN` GitHub Actions secret on this repo.
- Bot needs to be a member of any channel it archives — it auto-joins public
  channels via `channels:join`; private channels need a manual `/invite`.
- GitHub Pages source set to "GitHub Actions" in repo settings.

## Local dev

```bash
# after running slackdump export manually into data/raw/run-<ts>/
python3 scripts/render.py
npx pagefind --site public
python3 -m http.server -d public 8000
```

## Notes

- Only public (and manually-invited private) channel history is archived —
  DMs and group DMs are intentionally excluded.
- Cron runs every 6h; adjust in `archive.yml` if message volume changes.
