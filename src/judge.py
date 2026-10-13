"""Blind judge batches, reply validation, scoring and the public results table.

Usage:
  uv run python src/judge.py prepare --run RUN_ID [--method sentence|direct|chunked]
      Write blind batch files to data/runs/RUN_ID/judge/batch-NN.md and the item key to judge/key.json. The key
      records the method, the judge model and version, and a hash of every item as prepared. Cases already in the
      key are skipped, so preparing again after the run gained cases batches only the new ones (items and batch
      files continue the numbering).
      The bench skill hands each batch file's text, and nothing else, to a fresh agent of the method: `judge`
      (sentence), `judge-direct` or `judge-chunked`.
  uv run python src/judge.py score --run RUN_ID [--method sentence|direct|chunked]
      Refuse if the method differs from prepare's, a judged case is not prepared, or the judge or any item's inputs
      changed since prepare.
      Validate every reply. If any is missing or invalid, list them and write judge/retry-NN.md, one per fresh judge
      (exit 1). Once all are valid, score the run into judge/summary.json (per case and per length band; the top
      level covers all judged cases, `quick` the first 10 of data/sets/cases.txt), replace
      the run's lines in the private <data>/scores.jsonl (one line per case), print the per-case kept deltas against
      the comparison row when one exists, and regenerate outputs/results.md from every judged run (a quick row
      and, when the run has more cases, a full row).

Flags:
  --run RUN_ID  run folder name under <data>/runs/ (<prompt>_<model>_<effort>)
  --method M    judging method (default sentence): sentence answers every source and output sentence; direct
                tallies only the faults of a dictation; chunked gives that tally per transcript part. A method other
                than sentence prefixes the judge version, so its rows never compare with another method's.
  --data DIR    data root holding cases/, sets/ and runs/ (default: the tool's data/); fixture runs use it to stay
                out of the private data. outputs/results.md is always the tool's table.
"""

import argparse
import json
import math
import random
import re
import secrets
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

from config import ROOT
from extract import band
from run import DEFAULT_N, RECORD_FIELDS, RunError, load_cases, sha256

DATA = ROOT / "data"
RESULTS = ROOT / "outputs" / "results.md"
CLEANER = ROOT / "cleaner"
AGENTS = {
    "sentence": ".claude/agents/judge.md",
    "direct": ".claude/agents/judge-direct.md",
    "chunked": ".claude/agents/judge-chunked.md",
}
JUDGE_CODE = "src/judge.py"
REPLIES = "_replies"

MAX_ITEMS = 5
MAX_TOKENS = 80_000
TOKENS_PER_ENTRY = 110
REPLY_TOKENS = {"direct": 400, "chunked": 600}
TOKENS_THINKING = 3_000
CHUNK_OVER = 200
CHUNK_WORDS = 150

# USD per 1M tokens (input, cached input, cache write, output), standard tier, short context, from
# https://developers.openai.com/api/docs/pricing fetched 2026-10-13. A comparison measure, not the plan's bill.
# gpt-6-sol, gpt-5.6-terra and gpt-5.6-luna were not listed that day.
PRICES = {
    "gpt-6-astra": (10.00, 1.00, 12.50, 50.00),
    "gpt-6.1-sol": (2.00, 0.10, 2.50, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.125, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 5.00, 20.00),
}

ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "st", "vs", "e.g", "i.e", "cf", "approx"}
BOUNDARY = re.compile(r"([.!?]+)[\"')\]]*\s+")
PARAGRAPH = re.compile(r"\n\s*\n")
LAST_TOKEN = re.compile(r"([\w.]+)$")

UNAMBIGUOUS = re.compile(r"\b(?:u+m+|u+h+|e+r+m*)\b|,\s*like\s*,", re.IGNORECASE)
YOU_KNOW = re.compile(r"(?:^|[,;:]\s*)you know\s*(?:[,;:.!?]|$)", re.IGNORECASE)
OPENER = re.compile(r"\s*(yeah|okay|ok)\b", re.IGNORECASE)
REGEX_ONLY = {"um", "uh", "er", "erm", "you know"}

OUTCOMES = {"clean", "suspect-then-clean", "other", "failed"}
JUDGED = ("clean", "suspect-then-clean")
YES_NO = {"yes", "no"}
YES_NO_NA = {"yes", "no", "na"}
CHECKS = ("hedges", "mood", "roles", "alternatives")
SOURCE_KEYS = {"id", "kind", "present", "missing", *CHECKS, "output_ids", "note"}
OUTPUT_KEYS = {"id", "sourced", "added", "note"}
LIST_KEYS = {
    "fillers_left": {"output_id": "output", "token": str},
    "stubs_left": {"output_id": "output", "text": str},
    "wrong_fixes": {"output_id": "output", "transcript": str, "output": str},
    "missed_fixes": {"source_id": "source", "transcript": str},
}
REPLY_KEYS = {"source", "output", *LIST_KEYS}

FAULT_TYPES = {"dropped", "clause", "hedge", "mood", "roles", "alternatives"}
OMISSION_TYPES = {"dropped", "clause"}
# field kinds: "id" a source sentence id, "type" a fault type, "transcript"/"output" an exact quote from that text,
# "text" any non-empty string; a bare kind is a list of such values
TALLY_LISTS: dict[str, dict[str, str] | str] = {
    "faults": {"s": "id", "type": "type", "quote": "transcript"},
    "added": "output",
    "stubs_left": "output",
    "wrong_fixes": {"transcript": "text", "output": "output"},
    "missed_fixes": {"s": "id", "transcript": "transcript"},
}
TALLY_KEYS = {"noise", "content_sentences", "fillers_left", *TALLY_LISTS}
PART_KEYS = {"part", "output", *TALLY_KEYS}

