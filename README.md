# Voice prompt cleaner

A prompt for Codex Desktop that turns a dictated speech-to-text transcript into the prompt you meant, and a benchmark
that measures how well it does that on your own past dictations.

## Why

Talking is faster than typing, and a long, careful prompt to a coding agent is easier to say than to write. Codex
Desktop has the best speech-to-text the author has found; it caps a single recording at ten minutes. A raw transcript
is still full of speech noise: fillers, false starts, repeated words and the odd misheard term.

The cleaner is one file, `AGENTS.md`. Opened as a Codex Desktop project, it makes every dictation come back as a clean
prompt. Speech noise is removed; every instruction, hedge, question and afterthought is kept. It does not answer the
prompt, carry it out or improve it, and a "maybe" stays a maybe.

## Using the cleaner

The cleaner lives in its own repository and is a git submodule at `cleaner/`. Clone both together:

```
git clone --recurse-submodules <tool repository URL>
```

After a plain clone, run `git submodule update --init` instead. If you only want the cleaner, clone the cleaner
repository alone.

1. In Codex Desktop, open the `cleaner/` folder as a project. Open that folder itself, not this repository's root: the
   cleaner's own `.git` is what makes Codex load only its `AGENTS.md`.
2. Start a session and dictate your prompt.
3. Codex replies with the cleaned prompt in a text block. Copy it and paste it into whichever agent you use.

When the cleaner suspects a word was misheard, for example a library name that came through as two ordinary words, it
asks before cleaning, listing the phrases it suspects. Reply with the right word, or say the
transcript was correct, and it then returns the cleaned block with your answers applied.

## Your private dictionary

You can add `cleaner/dictionary.md`, a list of the names and terms you use and the mishearings you keep getting, with
what each should be. The cleaner reads it on every dictation. The cleaner repository ignores the file, so it never
leaves your machine. You can write it by hand, or let the benchmark's mining step build it from your own corrections
(below).

## Benchmarking the cleaner on your own dictations

Once you have dictated for a while, your Codex session logs hold real cases: what you said, what the cleaner returned
and any corrections you made. The benchmark turns those into a fixed test set, runs a given version of `AGENTS.md`
under a given model and effort level over it, and has a judge score the outputs, so that a prompt change or a new
model is measured instead of guessed. It needs Claude Code for the judging and mining, and uv for the Python scripts.
Run everything from this repository's root.

1. Copy `bench.example.toml` to `bench.toml` and list the folder you opened in Codex Desktop as the cleaner, so only
   those sessions are read. Optional dates narrow the range.
2. Extract the cases from your logs into `data/`:

   ```
   uv run python src/extract.py
   ```

3. Run the cleaner over a case set and judge the outputs (coming: `src/run.py`, `src/judge.py` and the `bench` skill).
   The sets are quick (10 cases) and full (30), with one output per case and no repeats.
4. Read the scores in `outputs/results.md` (coming), one row per prompt version, model, effort level and dictionary
   state.
5. Mine a dictionary: in Claude Code at this root, ask for the `mine` skill. It reads your correction exchanges, finds
   mishearings that recur, and writes `cleaner/dictionary.md`.

## What stays private

Anything holding your words stays on your machine. `data/` (cases, runs, judge replies, mining state) and `bench.toml`
are ignored by this repository, and `dictionary.md` by the cleaner's. What is tracked is the method, the code and
aggregate scores. Nothing from a dictation, correction or dictionary goes into a tracked file, examples are invented,
and `uv run python src/check.py --all` is a one-off scan to run before a repository gets a remote; it checks every
tracked file against your own list of private terms in `data/private-terms.txt`, one per line.
