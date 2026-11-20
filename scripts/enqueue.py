#!/usr/bin/env python3
"""Enqueue a file for human review via the Inbox for Human queue.

Writes to a JSON queue file (default ``~/.agents/inbox-for-human/queue.json``,
overridable via ``INBOX_QUEUE_PATH``). The path field is the unique key:
re-enqueueing the same path updates the existing entry in place; if the path
exists only in the archive, the entry is restored to the active queue with its
original ``id`` preserved.

Concurrency: holds an exclusive ``fcntl.flock`` on the queue file across the
read-modify-write-replace cycle. Atomic write uses a writer-unique tmp file
(``queue.json.{pid}.tmp``) replaced via ``os.replace``.

This script is part of the public ``inbox-for-human`` Claude Code plugin. The
companion reader is the Inbox for Human Raycast extension (macOS only).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_ASK_LEN = 20
GENERIC_ASK_RE = re.compile(r"^(Review|FYI|Continued)\.?\s*$", re.IGNORECASE)
MAX_CONTEXT_LEN = 250

DEFAULT_QUEUE_PATH = "~/.agents/inbox-for-human/queue.json"
FIRST_RUN_SENTINEL = "~/.agents/inbox-for-human/.suppressed-first-run-msg"

PLATFORM_WARNING = (
    "Note: Inbox for Human's list runs in Raycast, which is Mac-only. "
    "The file was still added."
)

FIRST_RUN_ADVISORY = (
    "Note: Inbox for Human shows your inbox in Raycast, which isn't on "
    "this Mac. Get Raycast at https://raycast.com/?via=jasoneric, then "
    "the Inbox for Human extension at "
    "https://www.raycast.com/jasoneric/inbox-for-human. "
    "(This note won't repeat.)"
)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_queue_path() -> Path:
    """Resolve the queue path from ``INBOX_QUEUE_PATH`` or default.

    Expands ``~`` in either case. Does not create the file.
    """
    raw = os.environ.get("INBOX_QUEUE_PATH", DEFAULT_QUEUE_PATH)
    return Path(os.path.expanduser(raw))


def get_archive_path(queue_path: Path) -> Path:
    """Return the sibling archive path (``queue.archive.jsonl``) for a queue file."""
    return queue_path.with_name("queue.archive.jsonl")


def is_strict() -> bool:
    """True unless ``INBOX_STRICT`` env var is the literal string ``0``."""
    return os.environ.get("INBOX_STRICT", "1") != "0"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_args(path: str, ask: str, context: str) -> str | None:
    """Validate args. Returns an error message string, or None on success.

    The ``path`` absolute-path check always runs (even if strict mode is off).
    The ``ask`` length and generic-stub checks and the ``context`` length check
    are skipped when ``INBOX_STRICT=0``.
    """
    if not os.path.isabs(path):
        return "use absolute paths"

    if not is_strict():
        return None

    if len(ask) < MIN_ASK_LEN:
        return "ask too short; describe what you want the human to decide"

    if GENERIC_ASK_RE.match(ask.strip()):
        return "ask is too generic; say what specifically needs review"

    if len(context) > MAX_CONTEXT_LEN:
        return "context too long; trim to a single sentence"

    return None


# ---------------------------------------------------------------------------
# Queue I/O
# ---------------------------------------------------------------------------


def load_queue(queue_path: Path) -> list[dict[str, Any]]:
    """Load queue entries.

    Returns ``[]`` if the file is missing. On ``JSONDecodeError``, renames the
    corrupted file to ``queue.json.bak.<timestamp>``, warns to stderr, and
    returns ``[]``.
    """
    if not queue_path.exists():
        return []

    raw = queue_path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = queue_path.with_name(f"{queue_path.name}.bak.{ts}")
        queue_path.rename(backup)
        print(
            f"Warning: queue file was corrupted; backed up to {backup} "
            "and starting fresh.",
            file=sys.stderr,
        )
        return []

    if not isinstance(data, list):
        # Defensive: queue must be a list. Treat anything else as corruption.
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = queue_path.with_name(f"{queue_path.name}.bak.{ts}")
        queue_path.rename(backup)
        print(
            f"Warning: queue file was not a JSON array; backed up to {backup} "
            "and starting fresh.",
            file=sys.stderr,
        )
        return []

    return data


def write_queue_atomic(queue_path: Path, items: list[dict[str, Any]]) -> None:
    """Atomically write the queue.

    Writes to a writer-unique tmp file (``queue.json.{pid}.tmp``) then
    ``os.replace`` to the final path.
    """
    tmp = queue_path.with_name(f"{queue_path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(items, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, queue_path)


def find_archived_entry(archive_path: Path, target_path: str) -> dict[str, Any] | None:
    """Scan the archive in reverse for the most recent full entry matching ``path``.

    Skips ``restored_at`` marker lines (single-key markers used to record that a
    previously-archived entry has since been restored to the active queue).
    Returns the parsed archive entry or ``None``.
    """
    if not archive_path.exists():
        return None

    try:
        text = archive_path.read_text(encoding="utf-8")
    except OSError:
        return None

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # Skip restored_at marker lines (they are short single-purpose markers
        # that reference an archived entry by id but do not themselves carry a
        # full entry payload).
        if "restored_at" in obj and "path" not in obj:
            continue
        if obj.get("path") == target_path:
            return obj

    return None


def append_archive_marker(archive_path: Path, entry_id: str) -> None:
    """Append a ``restored_at`` marker line to the archive file.

    The marker is a single-line JSON object: ``{"restored_at": "<iso>", "id": "<id>"}``.
    Append-only — never rewrites existing archive content. Uses ``O_APPEND`` for
    POSIX-atomic single-line append.
    """
    marker = {
        "restored_at": datetime.now().astimezone().isoformat(),
        "id": entry_id,
    }
    line = json.dumps(marker, ensure_ascii=False) + "\n"
    # O_APPEND ensures the write is atomic for sub-PIPE_BUF lengths even with
    # concurrent writers.
    fd = os.open(
        archive_path,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT,
        0o644,
    )
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def append_archive_entries(
    archive_path: Path,
    entries: list[dict[str, Any]],
    reason: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append resolved entries to the archive, one JSON object per line.

    Each line is the entry plus ``resolved_at``, ``resolved_reason``, and any
    ``extra`` fields. Append-only, like :func:`append_archive_marker`.
    """
    stamp = datetime.now().astimezone().isoformat()
    lines = []
    for entry in entries:
        record = dict(entry)
        record["resolved_at"] = stamp
        record["resolved_reason"] = reason
        if extra:
            record.update(extra)
        lines.append(json.dumps(record, ensure_ascii=False))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    fd = os.open(archive_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Core enqueue logic
# ---------------------------------------------------------------------------


def upsert_entry(
    queue_path: Path,
    archive_path: Path,
    path: str,
    ask: str,
    context: str,
    session_id: str | None,
    obsidian_url: str | None = None,
    supersedes: list[str] | None = None,
) -> dict[str, Any]:
    """Insert / update / restore an entry. Performs the locked I/O dance.

    Resolution order on ``path``:

    1. Already in active queue → update in place; preserve ``id``; refresh
       ``ask``/``context``/``timestamp``/``session_id``; clear ``opened_at``.
    2. Otherwise, in archive → restore: pull the archived entry's ``id``,
       append a ``restored_at`` marker line to the archive, insert into active.
    3. Otherwise → fresh ``id`` (8-char uuid4 hex), insert.

    ``supersedes`` lists ids or paths of active entries this one replaces.
    They leave the queue and go to the archive with ``resolved_reason``
    ``superseded`` and ``superseded_by`` set to this entry's id. A ref equal to
    ``path`` is ignored (that is a plain update).

    Returns the new/updated entry, plus two result-only keys not written to the
    queue: ``superseded`` (archived entries) and ``unmatched_supersedes`` (refs
    with no active match). Active queue is sorted by ``timestamp`` descending
    after the mutation.
    """
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    # Touch so we always have a file to lock.
    if not queue_path.exists():
        queue_path.write_text("[]\n", encoding="utf-8")

    timestamp = datetime.now().astimezone().isoformat()

    # Acquire exclusive lock for the whole read-modify-write-replace.
    with open(queue_path, "r+", encoding="utf-8") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            items = load_queue(queue_path)

            existing = next(
                (i for i in items if i.get("path") == path),
                None,
            )

            if existing is not None:
                entry_id = existing["id"]
                items.remove(existing)
            else:
                archived = find_archived_entry(archive_path, path)
                if archived is not None:
                    entry_id = archived.get("id") or uuid.uuid4().hex[:8]
                    append_archive_marker(archive_path, entry_id)
                else:
                    entry_id = uuid.uuid4().hex[:8]

            entry: dict[str, Any] = {
                "id": entry_id,
                "path": path,
                "ask": ask,
                "context": context,
                "timestamp": timestamp,
            }
            if session_id:
                entry["session_id"] = session_id
            if obsidian_url:
                entry["obsidian_url"] = obsidian_url

            refs = [r for r in (supersedes or []) if r != path]
            superseded = [
                i for i in items if i.get("id") in refs or i.get("path") in refs
            ]
            matched = {i.get("id") for i in superseded} | {
                i.get("path") for i in superseded
            }
            if superseded:
                append_archive_entries(
                    archive_path,
                    superseded,
                    "superseded",
                    {"superseded_by": entry_id},
                )
                items = [i for i in items if i not in superseded]

            items.append(entry)
            items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

            write_queue_atomic(queue_path, items)
            return {
                **entry,
                "superseded": superseded,
                "unmatched_supersedes": [r for r in refs if r not in matched],
            }
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Advisories
# ---------------------------------------------------------------------------


def maybe_emit_platform_warning() -> None:
    """Emit a non-fatal stderr warning on non-Darwin platforms."""
    if sys.platform != "darwin":
        print(PLATFORM_WARNING, file=sys.stderr)


def raycast_app_present() -> bool:
    """True if any Raycast flavor (release or beta) is installed.

    Raycast ships two parallel macOS apps: ``Raycast.app`` (stable, bundle
    ``com.raycast.macos``) and ``Raycast Beta.app`` (bundle
    ``com.raycast-x.macos``). The Raycast Store serves extensions to both, so
    either is sufficient for the companion reader to work.
    """
    candidates = (
        "~/Applications/Raycast.app",
        "/Applications/Raycast.app",
        "~/Applications/Raycast Beta.app",
        "/Applications/Raycast Beta.app",
    )
    return any(Path(os.path.expanduser(p)).exists() for p in candidates)


def maybe_emit_first_run_advisory() -> None:
    """Emit a one-time advisory if Raycast.app is not present.

    Suppressed by a sentinel file at
    ``~/.agents/inbox-for-human/.suppressed-first-run-msg``. Sentinel write
    failures are non-fatal (e.g. unwritable home dir): the advisory simply may
    appear again on a subsequent run.
    """
    sentinel = Path(os.path.expanduser(FIRST_RUN_SENTINEL))
    if sentinel.exists():
        return
    if raycast_app_present():
        return

    print(FIRST_RUN_ADVISORY, file=sys.stderr)

    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
    except OSError:
        # Best-effort: don't blow up if the home dir is unwritable.
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def find_vault_root(path: str) -> tuple[Path, str] | None:
    """The nearest vault ancestor of ``path``, plus the vault's name.

    A vault is auto-detected by the ``.obsidian`` config directory Obsidian
    itself creates - not by a git root, which is a different thing that
    merely coincides in some setups. The nearest matching ancestor wins, so a
    nested vault beats the one above it.

    Returns ``(root, vault_name)`` or ``None`` when the file is not inside a
    vault, which is the normal case for most users and the reason the caller
    treats the URL as optional rather than required.
    """
    try:
        current = Path(path).expanduser().resolve()
    except OSError:
        return None
    for ancestor in current.parents:
        if (ancestor / ".obsidian").is_dir():
            return ancestor, ancestor.name
    return None


def obsidian_url(path: str) -> str | None:
    """An ``obsidian://open`` URL for ``path``, or ``None`` if it is not in
    a vault.

    The vault name defaults to the vault directory's own basename, which is
    what Obsidian registers a vault under by default.
    """
    found = find_vault_root(path)
    if found is None:
        return None
    root, name = found
    try:
        relative = Path(path).expanduser().resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    return (
        "obsidian://open?vault="
        + quote(name, safe="")
        + "&file="
        + quote(str(relative), safe="")
        + "&paneType=tab"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        description="Enqueue a file for human review.",
    )
    parser.add_argument("--path", required=True, help="Absolute path to the file")
    parser.add_argument(
        "--ask",
        required=True,
        help="What you want the human to do (plain English)",
    )
    parser.add_argument(
        "--context",
        required=True,
        help="Brief context for the entry (plain English, ≤ 250 chars)",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help=(
            "Claude Code session id to record on the entry. "
            "Falls back to the CLAUDE_CODE_SESSION_ID environment variable "
            "when omitted (set automatically by Claude Code)."
        ),
    )
    parser.add_argument(
        "--supersedes",
        action="append",
        default=None,
        metavar="ID_OR_PATH",
        help=(
            "Id or absolute path of an active entry this one replaces; it is "
            "archived as superseded. Repeatable."
        ),
    )
    args = parser.parse_args(argv)

    err = validate_args(args.path, args.ask, args.context)
    if err is not None:
        print(err, file=sys.stderr)
        return 1

    session_id = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID") or None

    queue_path = get_queue_path()
    archive_path = get_archive_path(queue_path)
    url = obsidian_url(args.path)

    try:
        entry = upsert_entry(
            queue_path=queue_path,
            archive_path=archive_path,
            path=args.path,
            ask=args.ask,
            context=args.context,
            session_id=session_id,
            obsidian_url=url,
            supersedes=args.supersedes,
        )
    except OSError as e:
        # Common cause: parent dir cannot be created (e.g. permission denied).
        if e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            print(f"cannot write queue file: {e}", file=sys.stderr)
            return 1
        raise

    print(f"Queued: {entry['context']} — {entry['ask']}")
    for old in entry["superseded"]:
        print(f"Superseded: {old.get('context', '')} ({old.get('id')})")
    for ref in entry["unmatched_supersedes"]:
        print(f"Warning: not in active queue: {ref}", file=sys.stderr)

    if url is not None:
        print(f"Obsidian: {url}")

    # Out-of-lock advisories (non-fatal).
    maybe_emit_first_run_advisory()
    maybe_emit_platform_warning()

    return 0


if __name__ == "__main__":
    sys.exit(main())