METRICS = (
    "critical_free",
    "kept",
    "critical",
    "omissions",
    "distortions",
    "additions",
    "fillers",
    "wrong_fixes",
    "missed_fixes",
    "flags",
    "incomplete",
)


class JudgeError(Exception):
    pass


def split_sentences(text: str) -> list[str]:
    """Split on sentence-final punctuation and on paragraph breaks; an ellipsis or abbreviation does not end one."""
    sentences = []
    for paragraph in PARAGRAPH.split(text):
        paragraph = " ".join(paragraph.split())
        start = 0
        for m in BOUNDARY.finditer(paragraph):
            punct = m.group(1)
            if len(punct) > 1 and set(punct) == {"."}:
                continue
            if punct == ".":
                token = LAST_TOKEN.search(paragraph[start : m.start()])
                if token and token.group(1).lower() in ABBREVIATIONS:
                    continue
            sentences.append(paragraph[start : m.end()].strip())
            start = m.end()
        if paragraph[start:].strip():
            sentences.append(paragraph[start:].strip())
    return sentences


def paragraphs(text: str) -> list[list[str]]:
    """split_sentences grouped by paragraph; the groups concatenate to split_sentences(text)."""
    return [group for group in map(split_sentences, PARAGRAPH.split(text)) if group]


def chunks(text: str) -> list[list[str]]:
    """Transcript parts: each paragraph, or for one over CHUNK_OVER words, blocks closing at the first sentence end
    past CHUNK_WORDS words."""
    parts = []
    for group in paragraphs(text):
        if sum(len(s.split()) for s in group) <= CHUNK_OVER:
            parts.append(group)
            continue
        block, words = [], 0
        for s in group:
            block.append(s)
            words += len(s.split())
            if words > CHUNK_WORDS:
                parts.append(block)
                block, words = [], 0
        if block:
            parts.append(block)
    return parts


def norm(text: str) -> str:
    return " ".join(text.split()).lower()


def regex_fillers(sentence: str) -> Counter[str]:
    """Unambiguous fillers in one sentence, by lowercase token."""
    found = Counter(re.sub(r"[\s,]", "", m.group()).lower() for m in UNAMBIGUOUS.finditer(sentence))
    found["you know"] += len(YOU_KNOW.findall(sentence))
    opener = OPENER.match(sentence)
    if opener:
        found[opener.group(1).lower()] += 1
    return +found


def filler_count(output: list[str], fillers_left: list[dict]) -> int:
    """Regex fillers plus judge-listed ones, dropping judge entries that repeat an occurrence the regex counted."""
    regex = [regex_fillers(s) for s in output]
    total = sum(sum(c.values()) for c in regex)
    listed: Counter[tuple[int, str]] = Counter()
    for f in fillers_left:
        i, token = f["output_id"] - 1, f["token"].strip().lower()
        if token in REGEX_ONLY:
            continue
        if regex[i][token]:
            occurrences = len(re.findall(rf"\b{re.escape(token)}\b", output[i], re.IGNORECASE))
            if listed[(i, token)] >= occurrences - regex[i][token]:
                continue
        listed[(i, token)] += 1
        total += 1
    return total


def tally_fillers(output: str, fillers_left: dict[str, int]) -> int:
    """Regex fillers plus, per judge token, at most its occurrences in the output the regex did not count."""
    regex: Counter[str] = Counter()
    for s in split_sentences(output):
        regex += regex_fillers(s)
    judged: Counter[str] = Counter()
    for token, n in fillers_left.items():
        judged[norm(token)] += n
    total = sum(regex.values())
    for token, n in judged.items():
        occurrences = len(re.findall(rf"\b{re.escape(token)}\b", output, re.IGNORECASE))
        total += min(n, max(0, occurrences - regex[token]))
    return total


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JudgeError(f"cannot read {path}: {exc}") from exc


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def parse_run_id(run_id: str) -> dict:
    parts = run_id.split("_")
    if len(parts) != 3 or not all(parts):
        raise JudgeError(f"run id must be <prompt>_<model>_<effort>: {run_id}")
    return dict(zip(("prompt", "model", "effort"), parts))


class Run:
    """A run folder with its spec and the records of the cases of data/sets/cases.txt it holds; fails fast on
    anything incomplete or mixed. fields["dictionary"] is the records' shared dictionary value, or "off"."""

    def __init__(self, data: Path, run_id: str):
        self.data = data
        self.id = run_id
        self.fields = parse_run_id(run_id)
        self.dir = data / "runs" / run_id
        self.judge_dir = self.dir / "judge"
        spec_path = self.dir / "spec.json"
        if not spec_path.is_file():
            raise JudgeError(f"no spec.json in {self.dir}")
        self.spec = read_json(spec_path)
        if "harness" not in self.spec:
            raise JudgeError(f"{spec_path}: missing key 'harness'")
        try:
            cases = load_cases(data)
        except RunError as exc:
            raise JudgeError(str(exc)) from exc
        self.inputs = {c.case_id: c for c in cases}
        self.cases = [c for c in self.inputs if (self.dir / f"{c}.json").is_file()]
        if not self.cases:
            raise JudgeError(f"no case records in {self.dir}")
        self.records = self._load_records()
        dictionaries = {r["dictionary"] for r in self.records.values()}
        if len(dictionaries) > 1:
            raise JudgeError(f"records in {self.dir} mix dictionary values {sorted(map(str, dictionaries))}")
        self.fields["dictionary"] = dictionaries.pop() or "off"

    def _load_records(self) -> dict[str, dict]:
        records: dict[str, dict] = {}
        problems = []
        for case in self.cases:
            rec = read_json(self.dir / f"{case}.json")
            absent = [k for k in RECORD_FIELDS if k not in rec]
            if absent:
                problems.append(f"{case}.json: missing fields {absent}")
            elif rec["outcome"] not in OUTCOMES:
                problems.append(f"{case}.json: unknown outcome {rec['outcome']!r}")
            elif rec["outcome"] == "failed":
                problems.append(f"{case}.json: outcome failed")
            elif rec["outcome"] in JUDGED and not isinstance(rec["output"], str):
                problems.append(f"{case}.json: outcome {rec['outcome']} without an output string")
            records[case] = rec
        if problems:
            raise JudgeError("run incomplete; rerun it before judging:\n  " + "\n  ".join(problems))
        return records

    def transcript(self, case: str) -> str:
        return self.inputs[case].transcript

    def corrections(self, case: str) -> list[dict]:
        resolution = self.inputs[case].resolution
        return resolution["corrections"] if resolution else []


