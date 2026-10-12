---
name: mine
description: Mine dictation cases for recurring corrections and export the dictionary. Use to build or refresh data/cases/<case_id>/resolution.json files, mine corrections across cases, or export dictionary.md.
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

TODO: written later.

## 3. Dictionary export

TODO: written later.
