#!/usr/bin/env python3
"""
Render all slackdump raw export snapshots (data/raw/run-*/) into a static
HTML site (public/) — one page per channel, deduped by message ts across
snapshots. Pagefind indexes the output separately in CI.

Tolerant of the standard Slack export directory layout:
  data/raw/run-<ts>/
    users.json          [{"id": "...", "name": "...", "profile": {"real_name": "..."}}, ...]
    channels.json        [{"id": "...", "name": "..."}, ...]
    <channel-name>/
      2026-01-01.json    [{"ts": "...", "user": "...", "text": "...", "thread_ts": "..."}, ...]
"""
import json
import re
import html
import sys
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "public"
DAY_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def collect_users():
    """Merge users.json across all snapshots -> {user_id: display_name}."""
    users = {}
    for run_dir in sorted(RAW_DIR.glob("run-*")):
        for u in load_json(run_dir / "users.json", []):
            uid = u.get("id")
            if not uid:
                continue
            profile = u.get("profile") or {}
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or u.get("real_name")
                or u.get("name")
                or uid
            )
            users[uid] = name
    return users


def collect_channels():
    """Merge channels.json across all snapshots -> {channel_dir_name: display_name}."""
    channels = {}
    for run_dir in sorted(RAW_DIR.glob("run-*")):
        for c in load_json(run_dir / "channels.json", []):
            name = c.get("name")
            if name:
                channels[name] = name
    return channels


def collect_messages():
    """
    Walk every run-*/<channel>/*.json day file across all snapshots and
    dedup messages by (channel, ts) — the same message may appear in
    multiple overlapping snapshots.
    Returns {channel_name: [message, ...]} sorted by ts.
    """
    by_channel = defaultdict(dict)  # channel -> {ts: message}
    if not RAW_DIR.exists():
        return {}
    for run_dir in sorted(RAW_DIR.glob("run-*")):
        if not run_dir.is_dir():
            continue
        for channel_dir in run_dir.iterdir():
            if not channel_dir.is_dir():
                continue
            channel = channel_dir.name
            for day_file in channel_dir.glob("*.json"):
                if not DAY_FILE_RE.match(day_file.name):
                    continue
                for msg in load_json(day_file, []):
                    ts = msg.get("ts")
                    if not ts:
                        continue
                    by_channel[channel][ts] = msg
    return {
        ch: sorted(msgs.values(), key=lambda m: float(m["ts"]))
        for ch, msgs in by_channel.items()
    }


def fmt_time(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (ValueError, TypeError):
        return ts


PAGE_TMPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>#{channel} — OLake Slack Archive</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
  a {{ color: #2563eb; }}
  .msg {{ padding: .6rem 0; border-bottom: 1px solid rgba(128,128,128,.2); }}
  .meta {{ font-size: .8rem; opacity: .65; margin-bottom: .15rem; }}
  .user {{ font-weight: 600; }}
  .text {{ white-space: pre-wrap; word-wrap: break-word; }}
  nav {{ margin-bottom: 1.5rem; font-size: .9rem; }}
</style>
</head>
<body>
<nav><a href="./index.html">&larr; all channels</a></nav>
<h1>#{channel}</h1>
<p>{count} messages archived</p>
{messages}
</body>
</html>
"""

MSG_TMPL = """<div class="msg">
  <div class="meta"><span class="user">{user}</span> &middot; {time}</div>
  <div class="text">{text}</div>
</div>
"""

INDEX_TMPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OLake Slack Archive</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
  a {{ color: #2563eb; }}
  li {{ margin: .3rem 0; }}
</style>
</head>
<body>
<h1>OLake Slack Archive</h1>
<p>Searchable archive of public channel history. Updated automatically.</p>
<ul>
{items}
</ul>
</body>
</html>
"""


def main():
    users = collect_users()
    channel_messages = collect_messages()

    if not channel_messages:
        print("No messages found under data/raw/ — nothing to render.", file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    index_items = []
    for channel, messages in sorted(channel_messages.items()):
        rendered = []
        for m in messages:
            user_id = m.get("user", "")
            user_name = users.get(user_id, user_id or "unknown")
            text = html.escape(m.get("text", ""))
            rendered.append(
                MSG_TMPL.format(
                    user=html.escape(user_name),
                    time=fmt_time(m.get("ts", "")),
                    text=text,
                )
            )
        page = PAGE_TMPL.format(
            channel=html.escape(channel),
            count=len(messages),
            messages="\n".join(rendered) if rendered else "<p>No messages.</p>",
        )
        (OUT_DIR / f"{channel}.html").write_text(page, encoding="utf-8")
        index_items.append(
            f'<li><a href="./{channel}.html">#{html.escape(channel)}</a> — {len(messages)} messages</li>'
        )

    (OUT_DIR / "index.html").write_text(
        INDEX_TMPL.format(items="\n".join(index_items) or "<li>No channels archived yet.</li>"),
        encoding="utf-8",
    )
    print(f"Rendered {len(channel_messages)} channel(s) to {OUT_DIR}")


if __name__ == "__main__":
    main()