def item_head(n: int, reply: Path, corrections: list[dict]) -> list[str]:
    lines = [f"## Item {n}", "", f"Reply path: {reply}", "", "Confirmed corrections (heard -> meant):"]
    return lines + ([f'- "{c["heard"]}" -> "{c["meant"]}"' for c in corrections] or ["- none"])


def item_text(n: int, reply: Path, corrections: list[dict], source: list[str], output: list[str]) -> str:
    lines = item_head(n, reply, corrections)
    lines += ["", "Source sentences:"] + [f"S{i}: {s}" for i, s in enumerate(source, 1)]
    lines += ["", "Output sentences:"] + [f"O{i}: {s}" for i, s in enumerate(output, 1)]
    return "\n".join(lines) + "\n"


def numbered(groups: list[list[str]], prefix: str) -> tuple[list[str], list[range]]:
    """One line per group with running [S1]-style markers before each sentence, and each group's id range."""
    lines, ranges, start = [], [], 1
    for group in groups:
        lines.append(" ".join(f"[{prefix}{i}] {s}" for i, s in enumerate(group, start)))
        ranges.append(range(start, start + len(group)))
        start += len(group)
    return lines, ranges


def tally_text(n: int, reply: Path, corrections: list[dict], transcript: list[str], output: str) -> str:
    lines = item_head(n, reply, corrections) + ["", "Transcript:"]
    for block in transcript:
        lines += ["", block]
    return "\n".join(lines + ["", "Output:", "", output]) + "\n"


def batch_text(items: list[str]) -> str:
    head = "Judge each item below and write its reply to the path it names.\n"
    return "\n".join([head, *items])


def estimate_tokens(text: str, reply_tokens: int) -> int:
    return len(text) // 4 + reply_tokens + TOKENS_THINKING


def reply_dir(run: Run, key: dict) -> Path:
    return run.data / "runs" / REPLIES / key["token"]


def item_parts(run: Run, case: str) -> tuple[list[dict], list[str], list[str]]:
    return run.corrections(case), split_sentences(run.transcript(case)), split_sentences(run.records[case]["output"])


Validator = Callable[[object], list[str]]


def item(run: Run, key: dict, n: int, case: str) -> tuple[str, int, Validator]:
    """Item text under the key's method, its reply-token allowance and the validator for its reply."""
    corrections, source, output = item_parts(run, case)
    path = reply_dir(run, key) / f"{n}.json"
    method = key["method"]
    if method == "sentence":
        text = item_text(n, path, corrections, source, output)
        return text, TOKENS_PER_ENTRY * (len(source) + len(output)), lambda r: validate(r, len(source), len(output))
    transcript, cleaned = run.transcript(case), run.records[case]["output"]
    quotes = norm(transcript), norm(cleaned)
    if method == "direct":
        text = tally_text(n, path, corrections, numbered(paragraphs(transcript), "S")[0], cleaned.strip())
        return text, REPLY_TOKENS[method], lambda r: validate_direct(r, len(source), *quotes)
    parts, ranges = numbered(chunks(transcript), "S")
    blocks = [f"Part {i}:\n{p}" for i, p in enumerate(parts, 1)]
    text = tally_text(n, path, corrections, blocks, "\n\n".join(numbered(paragraphs(cleaned), "O")[0]))
    return text, REPLY_TOKENS[method], lambda r: validate_chunked(r, ranges, len(output), *quotes)


def pack(items: list[tuple[int, str, int]]) -> list[list[tuple[int, str]]]:
    """Group (number, text, reply tokens) items into batches within MAX_ITEMS and MAX_TOKENS."""
    batches: list[list[tuple[int, str]]] = [[]]
    used = 0
    for n, text, reply_tokens in items:
        est = estimate_tokens(text, reply_tokens)
        if est > MAX_TOKENS:
            raise JudgeError(f"item {n} alone needs ~{est} tokens, over the {MAX_TOKENS} batch ceiling")
        if batches[-1] and (len(batches[-1]) >= MAX_ITEMS or used + est > MAX_TOKENS):
            batches.append([])
            used = 0
        batches[-1].append((n, text))
        used += est
    return [b for b in batches if b]


def write_batches(folder: Path, stem: str, batches: list[list[tuple[int, str]]], first: int = 1) -> list[Path]:
    paths = []
    for number, batch in enumerate(batches, first):
        path = folder / f"{stem}-{number:02d}.md"
        write_text(path, batch_text([t for _, t in batch]))
        paths.append(path)
    return paths


