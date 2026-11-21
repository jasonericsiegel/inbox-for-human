---
name: inbox-for-human
description: 'Adds a plan, report, draft, or mockup you made to the user''s Inbox for Human, a list in Raycast they open with a hotkey, with what you need from them. Use whenever you ask the user to review, approve or pick from a report, plan, options, or a list, setting or rule you propose (mid-task too, even as one yes/no), they ask to see or review work ("show me what it looks like now", "let me review the plan", "let me review it", "open it for feedback", "show me the options so I can pick one"), you have three or more questions or decisions open for them, or you finish a document or visual deliverable for them. Count the open questions together even across messages; a choice among options counts as one, but a numbered list of proposed changes counts each item, even when asked as one yes/no ("make changes 1–6?", "reply with numbers, all, or none", a 📚 lesson list). Not for a yes/no with nothing to look over, brief chat answers or reference files; show unfinished work only on an explicit preview request.'
allowed-tools: Bash
metadata:
  version: 1.3.1
---

# inbox-for-human

Flags items for the user's review by writing to a JSON queue surfaced in the *Inbox for Human* Raycast extension.

## The inbox is a queue, not a folder

The inbox is the JSON queue surfaced in Raycast, reached **only** via the `enqueue.py` command below. There is **no `inbox/` directory** anywhere, and you must never create one.

When the user says "put it in my inbox" / "add to my inbox" / "save it to my inbox", that means: save the file where the project keeps that kind of work **and enqueue it here**. It never means writing to a folder named `inbox`.

Never resolve the word "inbox" to a filesystem path — not even if a `find` turns one up.

## How this skill fires

Three or more questions or decisions open for the user now also enter the review flow. Count them together before choosing which to ask, even across messages; a choice among options counts as one, but a numbered list of proposed changes counts each item, even when asked as one yes/no ("make changes 1–6?", "reply with numbers, all, or none", a 📚 lesson list). Prepare one page with one part per question, using `human-feedback` when installed, then queue it. Older ticket or skill wording asking for chat does not override this trigger; the user’s explicit request for chat does. One or two brief questions stay in chat unless another review trigger applies.

The description above is the auto-invocation matching surface. It covers review or choice requests, requests to see work such as “Show me what it looks like now”, and finished documents or visual deliverables made for the user, including printable checklists. Naming the inbox also loads it. When `human-feedback` is installed it fires alongside this skill: the same work gets both. Once loaded, three **modes** govern what the agent does: enqueue, announce, and show. Each mode has an entry trigger, a behavior while in it, and an exit condition. Check the mode at every subsequent action. An unfinished draft doesn't enter these modes just because it was saved.

## Enqueue mode

Before entering: when the work belongs on a notes page and `human-feedback` is in your available-skills list, load it and finish/check its held page first. Use an already prepared notes page instead of creating another. Enqueue that page, keeping the original document as its source; don't enqueue the source and then a second preview for the same handoff.

- **Enter:** you're asking the user to review, approve or choose from the file, they asked to see or review it or named their inbox, you finished a document or visual deliverable for them. Prepare and check the deliverable before entering; an unfinished save alone doesn't enter. A deliverable with a notes page → enqueue that page once it's ready. A new version of that page → enqueue it with `--supersedes` naming the last one.
- **Behavior while in mode:** no further user-facing text. No other tool calls except the enqueue command. Do not summarize, do not ask, do not continue work — the only valid next step is the enqueue.
- **Exit:** enqueue.py returns exit code 0.

If enqueue.py prints a line starting `Note:` (Raycast isn't installed, or this isn't a Mac), pass it on to the user word for word, after the announce line below. Pass each note on only once a session, even when the script prints it again.

Re-enqueueing is idempotent on path; do not skip because the path was queued earlier in the session.

```bash
python3 "$SKILL_DIR/scripts/enqueue.py" \
  --path "/absolute/path/to/file.md" \
  --ask "take a look and tell me if the tone works" \
  --context "draft caption for post 42"
```

`$SKILL_DIR` is the folder this SKILL.md was loaded from (same folder under Claude Code and Codex); spell the path out in the command.

`--session-id <id>` is optional; pass it when the session knows its id.

