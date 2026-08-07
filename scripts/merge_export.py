#!/usr/bin/env python3
"""
Consolidate all incremental slackdump snapshots (data/raw/run-*/) into a
single deduped Slack-export-format directory (data/merged/), suitable for
feeding straight into slack-export-viewer's --html-only renderer.

Each CI run only exports messages since the last run, so the same message
can appear in multiple overlapping run-*/ snapshots — this dedups by
(channel, ts) and re-buckets messages into per-day files by their actual
UTC date, same as a native Slack export would.
"""
import json
import re
import shutil
import sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
MERGED_DIR = ROOT / "data" / "merged"
DAY_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def merge_users():
    """Merge users.json across snapshots, keyed by id. Later snapshots win on conflict."""
    users = {}
    for run_dir in sorted(RAW_DIR.glob("run-*")):
        for u in load_json(run_dir / "users.json", []):
            uid = u.get("id")
            if uid:
                users[uid] = u
    return list(users.values())


def merge_channels():
    """Merge channels.json across snapshots, keyed by id."""
    channels = {}
    for run_dir in sorted(RAW_DIR.glob("run-*")):
        for c in load_json(run_dir / "channels.json", []):
            cid = c.get("id")
            if cid:
                channels[cid] = c
    return list(channels.values())


def merge_channel_messages():
    """
    Walk every run-*/<channel>/*.json day file, dedup by (channel, ts), and
    re-bucket into day files keyed by the message's actual UTC date.
    Returns {channel_dir_name: {date_str: [messages...]}}.
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

    result = {}
    for channel, msgs_by_ts in by_channel.items():
        by_day = defaultdict(list)
        for ts, msg in msgs_by_ts.items():
            try:
                date_str = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                date_str = "unknown"
            by_day[date_str].append(msg)
        for date_str in by_day:
            by_day[date_str].sort(key=lambda m: float(m["ts"]))
        result[channel] = by_day
    return result


def merge_binary_assets():
    """
    Copy downloaded avatars (top-level __avatars/) and message attachments
    (<channel>/attachments/) across all snapshots into the merged dir.
    Same file ID always produces the same filename, so plain overwrite-copy
    is a safe dedup.
    """
    n_avatars = n_attachments = 0
    for run_dir in sorted(RAW_DIR.glob("run-*")):
        avatars_src = run_dir / "__avatars"
        if avatars_src.is_dir():
            dest = MERGED_DIR / "__avatars"
            dest.mkdir(parents=True, exist_ok=True)
            for f in avatars_src.iterdir():
                if f.is_file():
                    shutil.copy2(f, dest / f.name)
                    n_avatars += 1

        for channel_dir in run_dir.iterdir():
            if not channel_dir.is_dir() or channel_dir.name == "__avatars":
                continue
            attachments_src = channel_dir / "attachments"
            if attachments_src.is_dir():
                dest = MERGED_DIR / channel_dir.name / "attachments"
                dest.mkdir(parents=True, exist_ok=True)
                for f in attachments_src.iterdir():
                    if f.is_file():
                        shutil.copy2(f, dest / f.name)
                        n_attachments += 1
    return n_avatars, n_attachments


def main():
    if MERGED_DIR.exists():
        shutil.rmtree(MERGED_DIR)
    MERGED_DIR.mkdir(parents=True)

    users = merge_users()
    channels = merge_channels()
    channel_messages = merge_channel_messages()

    (MERGED_DIR / "users.json").write_text(json.dumps(users, ensure_ascii=False), encoding="utf-8")
    (MERGED_DIR / "channels.json").write_text(json.dumps(channels, ensure_ascii=False), encoding="utf-8")

    total = 0
    for channel, by_day in channel_messages.items():
        chan_dir = MERGED_DIR / channel
        chan_dir.mkdir(parents=True, exist_ok=True)
        for date_str, msgs in by_day.items():
            (chan_dir / f"{date_str}.json").write_text(
                json.dumps(msgs, ensure_ascii=False), encoding="utf-8"
            )
            total += len(msgs)

    n_avatars, n_attachments = merge_binary_assets()

    print(
        f"Merged {total} messages across {len(channel_messages)} channel(s), "
        f"{n_avatars} avatar(s), {n_attachments} attachment(s) into {MERGED_DIR}"
    )
    if not channel_messages:
        print("No messages found under data/raw/ — nothing to merge.", file=sys.stderr)


if __name__ == "__main__":
    main()