def prepare(run: Run, method: str = "sentence") -> list[Path]:
    key_path = run.judge_dir / "key.json"
    if key_path.exists():
        key = read_json(key_path)
        have = (key["method"], key["judge_model"], key["judge_version"])
        now = (method, judge_model(method), judge_version(method))
        if have != now:
            raise JudgeError(
                f"{run.judge_dir} was prepared with {' '.join(have)} but this is {' '.join(now)}; "
                "trash it and its reply folder to prepare again"
            )
    else:
        key = {
            "token": secrets.token_hex(6),
            "method": method,
            "judge_model": judge_model(method),
            "judge_version": judge_version(method),
            "items": {},
        }
    prepared = {e["case"] for e in key["items"].values()}
    cases = [c for c in sorted(run.records) if run.records[c]["outcome"] in JUDGED and c not in prepared]
    if not cases:
        raise JudgeError(f"nothing new to judge in {run.id}: every judged case is already in {key_path}")
    random.SystemRandom().shuffle(cases)
    last = max((int(n) for n in key["items"]), default=0)
    first_batch = max((e["batch"] for e in key["items"].values()), default=0) + 1
    items = []
    for n, case in enumerate(cases, last + 1):
        text, reply_tokens, _ = item(run, key, n, case)
        items.append((n, text, reply_tokens))
        key["items"][str(n)] = {"case": case, "sha256": sha256(text.encode("utf-8"))}
    batches = pack(items)
    for number, batch in enumerate(batches, first_batch):
        for n, _ in batch:
            key["items"][str(n)]["batch"] = number
    reply_dir(run, key).mkdir(parents=True, exist_ok=True)
    paths = write_batches(run.judge_dir, "batch", batches, first_batch)
    for path, batch in zip(paths, batches):
        print(f"{path.name}  {len(batch)} items")
    write_text(key_path, json.dumps(key, indent=2) + "\n")
    return paths


def validate(reply, n_source: int, n_output: int) -> list[str]:
    """Schema and rubric-consistency errors of one reply; empty when valid."""
    if not isinstance(reply, dict):
        return ["reply is not a JSON object"]
    errors = []
    if set(reply) != REPLY_KEYS:
        return [f"top-level keys {sorted(reply)} != {sorted(REPLY_KEYS)}"]
    bounds = {"source": n_source, "output": n_output}

    def is_id(value, side: str) -> bool:
        return type(value) is int and 1 <= value <= bounds[side]

    def is_texts(value) -> bool:
        return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)

    def check_enum(where: str, e: dict, field: str, allowed: set[str]) -> None:
        if not (isinstance(e[field], str) and e[field] in allowed):
            errors.append(f"{where}: {field} {e[field]!r}")

    for side, keys in (("source", SOURCE_KEYS), ("output", OUTPUT_KEYS)):
        entries = reply[side]
        if not isinstance(entries, list) or len(entries) != bounds[side]:
            errors.append(f"{side}: expected {bounds[side]} entries")
            continue
        for i, e in enumerate(entries, 1):
            where = f"{side}[{i}]"
            if not isinstance(e, dict) or set(e) != keys:
                errors.append(f"{where}: keys must be {sorted(keys)}")
                continue
            before = len(errors)
            if type(e["id"]) is not int or e["id"] != i:
                errors.append(f"{where}: id {e['id']!r} != {i}")
            if not isinstance(e["note"], str):
                errors.append(f"{where}: note not a string")
            if side == "output":
                check_enum(where, e, "sourced", YES_NO)
                if not is_texts(e["added"]):
                    errors.append(f"{where}: added must be a list of non-empty strings")
                elif e["sourced"] == "no" and e["added"]:
                    errors.append(f"{where}: an unsourced sentence takes added []")
                continue
            check_enum(where, e, "kind", {"content", "noise"})
            check_enum(where, e, "present", YES_NO)
            for check in CHECKS:
                check_enum(where, e, check, YES_NO_NA)
            if not is_texts(e["missing"]):
                errors.append(f"{where}: missing must be a list of non-empty strings")
            if not isinstance(e["output_ids"], list) or not all(is_id(v, "output") for v in e["output_ids"]):
                errors.append(f"{where}: output_ids must be output ids")
            if len(errors) > before:
                continue
            all_na = all(e[c] == "na" for c in CHECKS)
            if (e["present"] == "no") != bool(e["missing"]):
                errors.append(f"{where}: missing must be non-empty exactly when present is no")
            if e["kind"] == "noise":
                if not all_na or e["present"] != "yes" or e["output_ids"]:
                    errors.append(f"{where}: noise needs present yes, output_ids [] and every check na")
            elif not e["output_ids"]:
                if e["present"] != "no" or not all_na:
                    errors.append(f"{where}: content no output carries needs present no and every check na")
            elif e["mood"] == "na":
                errors.append(f"{where}: mood is na only for content no output carries")
    for name, fields in LIST_KEYS.items():
        entries = reply[name]
        if not isinstance(entries, list):
            errors.append(f"{name}: not a list")
            continue
        for i, e in enumerate(entries, 1):
            if not isinstance(e, dict) or set(e) != set(fields):
                errors.append(f"{name}[{i}]: keys must be {sorted(fields)}")
                continue
            for field, kind in fields.items():
                ok = is_id(e[field], kind) if isinstance(kind, str) else isinstance(e[field], str) and e[field].strip()
                if not ok:
                    errors.append(f"{name}[{i}]: bad {field} {e[field]!r}")
    return errors


