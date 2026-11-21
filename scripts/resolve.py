#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""Resolve (dequeue) inbox-for-human entries that no longer need human attention.

Two jobs:

  --list                    Print every active queue entry as JSON (id, path, ask,
                            context, session_id, timestamp). Callers filter.
  --resolve ID [ID ...]     Remove those entries from the active queue and append
                            them to queue.archive.jsonl with a resolution stamp.

Removing an entry is how a finished or obsolete item stops showing up in the
Raycast drain. Nothing else in the queue is touched.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from enqueue import (  # noqa: E402
    append_archive_entries,
    get_archive_path,
    get_queue_path,
    load_queue,
    lock_queue,
    write_queue_atomic,
)

VALID_REASONS = ("done", "obsolete", "superseded")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print active entries as JSON")
    parser.add_argument("--resolve", nargs="+", metavar="ID", help="entry ids to dequeue")
    parser.add_argument(
        "--reason",
        default="done",
        choices=VALID_REASONS,
        help="why the entry is leaving the queue (default: done)",
    )
    args = parser.parse_args(argv)

    if not args.list and not args.resolve:
        parser.error("give --list or --resolve")

    queue_path = get_queue_path()

    if args.list:
        print(json.dumps(load_queue(queue_path), ensure_ascii=False, indent=2))
        return 0

    wanted = set(args.resolve)
    # Hold the queue's lock from the read to the write, so an entry another
    # agent adds meanwhile isn't lost.
    with lock_queue(queue_path):
        queue = load_queue(queue_path)
        resolved = [e for e in queue if isinstance(e, dict) and e.get("id") in wanted]
        if not resolved:
            print("No matching queue entries; nothing removed.", file=sys.stderr)
            return 0

        remaining = [e for e in queue if not (isinstance(e, dict) and e.get("id") in wanted)]
        append_archive_entries(get_archive_path(queue_path), resolved, args.reason)
        write_queue_atomic(queue_path, remaining)

    for entry in resolved:
        print(f"resolved ({args.reason}): {entry.get('id')} — {entry.get('ask', '')}")
    missing = wanted - {e.get("id") for e in resolved}
    for mid in sorted(missing):
        print(f"not found: {mid}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
