---
name: mine
description: Mine dictation cases and Codex session logs for recurring corrections and cleaning feedback, and export the dictionary. Use to build or refresh data/cases/<case_id>/resolution.json files, run a mining pass over new sessions, or export dictionary.md.
---

# mine

Steps run in order. Paths are relative to the repository root.

## 1. Resolutions

Goal: for each case, record which of the cleaner's suspect phrases the user confirmed or rejected, as
`data/cases/<case_id>/resolution.json`. Inputs are that case's `exchange.txt` and `feedback.txt`; read nothing else for
the resolution. `data/manifest.jsonl` lists the cases (`has_feedback`, `suspect_rounds`).

Schema:

{"corrections": [{"heard": "...", "meant": "...", "kind": "mishearing|term"}], "accepted": ["..."]}

- `corrections`: a phrase the cleaner flagged (or the user quoted) and the user said was wrong. `kind` is `mishearing`
  when the speech recognizer produced an ordinary wrong word or phrase ("liver" for "level"), and `term` when it garbled
  a name, tool, acronym or other specialised word.
- `accepted`: flagged phrases the user left as transcribed.

`exchange.txt` is blocks separated by `--- assistant ---` (a numbered suspect-transcription list, flagged phrase in
bold) and `--- user ---` (the free-text reply), in order. Read each reply against the list directly before it:

- "3 is X": item 3's flagged phrase is `heard`, X is `meant`.
- "none", "all fine", "leave it": every flagged phrase in that list goes to `accepted`.
- "all fine except 2, which is X": item 2 is a correction, the other items go to `accepted`.
- A corrected sentence or phrase quoted back: diff it against the flagged text; each changed span is a correction. If
  the reply corrects a phrase the list did not flag (a repeat of the same mishearing), include it too.
- An item the reply does not mention while it addresses others stays out of both lists.

`feedback.txt` holds the user's turns after a clean (`--- user ---` blocks). Take only term-level statements from it
("X should be Y", "X is a real word"). Style complaints ("keep my wording") give no entry.

Rules:

- `heard` is copied from the list exactly, without the bold markers. It is matched later by containment after
  lowercasing and stripping punctuation, so keep the words as listed and do not shorten, expand or retype them.
- `meant` is the user's wording, as written.
- Only what the user confirmed goes in. Never add your own guesses, your own reading of an unflagged phrase, or a
  correction the user did not state or confirm.
- A case with an exchange or feedback gets a file, with empty lists where nothing was resolved. A case with neither gets
  no file.
- Write with 2-space indented JSON and a trailing newline.

## 2. Mining

Goal: collect mishearings the user confirmed and feedback on how the cleaner cleaned, from the Codex session logs,
into `data/mining/state.json`. Workers read the logs; this session only plans batches, dispatches and merges, and
never reads a rollout itself.

Cadence: run steps 2 and 3 about every ten new dictation sessions. The cleaner is stateless and cannot count them, so
whenever this skill is loaded, count the manifest rollouts newer than the watermark and say so if there are ten or more.

### Setup

- Start Claude Code from the repository root: the `miner` agent (`.claude/agents/miner.md`) registers only there.
- Refresh the manifest first with `uv run python src/extract.py`.
- Rollouts live at `$CODEX_HOME/sessions/YYYY/MM/DD/<rollout>` (default `CODEX_HOME` is `~/.codex`), the date taken
  from the file name. That folder is outside the repository; add it as a working directory (`/add-dir`, or
  `--add-dir` for a headless run) so miners can read it.

### Queue

`data/manifest.jsonl` has one row per dictation (case) with its `rollout` file name and `started` time. One rollout
is one session. Group rows by `rollout`, skip rollouts whose rows are all `missing_log`, and sort by file name, which
sorts by time.

- No watermark yet: the first pass takes the ten most recent sessions.
- Otherwise: every session with a case `started` after the watermark's `through`. A session at or before the
  watermark's `rollout` (one that continued after the last pass) gets `through` as its start timestamp.
- Older sessions are read only on request, as a backfill, or by a later pass checking that something happened before.
  A backfill never moves the watermark.

### Batches and dispatch

A batch is at most 5 sessions and at most 30 manifest cases; a session with more than 30 cases is a batch alone. Batch
ids are `<YYYY-MM-DD>-<n>`, the pass date and a number from 1. Each batch writes `data/mining/evidence/<batch id>.json`.