def validate_tally(t: dict, ids: range, transcript: str, output: str) -> list[str]:
    """Errors in the tally fields of a direct reply or a chunked part; ids are the sentences it may name, and
    transcript and output are normalised with norm for the quote checks."""
    errors = []
    texts = {"transcript": transcript, "output": output}

    def ok(value, kind: str) -> bool:
        if kind == "id":
            return type(value) is int and value in ids
        if not isinstance(value, str):
            return False
        if kind == "type":
            return value in FAULT_TYPES
        return bool(value.strip()) and (kind == "text" or norm(value) in texts[kind])

    noise = t["noise"]
    if not (isinstance(noise, list) and all(ok(v, "id") for v in noise) and len(set(noise)) == len(noise)):
        errors.append(f"noise must be distinct sentence ids S{ids.start}..S{ids.stop - 1}: {noise!r}")
    if type(t["content_sentences"]) is not int or t["content_sentences"] < 0:
        errors.append(f"bad content_sentences {t['content_sentences']!r}")
    fillers = t["fillers_left"]
    if not isinstance(fillers, dict):
        errors.append("fillers_left: not an object")
    else:
        for token, n in fillers.items():
            if not norm(token) or norm(token) in REGEX_ONLY or type(n) is not int or n < 1:
                errors.append(f"fillers_left: bad entry {token!r}: {n!r}")
    for name, fields in TALLY_LISTS.items():
        entries = t[name]
        if not isinstance(entries, list):
            errors.append(f"{name}: not a list")
            continue
        for i, e in enumerate(entries, 1):
            if isinstance(fields, str):
                if not ok(e, fields):
                    errors.append(f"{name}[{i}]: {e!r} is not a quote from the {fields}")
                continue
            if not isinstance(e, dict) or set(e) != set(fields):
                errors.append(f"{name}[{i}]: keys must be {sorted(fields)}")
                continue
            for field, kind in fields.items():
                if not ok(e[field], kind):
                    what = f"not a quote from the {kind}" if kind in texts else "bad"
                    errors.append(f"{name}[{i}]: {field} {e[field]!r} {what}")
    if errors:
        return errors
    types: dict[int, list[str]] = {}
    for f in t["faults"]:
        types.setdefault(f["s"], []).append(f["type"])
    for s, found in sorted(types.items()):
        if s in noise:
            errors.append(f"faults: S{s} is listed as noise")
        if "dropped" in found and len(found) > 1:
            errors.append(f"faults: S{s} is dropped, which stands alone")
        repeated = sorted({k for k in found if k != "clause" and found.count(k) > 1})
        if repeated:
            errors.append(f"faults: S{s} repeats {repeated}")
    return errors


def validate_direct(reply, n_source: int, transcript: str, output: str) -> list[str]:
    """Errors of one direct-method reply; empty when valid."""
    if not isinstance(reply, dict):
        return ["reply is not a JSON object"]
    if set(reply) != TALLY_KEYS:
        return [f"top-level keys {sorted(reply)} != {sorted(TALLY_KEYS)}"]
    return validate_tally(reply, range(1, n_source + 1), transcript, output)


def validate_chunked(reply, parts: list[range], n_output: int, transcript: str, output: str) -> list[str]:
    """Errors of one chunked-method reply; parts are the source id ranges of the transcript parts."""
    if not isinstance(reply, dict):
        return ["reply is not a JSON object"]
    if set(reply) != {"parts"}:
        return [f"top-level keys {sorted(reply)} != ['parts']"]
    if not isinstance(reply["parts"], list) or len(reply["parts"]) != len(parts):
        return [f"parts: expected {len(parts)} entries"]
    errors, spans = [], []
    for i, (e, ids) in enumerate(zip(reply["parts"], parts), 1):
        where = f"parts[{i}]"
        if not isinstance(e, dict) or set(e) != PART_KEYS:
            errors.append(f"{where}: keys must be {sorted(PART_KEYS)}")
            continue
        if type(e["part"]) is not int or e["part"] != i:
            errors.append(f"{where}: part {e['part']!r} != {i}")
        span = e["output"]
        if not isinstance(span, list) or (
            span
            and not (len(span) == 2 and all(type(v) is int and 1 <= v <= n_output for v in span) and span[0] <= span[1])
        ):
            errors.append(f"{where}: output must be [] or [first, last] within O1..O{n_output}: {span!r}")
        elif span:
            spans.append(span)
        errors += [f"{where}: {x}" for x in validate_tally(e, ids, transcript, output)]
    if not errors and n_output:
        gaps = any(not a[1] <= b[0] <= a[1] + 1 for a, b in pairwise(spans))
        if not spans or spans[0][0] != 1 or spans[-1][1] != n_output or gaps:
            errors.append(f"output spans must run in order over O1..O{n_output}, sharing at most a boundary sentence")
    return errors


def merge_parts(reply: dict) -> dict:
    """One direct-method tally summed over a chunked reply's parts."""
    parts = reply["parts"]
    fillers: Counter[str] = Counter()
    for p in parts:
        fillers.update(p["fillers_left"])
    merged = {k: [x for p in parts for x in p[k]] for k in ("noise", *TALLY_LISTS)}
    return merged | {"content_sentences": sum(p["content_sentences"] for p in parts), "fillers_left": dict(fillers)}


def check_replies(
    folder: Path, key: dict, validators: dict[str, Validator]
) -> tuple[dict[str, dict], dict[str, str]]:
    """Valid replies by case id, and problems by item number."""
    valid, problems = {}, {}
    for n, entry in key["items"].items():
        path = folder / f"{n}.json"
        if not path.is_file():
            problems[n] = "missing"
            continue
        try:
            reply = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems[n] = f"invalid JSON: {exc}"
            continue
        errors = validators[n](reply)
        if errors:
            problems[n] = "invalid: " + "; ".join(errors[:3]) + (f" (+{len(errors) - 3})" if len(errors) > 3 else "")
        else:
            valid[entry["case"]] = reply
    return valid, problems


def sentence_tally(reply: dict, output: str) -> dict:
    """Fault counts of a per-sentence reply."""
    content = [s for s in reply["source"] if s["kind"] == "content"]
    additions = sum(o["sourced"] == "no" for o in reply["output"])
    additions += sum(len(o["added"]) for o in reply["output"] if o["sourced"] == "yes")
    return {
        "content": len(content),
        "omitted": sum(s["present"] == "no" for s in content),
        "distorted": sum(s["present"] == "yes" and any(s[c] == "no" for c in CHECKS) for s in content),
        "additions": additions,
        "fillers": filler_count(split_sentences(output), reply["fillers_left"]) + len(reply["stubs_left"]),
        "wrong_fixes": len(reply["wrong_fixes"]),
        "missed_fixes": len(reply["missed_fixes"]),
        "judge_content": None,
    }


