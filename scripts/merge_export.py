#!/usr/bin/env python3
"""
Consolidate all incremental slackdump snapshots (data/raw/run-*/) into a
single deduped Slack-export-format directory (data/merged/), suitable for
feeding straight into slack-export-viewer's --html-only renderer.

Each CI run only exports messages since the last run, so the same message
can appear in multiple overlapping run-*/ snapshots — this dedups by
(channel, ts) and re-buckets messages into per-day files by their actual
UTC date, same as a native Slack export would.

slack-export-viewer never looks for locally downloaded files — it always
renders a message's file "url_private" (and thumb_* fields) directly
(see slackviewer/message.py's LinkAttachment.link), which point at Slack's
auth-required CDN and are broken for anonymous site visitors. So for any
file we've downloaded a local copy of, this rewrites those URL fields to a
relative path under __attachments/, which the CI workflow copies into the
rendered public/ output afterwards.
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

# Every channel page renders at public/channel/<name>/index.html, i.e. a
# fixed depth of 2 below public/, so the relative path back up to a
# top-level __attachments/ dir is always the same.
ATTACHMENTS_REL_PREFIX = "../../__attachments/"


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


# Slack's file CDN occasionally serves its own HTML login/interstitial page
# instead of the actual file (looks like an expired/redirected signed URL) —
# with a 200 status and no error slackdump would catch, so it saves the HTML
# verbatim with the original filename/extension. Recognize and reject it.
POISON_SIGNATURES = (b"<!DOCTYPE", b"<!doctype", b"<html")


def is_poisoned(path):
    try:
        with open(path, "rb") as f:
            head = f.read(64).lstrip()
    except OSError:
        return True
    return any(head.startswith(sig) for sig in POISON_SIGNATURES)


def merge_attachments():
    """
    Copy every downloaded file attachment across all snapshots into a single
    flat pool (MERGED_DIR/__attachments/). slackdump names files
    <file-id>-<original-name>, which is already collision-safe, and puts
    them either under <channel>/attachments/ or (for files not tied to one
    channel) a top-level attachments/ dir — both get merged the same way.

    A poisoned re-download must never overwrite a previously-good copy, so
    once a filename has a good copy in the pool it's left alone; a poisoned
    source is skipped outright rather than counted, so a rewritten message
    URL never points at bad content.
    Returns the set of filenames with a valid copy in the pool.
    """
    dest = MERGED_DIR / "__attachments"
    good = set()
    skipped_poisoned = 0
    for run_dir in sorted(RAW_DIR.glob("run-*")):
        candidates = [run_dir / "attachments"]
        candidates += [d / "attachments" for d in run_dir.iterdir() if d.is_dir()]
        for attachments_src in candidates:
            if not attachments_src.is_dir():
                continue
            for f in attachments_src.iterdir():
                if not f.is_file():
                    continue
                if f.name in good:
                    continue  # already have a good copy, don't risk overwriting it
                if is_poisoned(f):
                    skipped_poisoned += 1
                    continue
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest / f.name)
                good.add(f.name)
    if skipped_poisoned:
        print(f"Skipped {skipped_poisoned} poisoned attachment download(s)", file=sys.stderr)
    return good


def rewrite_file_urls(msg, attachment_names):
    """
    For each file on this message that we have a local copy of, point
    url_private and any thumb_* fields at the local relative path instead
    of Slack's auth-required CDN URL.
    """
    for f in msg.get("files", []) or []:
        file_id = f.get("id")
        if not file_id:
            continue
        match = next((n for n in attachment_names if n.startswith(file_id + "-")), None)
        if not match:
            continue
        local_path = ATTACHMENTS_REL_PREFIX + match
        f["url_private"] = local_path
        f["url_private_download"] = local_path
        for key in list(f):
            if key.startswith("thumb_"):
                f[key] = local_path


def merge_channel_messages(attachment_names):
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
            if not channel_dir.is_dir() or channel_dir.name in ("attachments", "__avatars"):
                continue
            channel = channel_dir.name
            for day_file in channel_dir.glob("*.json"):
                if not DAY_FILE_RE.match(day_file.name):
                    continue
                for msg in load_json(day_file, []):
                    ts = msg.get("ts")
                    if not ts:
                        continue
                    rewrite_file_urls(msg, attachment_names)
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


def main():
    if MERGED_DIR.exists():
        shutil.rmtree(MERGED_DIR)
    MERGED_DIR.mkdir(parents=True)

    users = merge_users()
    channels = merge_channels()
    attachment_names = merge_attachments()
    channel_messages = merge_channel_messages(attachment_names)

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

    print(
        f"Merged {total} messages across {len(channel_messages)} channel(s), "
        f"{len(attachment_names)} attachment(s) into {MERGED_DIR}"
    )
    if not channel_messages:
        print("No messages found under data/raw/ — nothing to merge.", file=sys.stderr)


if __name__ == "__main__":
    main()