Dispatch with the Agent tool, `subagent_type: miner`, up to four batches in parallel in one message, and wait for
them. The brief holds only:

    Batch: <batch id>
    Evidence: <absolute path of the evidence file>
    Files:
    <absolute rollout path>[ after <ISO timestamp>]
    ...

Each miner returns a short summary. Do not read the logs to check it; read its evidence file.

### Merge

For each evidence file not yet listed in the state's `merged`:

1. Check it parses, `files` equals the brief, and `processed` plus `unprocessed` cover `files`. Redispatch
   unprocessed files once as a new batch, and likewise a file the summary admits was only partly read; a file that
   fails twice is reported to the user and left unread. Drop entries that break the miner's rules (feedback taken
   from a dictation's subject rather than a reply about a clean) and name them in the report.
2. Each correction updates the pair keyed by `heard` (lowercased, punctuation stripped) and `meant`
   (case-insensitive), creating it as `pending` with `source: "mined"` if new. A `term` correction also updates the
   term keyed by `meant`; give a new term a `group` (an existing group where one fits).
3. Updating an entry adds the dictation key `<rollout>:<dictation line>` to `dictations` if absent, the session to
   `sessions` and the date to `dates`, all distinct and sorted; `count` is the length of `dictations`. The `confirm`
   quote goes to the entry's `evidence` list with its key. Counts are of distinct dictations, never of mentions.
4. Adoption: a pending entry becomes `adopted` once `count` is at least 2 across at least 2 sessions, or at once when
   `proper_name` is true. Never demote an adopted or seed entry, and never delete an entry. Set `rejected`, with a
   `note`, only when the user rejects it or the evidence shows a style edit or changed intent rather than a
   mishearing; rejected entries keep collecting evidence.
5. Each feedback item joins the feedback entry whose `rule` means the same, or starts a new one with `status:
   "pending"`; counts as in 3. An entry seen in at least 2 sessions becomes `proposed`.
6. Append the file name to `merged`.

Then set the watermark to the newest session for which it and every earlier queued session were processed:
`{"rollout": <file name>, "through": <latest started among its manifest cases>}`. Write the state as 2-space indented
JSON with a trailing newline.

State schema:

    {
      "watermark": {"rollout": "...", "through": "..."} | null,
      "merged": ["<batch id>.json"],
      "terms": [{"term": "...", "group": "...", "status": "adopted|pending|rejected", "source": "seed|mined",
                 "proper_name": false, "count": 0, "dictations": [], "sessions": [], "dates": [], "evidence": [],
                 "note": "..."}],
      "pairs": [{"heard": "...", "meant": "...", "kind": "mishearing|term", "status": "...", "source": "...",
                 "proper_name": false, "count": 0, "dictations": [], "sessions": [], "dates": [], "evidence": [],
                 "note": "..."}],
      "feedback": [{"rule": "...", "status": "pending|proposed|approved|rejected", "count": 0, "dictations": [],
                    "sessions": [], "dates": [], "evidence": []}]
    }

`note` is optional. A pair's `note` is the condition printed beside it in the export ("when the tool is clearly
meant"); a term's `note` stays in the state.

### Proposals

Rewrite `data/mining/proposals.md` from the `proposed` and `approved` feedback entries: per entry, the rule, its
counts of dictations and sessions, its dates, the evidence quotes, and a suggested generic `cleaner/AGENTS.md` change
that names no case content. Never edit `cleaner/AGENTS.md` from this skill; an approved proposal goes through the
prompt-change loop in the bench skill. The user sets `approved` or `rejected`.

Report to the user: sessions read and the watermark, candidates found, entries newly adopted, entries pending, new
proposals, and anything unprocessed.

## 3. Dictionary export

Regenerate `cleaner/dictionary.md` whole from the state, never by hand-editing it:

    # Dictionary

    Terms are candidates for recovering a misheard word, never words to insert. Mishearings are past errors, heard -> meant.

    ## Terms

    <Group>:
    - <term>

    ## Mishearings

    - <heard> -> <meant>[ (<note>)]

- Adopted entries only; pending and rejected ones stay in the state.
- Groups in order of first appearance in the state, terms and pairs in state order, a blank line between groups.
- Limits: at most 50 terms and 20 pairs. These are a starting point to test, not a measured optimum. Seed entries
  always export; mined entries fill the rest by `count`, then by latest date. Name any adopted entry left out in the
  report.

The dictionary's hash names the dictionary state in a results row, so a regenerated file that differs starts a new
dictionary state.