def direct_tally(reply: dict, n_source: int, output: str) -> dict:
    """Fault counts of a direct-method tally; content is the split's sentences minus the judge's noise."""
    omitted = {f["s"] for f in reply["faults"] if f["type"] in OMISSION_TYPES}
    distorted = {f["s"] for f in reply["faults"]} - omitted
    return {
        "content": n_source - len(reply["noise"]),
        "omitted": len(omitted),
        "distorted": len(distorted),
        "additions": len(reply["added"]),
        "fillers": tally_fillers(output, reply["fillers_left"]) + len(reply["stubs_left"]),
        "wrong_fixes": len(reply["wrong_fixes"]),
        "missed_fixes": len(reply["missed_fixes"]),
        "judge_content": reply["content_sentences"],
    }


def reply_tally(method: str, reply: dict, n_source: int, output: str) -> dict:
    if method == "sentence":
        return sentence_tally(reply, output)
    return direct_tally(merge_parts(reply) if method == "chunked" else reply, n_source, output)


def case_metrics(record: dict, tally: dict | None, words: int) -> tuple[dict, dict]:
    """Per-case metric values and raw fault counts; None where a value does not apply."""
    flags = len(record.get("suspect_lists") or [])
    if record["outcome"] == "other":
        counts = {"kept": 0.0, **dict.fromkeys(("omissions", "distortions", "additions", "fillers", "critical"))}
        counts |= {"content": None, "judge_content": None}
        return {
            "critical_free": 0.0,
            "kept": 0.0,
            "critical": 100.0,
            "omissions": 100.0,
            "distortions": 0.0,
            "additions": None,
            "fillers": None,
            "wrong_fixes": None,
            "missed_fixes": None,
            "flags": flags,
            "incomplete": 100.0,
        }, counts
    assert tally is not None
    content, omitted, distorted = tally["content"], tally["omitted"], tally["distorted"]
    additions, fillers = tally["additions"], tally["fillers"]
    kept = content - omitted - distorted

    def per100(count: int) -> float | None:
        return 100 * count / content if content else None

    critical = omitted + distorted + additions
    counts = {
        "kept": per100(kept),
        "omissions": omitted,
        "distortions": distorted,
        "additions": additions,
        "fillers": fillers,
        "critical": critical,
        "content": content,
        "judge_content": tally["judge_content"],
    }
    return {
        "critical_free": 0.0 if critical else 100.0,
        "kept": per100(kept),
        "critical": per100(critical),
        "omissions": per100(omitted),
        "distortions": per100(distorted),
        "additions": float(additions),
        "fillers": 1000 * fillers / words if words else None,
        "wrong_fixes": float(tally["wrong_fixes"]),
        "missed_fixes": float(tally["missed_fixes"]),
        "flags": float(flags),
        "incomplete": 0.0,
    }, counts


def mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def mean_se(values: list[float | None]) -> tuple[float | None, float | None]:
    present = [v for v in values if v is not None]
    if not present:
        return None, None
    m = sum(present) / len(present)
    if len(present) < 2:
        return m, None
    var = sum((v - m) ** 2 for v in present) / (len(present) - 1)
    return m, math.sqrt(var / len(present))


def aggregate(per_case: dict[str, dict]) -> dict[str, tuple[float | None, float | None]]:
    return {m: mean_se([v[m] for v in per_case.values()]) for m in METRICS}


def paired_delta(
    current: dict[str, float | None], previous: dict[str, float | None]
) -> tuple[float | None, float | None, dict]:
    """Mean per-case difference current - previous over cases defined in both, its SE, and the per-case deltas."""
    if set(current) != set(previous):
        raise JudgeError("paired runs cover different cases")
    deltas = {}
    for c in sorted(current):
        a, b = current[c], previous[c]
        if a is not None and b is not None:
            deltas[c] = a - b
    m, se = mean_se(list(deltas.values()))
    return m, se, deltas


def call_cost(record: dict, model: str) -> float | None:
    if model not in PRICES:
        return None
    price_in, price_cached, price_write, price_out = PRICES[model]
    total = record.get("input_tokens")
    if total is None:
        return None
    cached = record.get("cached_input_tokens") or 0
    written = record.get("cache_write_input_tokens") or 0
    out = record.get("output_tokens") or 0
    fresh = total - cached - written
    return (fresh * price_in + cached * price_cached + written * price_write + out * price_out) / 1e6


def call_stats(run: Run, cases: list[str]) -> dict:
    recs = [run.records[c] for c in cases]

    def tokens(r: dict) -> int | None:
        if r.get("input_tokens") is None or r.get("output_tokens") is None:
            return None
        return r["input_tokens"] + r["output_tokens"]

    return {
        "tokens": mean([tokens(r) for r in recs]),
        "reasoning": mean([r.get("reasoning_output_tokens") for r in recs]),
        "cost": mean([call_cost(r, run.fields["model"]) for r in recs]),
        "seconds": mean([r.get("seconds") for r in recs]),
    }


def git(cwd: Path, *args: str) -> str:
    cmd = ["git", "-C", str(cwd), *args]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise JudgeError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result.stdout.strip()


def commit_of(paths: tuple[str, ...]) -> str:
    head = git(ROOT, "log", "-1", "--format=%H", "--", *paths)[:7] or "none"
    tracked = git(ROOT, "ls-files", "--", *paths).splitlines()
    dirty = bool(git(ROOT, "status", "--porcelain", "--", *paths)) or len(tracked) < len(paths)
    return head + ("+dirty" if dirty else "")


def judge_version(method: str) -> str:
    """Last commit of the method's agent file and this script; any method but sentence is prefixed, "direct abc1234"."""
    commit = commit_of((AGENTS[method], JUDGE_CODE))
    return commit if method == "sentence" else f"{method} {commit}"