**Parameters:**
- `--ask` — what specifically should the user do? Plain English, like a text to a friend. Minimum 20 characters; stubs like "Review." or "FYI." are rejected.
- `--context` — what is this file in one short phrase. 250 characters max.
- `--supersedes <id-or-absolute-path>` — repeatable. This file replaces or obsoletes an earlier inbox item (a follow-up doc, a v2 plan, a redo at a new path) → name each earlier item; it leaves the inbox in the same step, archived as superseded. Decide this before entering Enqueue mode; to find an id, run `python3 "$SKILL_DIR/scripts/resolve.py" --list`. A re-enqueue of the same path needs no flag.

## Announce mode

- **Enter:** enqueue.py exits 0.
- **Behavior while in mode:** the literal next user-facing text in this turn must contain a line in this **exact** format, one line per file enqueued:

  ```
  🍪 **Added to Inbox for Human:** <absolute-path> — <one-line context, ≤80 chars> ([open](<obsidian-url>))
  ```

  Required elements, all literal: the 🍪 cookie, the `**Added to Inbox for Human:**` bolded prefix (with the colon inside the bold), the absolute path, a space + em-dash (`—`, U+2014) + space, and the context.

  **The trailing `([open](<obsidian-url>))` makes the file reachable in one click**, which a bare path in a terminal is not. Do not build that URL by hand: `enqueue.py` prints it as its own `Obsidian: obsidian://…` output line whenever the file sits inside a vault, already URL-encoded and already pointed at the right vault (a nested vault wins over the one above it). Copy that URL across verbatim. When the script prints no `Obsidian:` line the file is outside every vault, so omit the parenthetical entirely and leave the rest of the line exactly as it is — the link is additive, and the compliance form above stands with or without it. For an HTML page, use the link your project's instructions give for a local page (a preview server's localhost URL), else its `file://` URL: `([open](<url>))`. Never a bare URL, which wraps across lines and doesn't click.
  For each `Superseded:` line enqueue.py prints, add `🗄️ **Archived from Inbox:** <its context>` right after the cookie line.
- **Exit:** the line above is emitted.

This is not "include the cookie line somewhere in your response." It is "the cookie line is the first text the user sees after the enqueue runs." If you have already emitted user-facing text earlier in the turn, the cookie line is still the next text after enqueue.py succeeds.

Wrong order: enqueue → ask "what's next?" → user answers → announce 🍪.
Right order: enqueue → 🍪 announce → then continue.

## Show mode

- **Enter:** Announce mode exits **and** the deliverable is being surfaced to the user *now*: they asked to see it, you're asking for their review, or it is the answer to this turn's request. (A file merely queued for later stops at Announce mode; do not open it.)
- **Behavior while in mode:** show the finished work where the user can leave notes:
  - A document or visual deliverable made for the user, whether they asked to see it, asked for review, or haven't asked again → if `human-feedback` is in your available-skills list (its name, with or without a plugin prefix), load it. It builds a page for work that isn't one, preserves the source for use or printing, and opens the working preview. Interactive pages work too; served pages take `--copy`. A screenshot alone doesn't replace a working result.
  - An existing reference file the user only wants to read → its usual app, with no notes page.
  - No `human-feedback` → open the file in its usual app and say once: "Ran without the human-feedback skill, which would add a notes column beside the work. Get it at github.com/jasonericsiegel/human-feedback."
  - Any other extension → the default Mac app.
- **Exit:** the file is open. Don't wait or poll: their notes arrive when they paste them or tell you they've commented. Apply them then, carrying any wording they typed across verbatim.

### Deliverables go to their notes tools; everything else opens in the default Mac app

The notes route covers documents and visual deliverables made for the user, including plans, specs, briefs, reports, drafts, printable checklists, designs, and mockups. Reference notes and a file the user wants to file or link rather than inspect open in their usual app. When the user names the tool, that choice wins.

## When not to queue

Do not enqueue brief chat answers, unfinished work, existing reference files, or files the user wrote themselves this session. A finished document or visual deliverable made for them still queues when no one has asked for review, including when its preview is already open or its notes page opened already Done. Mention the path for excluded files; no special phrase is needed.

## Dedup

Path is the unique key. Re-enqueuing updates in place; re-enqueuing an archived path restores it.