def judge_label(s: dict) -> str:
    """Results-table judge column: "opus abc1234", or "opus/direct abc1234" for another method."""
    method, _, commit = s["judge_version"].rpartition(" ")
    return f"{s['judge_model']}{'/' + method if method else ''} {commit}"


def judge_model(method: str) -> str:
    text = (ROOT / AGENTS[method]).read_text(encoding="utf-8")
    m = re.search(r"^model:\s*(\S+)", text, re.MULTILINE)
    if not m:
        raise JudgeError(f"no model line in {AGENTS[method]}")
    return m.group(1)


def previous_change(prompt: str, changes: list[str]) -> str | None:
    """The AGENTS.md change before the one prompt measured; changes are those at or before prompt, newest first."""
    if not changes:
        return None
    if changes[0] == prompt:
        return changes[1] if len(changes) > 1 else None
    return changes[0]


def previous_prompt(prompt: str) -> str | None:
    """Comparison prompt for a row: the previous cleaner commit that changed AGENTS.md, relative to the row's commit."""
    if prompt.startswith("file-"):
        return None
    changes = git(CLEANER, "log", "--format=%H", prompt, "--", "AGENTS.md").splitlines()
    return previous_change(prompt, [h[:7] for h in changes])


Previous = Callable[[str], str | None]


def write_scores(path: Path, run_id: str, rows: list[dict]) -> None:
    """Replace run_id's lines in the per-case scores file with rows, keeping every other run's lines."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    kept = [ln for ln in lines if ln.strip() and json.loads(ln)["run_id"] != run_id]
    write_text(path, "".join(f"{ln}\n" for ln in kept + [json.dumps(r) for r in rows]))


def view(run: Run, per_case: dict[str, dict], words: dict[str, int], cases: list[str]) -> dict:
    """Aggregates over the scored cases among `cases`."""
    chosen = {c: per_case[c] for c in cases if c in per_case}
    bands: dict[str, dict] = {}
    for case, values in chosen.items():
        bands.setdefault(band(words[case]) or "outside bands", {})[case] = values
    return {
        "n_cases": len(chosen),
        "metrics": aggregate(chosen),
        "calls": call_stats(run, list(chosen)),
        "bands": {b: {"n": len(v), "metrics": aggregate(v)} for b, v in sorted(bands.items())},
        "per_case": chosen,
    }


def score(run: Run, previous: Previous, method: str = "sentence") -> dict:
    """Score the run into judge/summary.json and print the per-case kept deltas when a comparison row exists."""
    words = {c: len(run.transcript(c).split()) for c in run.cases}
    key = read_json(run.judge_dir / "key.json")
    if key.get("method") != method:
        raise JudgeError(f"prepared with --method {key.get('method')}; score with the same method, or prepare again")
    unprepared = [
        c for c in run.cases if run.records[c]["outcome"] in JUDGED and c not in {e["case"] for e in key["items"].values()}
    ]
    if unprepared:
        raise JudgeError(f"{len(unprepared)} judged case(s) have no verdict yet; run prepare again to batch them")
    stale = []
    current_judge = (judge_model(method), judge_version(method))
    if (key["judge_model"], key["judge_version"]) != current_judge:
        stale.append(f"judge {key['judge_model']} {key['judge_version']} -> {' '.join(current_judge)}")
    texts, reply_tokens, validators = {}, {}, {}
    for n, entry in key["items"].items():
        texts[n], reply_tokens[n], validators[n] = item(run, key, int(n), entry["case"])
    changed = [n for n in texts if sha256(texts[n].encode("utf-8")) != key["items"][n]["sha256"]]
    if changed:
        stale.append(f"inputs of items {', '.join(sorted(changed, key=int))}")
    if stale:
        raise JudgeError(
            f"changed since prepare: {'; '.join(stale)}; trash {run.judge_dir} and its reply folder and prepare again"
        )
    replies, problems = check_replies(reply_dir(run, key), key, validators)
    for old in run.judge_dir.glob("retry-*.md"):
        old.unlink()
    if problems:
        for n in sorted(problems, key=int):
            print(f"item {n}: {problems[n]}")
        numbers = sorted(problems, key=int)
        batches = pack([(int(n), texts[n], reply_tokens[n]) for n in numbers])
        paths = write_batches(run.judge_dir, "retry", batches)
        raise JudgeError(
            f"{len(problems)} of {len(key['items'])} replies missing or invalid; one fresh judge per file: "
            + ", ".join(str(p) for p in paths)
        )
    per_case, counts = {}, {}
    for case in run.cases:
        tally = None
        if case in replies:
            n_source = len(split_sentences(run.transcript(case)))
            tally = reply_tally(method, replies[case], n_source, run.records[case]["output"])
        per_case[case], counts[case] = case_metrics(run.records[case], tally, words[case])
    quick = list(run.inputs)[:DEFAULT_N]
    summary = {
        "run_id": run.id,
        **run.fields,
        "harness": run.spec["harness"],
        "judge_model": key["judge_model"],
        "judge_version": key["judge_version"],
        **view(run, per_case, words, run.cases),
        "quick": view(run, per_case, words, quick),
    }
    write_text(run.judge_dir / "summary.json", json.dumps(summary, indent=2) + "\n")
    rows = [
        {
            "run_id": run.id,
            **run.fields,
            "case_id": case,
            "method": method,
            "outcome": run.records[case]["outcome"],
            **counts[case],
        }
        for case in run.cases
    ]
    write_scores(run.data / "scores.jsonl", run.id, rows)
    found =find_delta(summary, load_summaries(run.data / "runs"), previous)
    if found:
        print(f"delta kept {found[0]}")
        for case, d in found[1].items():
            print(f"  {case}  {d:+.1f}")
    return summary


def fmt(value: float | None, digits: int, se: float | None = None) -> str:
    if value is None:
        return "n/a"
    text = f"{value:.{digits}f}"
    return text + (f" ({se:.{digits}f})" if se is not None else "")


def load_summaries(runs: Path) -> list[dict]:
    summaries = []
    for path in sorted(runs.glob("*/judge/summary.json")):
        if path.parent.parent.name.startswith("_"):
            continue
        summaries.append(read_json(path))
    return summaries


def find_delta(s: dict, summaries: list[dict], previous: Previous, rows: str = "full") -> tuple[str, dict] | None:
    """Comparison row for s: the previous AGENTS.md change at the same cell and judge version, paired over the cases
    both runs scored in the rows ("quick" or "full")."""
    target = previous(s["prompt"])
    if target is None:
        return None
    cell = ("model", "effort", "dictionary", "judge_version")
    for p in summaries:
        if p["prompt"] == target and all(p[k] == s[k] for k in cell):
            cur, old = (s, p) if rows == "full" else (s[rows], p[rows])
            common = cur["per_case"].keys() & old["per_case"].keys()
            current = {c: cur["per_case"][c]["kept"] for c in common}
            prior = {c: old["per_case"][c]["kept"] for c in common}
            m, se, deltas = paired_delta(current, prior)
            return f"{fmt(m, 1, se)} vs {p['prompt']}", deltas
    return None


COLUMNS = (
    "prompt", "model", "effort", "dictionary", "set", "n", "kept % (SE)", "critical faults /100 sentences",
    "omissions /100", "distortions /100", "additions /case", "fillers /1k words", "critical-free %", "delta kept",
    "flags /case", "wrong fixes /case", "missed fixes /case", "incomplete %", "tokens /call", "cost /call (USD)",
    "seconds /call", "judge", "harness",
)  # fmt: skip
DIGITS = {
    "critical_free": 1, "kept": 1, "critical": 1, "omissions": 1, "distortions": 1, "additions": 2, "fillers": 1,
    "wrong_fixes": 2, "missed_fixes": 2, "incomplete": 1, "flags": 2,
}  # fmt: skip


def table_row(s: dict, rows: str, delta: str) -> list[str]:
    v = s if rows == "full" else s["quick"]
    m = {k: fmt(x[0], DIGITS[k]) for k, x in v["metrics"].items()}
    kept, kept_se = v["metrics"]["kept"]
    c = v["calls"]
    return [
        s["prompt"], s["model"], s["effort"], s["dictionary"], rows, str(v["n_cases"]),
        fmt(kept, 1, kept_se), m["critical"], m["omissions"], m["distortions"], m["additions"], m["fillers"],
        m["critical_free"], delta, m["flags"], m["wrong_fixes"], m["missed_fixes"], m["incomplete"],
        fmt(c["tokens"], 0), fmt(c["cost"], 4), fmt(c["seconds"], 1),
        judge_label(s), s["harness"],
    ]  # fmt: skip


def render_table(summaries: list[dict], previous: Previous) -> str:
    keys = [(s["prompt"], s["model"], s["effort"], s["dictionary"], s["judge_version"]) for s in summaries]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise JudgeError(f"two judged runs share a results row: {sorted(dupes)}")
    cells = []
    for s in summaries:
        if s["quick"]["n_cases"]:
            cells.append((s, "quick"))
        if s["n_cases"] > s["quick"]["n_cases"]:
            cells.append((s, "full"))
    cells.sort(key=lambda c: (c[1] != "quick", c[0]["model"], c[0]["effort"], c[0]["dictionary"], c[0]["prompt"]))
    rows = []
    for s, which in cells:
        found = find_delta(s, summaries, previous, which)
        rows.append(table_row(s, which, found[0] if found else "n/a"))
    lines = [
        "# Results",
        "",
        "Generated by `uv run python src/judge.py score`; do not edit by hand. One row per prompt, model, effort,",
        "dictionary, case rows and judge version. The quick row is the first 10 cases of data/sets/cases.txt, the",
        "full row every judged case (shown when the run has more than 10). Values are per-case means, kept % with its",
        "standard error in brackets. Critical faults are omissions, distortions and additions per 100 content",
        "sentences. Rows are comparable only at the same case rows and judge version. Cost is list price, a comparison",
        "measure.",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "|".join("---" for _ in COLUMNS) + "|",
    ]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def regenerate(data: Path, previous: Previous) -> int:
    summaries = load_summaries(data / "runs")
    write_text(RESULTS, render_table(summaries, previous))
    return len(summaries)


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge batches and scoring for the cleaning benchmark.")
    parser.add_argument("command", choices=("prepare", "score"))
    parser.add_argument("--run", required=True, help="run folder name under <data>/runs/")
    parser.add_argument("--method", choices=tuple(AGENTS), default="sentence", help="judging method")
    parser.add_argument("--data", type=Path, default=DATA, help="data root holding cases/, sets/ and runs/")
    args = parser.parse_args()
    try:
        run = Run(args.data.resolve(), args.run)
        if args.command == "prepare":
            judged = sum(r["outcome"] in JUDGED for r in run.records.values())
            print(f"{run.id}: {len(run.records)} cases, {judged} to judge, method {args.method}")
            paths = prepare(run, args.method)
            print(f"{len(paths)} batches in {run.judge_dir} for the {Path(AGENTS[args.method]).stem} agent")
            return 0
        summary = score(run, previous_prompt, args.method)
        m =summary["metrics"]
        print(
            f"{run.id}: critical-free {fmt(m['critical_free'][0], 1)}%, kept {fmt(m['kept'][0], 1, m['kept'][1])}%, "
            f"incomplete {fmt(m['incomplete'][0], 1)}%; judge {summary['judge_version']}"
        )
        rows = regenerate(run.data, previous_prompt)
        print(f"{RESULTS.relative_to(ROOT)}: {rows} rows")
        return 0
    except JudgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
