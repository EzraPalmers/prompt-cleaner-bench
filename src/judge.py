"""Blind judge batches, reply validation, scoring and the public results table.

Usage:
  uv run python src/judge.py prepare --run RUN_ID
      Write blind batch files to data/runs/RUN_ID/judge/batch-NN.md and the item key to judge/key.json. The key
      records the set version, the judge model and version, and a hash of every item as prepared.
      The bench skill hands each batch file's text, and nothing else, to a fresh `judge` agent.
  uv run python src/judge.py score --run RUN_ID
      Refuse if the set, the judge or any item's inputs changed since prepare. Validate every reply. If any is
      missing or invalid, list them and write judge/retry-NN.md, one per fresh judge (exit 1). Once all are valid,
      score the run into judge/summary.json (per case and per length band) and regenerate outputs/results.md from
      every judged run.

Flags:
  --run RUN_ID  run folder name under <data>/runs/ (<prompt>_<model>_<effort>_<set>_<dictionary>)
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
from pathlib import Path

from config import ROOT
from extract import band
from run import RECORD_FIELDS, RunError, load_set, sha256

DATA = ROOT / "data"
RESULTS = ROOT / "outputs" / "results.md"
CLEANER = ROOT / "cleaner"
JUDGE_PATHS = (".claude/agents/judge.md", "src/judge.py")
REPLIES = "_replies"

MAX_ITEMS = 5
MAX_TOKENS = 80_000
TOKENS_PER_ENTRY = 110
TOKENS_THINKING = 3_000

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

METRICS = (
    "critical_free",
    "kept",
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
    if len(parts) != 5 or not all(parts):
        raise JudgeError(f"run id must be <prompt>_<model>_<effort>_<set>_<dictionary>: {run_id}")
    return dict(zip(("prompt", "model", "effort", "set", "dictionary"), parts))


class Run:
    """A run folder with its spec, schedule and per-call records; fails fast on anything incomplete or stale."""

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
        for key in ("set_version", "repeats", "harness"):
            if key not in self.spec:
                raise JudgeError(f"{spec_path}: missing key '{key}'")
        try:
            cases, version = load_set(data, self.fields["set"])
        except RunError as exc:
            raise JudgeError(str(exc)) from exc
        if version != self.spec["set_version"]:
            raise JudgeError(
                f"set is now {version} but the run used {self.spec['set_version']}: transcripts or resolutions "
                "changed since the run"
            )
        self.set_version = version
        self.inputs = {c.case_id: c for c in cases}
        self.cases = list(self.inputs)
        self.records = self._load_records()

    def _load_records(self) -> dict[str, dict]:
        repeats = self.spec["repeats"]
        if type(repeats) is not int or repeats < 1:
            raise JudgeError(f"spec.json: repeats must be a positive integer, got {repeats!r}")
        records: dict[str, dict] = {}
        problems = []
        for case in self.cases:
            pattern = re.compile(rf"{re.escape(case)}\.\d+\.json")
            found = {p.stem for p in self.dir.glob(f"{case}.*.json") if pattern.fullmatch(p.name)}
            expected = {f"{case}.{k}" for k in range(1, repeats + 1)}
            problems += [f"{c}.json: missing" for c in sorted(expected - found)]
            problems += [f"{c}.json: not scheduled (repeats {repeats})" for c in sorted(found - expected)]
            for call in sorted(expected & found):
                rec = read_json(self.dir / f"{call}.json")
                absent = [k for k in RECORD_FIELDS if k not in rec]
                if absent:
                    problems.append(f"{call}.json: missing fields {absent}")
                elif rec["outcome"] not in OUTCOMES:
                    problems.append(f"{call}.json: unknown outcome {rec['outcome']!r}")
                elif rec["outcome"] == "failed":
                    problems.append(f"{call}.json: outcome failed")
                elif rec["outcome"] in JUDGED and not isinstance(rec["output"], str):
                    problems.append(f"{call}.json: outcome {rec['outcome']} without an output string")
                records[call] = rec
        if problems:
            raise JudgeError("run incomplete; rerun it before judging:\n  " + "\n  ".join(problems))
        return records

    def case_of(self, call: str) -> str:
        return call.rsplit(".", 1)[0]

    def transcript(self, case: str) -> str:
        return self.inputs[case].transcript

    def corrections(self, case: str) -> list[dict]:
        resolution = self.inputs[case].resolution
        return resolution["corrections"] if resolution else []


def item_text(n: int, reply: Path, corrections: list[dict], source: list[str], output: list[str]) -> str:
    lines = [f"## Item {n}", "", f"Reply path: {reply}", "", "Confirmed corrections (heard -> meant):"]
    lines += [f'- "{c["heard"]}" -> "{c["meant"]}"' for c in corrections] or ["- none"]
    lines += ["", "Source sentences:"] + [f"S{i}: {s}" for i, s in enumerate(source, 1)]
    lines += ["", "Output sentences:"] + [f"O{i}: {s}" for i, s in enumerate(output, 1)]
    return "\n".join(lines) + "\n"


def batch_text(items: list[str]) -> str:
    head = "Judge each item below and write its reply to the path it names.\n"
    return "\n".join([head, *items])


def estimate_tokens(text: str, entries: int) -> int:
    return len(text) // 4 + TOKENS_PER_ENTRY * entries + TOKENS_THINKING


def reply_dir(run: Run, key: dict) -> Path:
    return run.data / "runs" / REPLIES / key["token"]


def item_parts(run: Run, call: str) -> tuple[list[dict], list[str], list[str]]:
    case = run.case_of(call)
    return run.corrections(case), split_sentences(run.transcript(case)), split_sentences(run.records[call]["output"])


def item(run: Run, key: dict, n: int, call: str) -> tuple[str, int, int]:
    """Item text with its source and output sentence counts."""
    corrections, source, output = item_parts(run, call)
    return item_text(n, reply_dir(run, key) / f"{n}.json", corrections, source, output), len(source), len(output)


def pack(items: list[tuple[int, str, int]]) -> list[list[tuple[int, str]]]:
    """Group (number, text, entries) items into batches within MAX_ITEMS and MAX_TOKENS."""
    batches: list[list[tuple[int, str]]] = [[]]
    used = 0
    for n, text, entries in items:
        est = estimate_tokens(text, entries)
        if est > MAX_TOKENS:
            raise JudgeError(f"item {n} alone needs ~{est} tokens, over the {MAX_TOKENS} batch ceiling")
        if batches[-1] and (len(batches[-1]) >= MAX_ITEMS or used + est > MAX_TOKENS):
            batches.append([])
            used = 0
        batches[-1].append((n, text))
        used += est
    return [b for b in batches if b]


def write_batches(folder: Path, stem: str, batches: list[list[tuple[int, str]]]) -> list[Path]:
    paths = []
    for number, batch in enumerate(batches, 1):
        path = folder / f"{stem}-{number:02d}.md"
        write_text(path, batch_text([t for _, t in batch]))
        paths.append(path)
    return paths


def prepare(run: Run) -> list[Path]:
    key_path = run.judge_dir / "key.json"
    if key_path.exists():
        raise JudgeError(f"already prepared: {run.judge_dir}; trash it and its reply folder to prepare again")
    calls = [c for c in sorted(run.records) if run.records[c]["outcome"] in JUDGED]
    random.SystemRandom().shuffle(calls)
    key = {
        "token": secrets.token_hex(6),
        "set_version": run.set_version,
        "judge_model": judge_model(),
        "judge_version": judge_version(),
        "items": {},
    }
    items = []
    for n, call in enumerate(calls, 1):
        text, n_source, n_output = item(run, key, n, call)
        items.append((n, text, n_source + n_output))
        key["items"][str(n)] = {"call": call, "sha256": sha256(text.encode("utf-8"))}
    batches = pack(items)
    for number, batch in enumerate(batches, 1):
        for n, _ in batch:
            key["items"][str(n)]["batch"] = number
    reply_dir(run, key).mkdir(parents=True, exist_ok=True)
    paths = write_batches(run.judge_dir, "batch", batches)
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


def check_replies(folder: Path, key: dict, sizes: dict[str, tuple[int, int]]) -> tuple[dict[str, dict], dict[str, str]]:
    """Valid replies by call, and problems by item number."""
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
        errors = validate(reply, *sizes[n])
        if errors:
            problems[n] = "invalid: " + "; ".join(errors[:3]) + (f" (+{len(errors) - 3})" if len(errors) > 3 else "")
        else:
            valid[entry["call"]] = reply
    return valid, problems


def call_metrics(record: dict, reply: dict | None, words: int) -> dict:
    """Per-call values; None where a metric does not apply."""
    flags = len(record.get("suspect_lists") or [])
    if record["outcome"] == "other":
        return {
            "critical_free": 0.0,
            "kept": 0.0,
            "omissions": 100.0,
            "distortions": 0.0,
            "additions": None,
            "fillers": None,
            "wrong_fixes": None,
            "missed_fixes": None,
            "flags": flags,
            "incomplete": 100.0,
        }
    assert reply is not None
    content = [s for s in reply["source"] if s["kind"] == "content"]
    omitted = sum(s["present"] == "no" for s in content)
    distorted = sum(s["present"] == "yes" and any(s[c] == "no" for c in CHECKS) for s in content)
    kept = len(content) - omitted - distorted
    additions = sum(o["sourced"] == "no" for o in reply["output"])
    additions += sum(len(o["added"]) for o in reply["output"] if o["sourced"] == "yes")
    fillers = filler_count(split_sentences(record["output"]), reply["fillers_left"]) + len(reply["stubs_left"])

    def per100(count: int) -> float | None:
        return 100 * count / len(content) if content else None

    return {
        "critical_free": 0.0 if omitted or distorted or additions else 100.0,
        "kept": per100(kept),
        "omissions": per100(omitted),
        "distortions": per100(distorted),
        "additions": float(additions),
        "fillers": 1000 * fillers / words if words else None,
        "wrong_fixes": float(len(reply["wrong_fixes"])),
        "missed_fixes": float(len(reply["missed_fixes"])),
        "flags": float(flags),
        "incomplete": 0.0,
    }


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
        raise JudgeError("paired runs cover different cases despite the same set version")
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


def call_stats(run: Run) -> dict:
    recs = list(run.records.values())

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


def judge_version() -> str:
    head = git(ROOT, "log", "-1", "--format=%H", "--", *JUDGE_PATHS)[:7] or "none"
    tracked = git(ROOT, "ls-files", "--", *JUDGE_PATHS).splitlines()
    dirty = bool(git(ROOT, "status", "--porcelain", "--", *JUDGE_PATHS)) or len(tracked) < len(JUDGE_PATHS)
    return head + ("+dirty" if dirty else "")


def judge_model() -> str:
    text = (ROOT / JUDGE_PATHS[0]).read_text(encoding="utf-8")
    m = re.search(r"^model:\s*(\S+)", text, re.MULTILINE)
    if not m:
        raise JudgeError(f"no model line in {JUDGE_PATHS[0]}")
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


def score(run: Run) -> dict:
    words = {c: len(run.transcript(c).split()) for c in run.cases}
    key = read_json(run.judge_dir / "key.json")
    stale = []
    if key["set_version"] != run.set_version:
        stale.append(f"set {key['set_version']} -> {run.set_version}")
    current_judge = (judge_model(), judge_version())
    if (key["judge_model"], key["judge_version"]) != current_judge:
        stale.append(f"judge {key['judge_model']} {key['judge_version']} -> {' '.join(current_judge)}")
    texts, sizes = {}, {}
    for n, entry in key["items"].items():
        texts[n], n_source, n_output = item(run, key, int(n), entry["call"])
        sizes[n] = (n_source, n_output)
    changed = [n for n in texts if sha256(texts[n].encode("utf-8")) != key["items"][n]["sha256"]]
    if changed:
        stale.append(f"inputs of items {', '.join(sorted(changed, key=int))}")
    if stale:
        raise JudgeError(
            f"changed since prepare: {'; '.join(stale)}; trash {run.judge_dir} and its reply folder and prepare again"
        )
    replies, problems = check_replies(reply_dir(run, key), key, sizes)
    for old in run.judge_dir.glob("retry-*.md"):
        old.unlink()
    if problems:
        for n in sorted(problems, key=int):
            print(f"item {n}: {problems[n]}")
        numbers = sorted(problems, key=int)
        batches = pack([(int(n), texts[n], sum(sizes[n])) for n in numbers])
        paths = write_batches(run.judge_dir, "retry", batches)
        raise JudgeError(
            f"{len(problems)} of {len(key['items'])} replies missing or invalid; one fresh judge per file: "
            + ", ".join(str(p) for p in paths)
        )
    per_call = {c: call_metrics(r, replies.get(c), words[run.case_of(c)]) for c, r in run.records.items()}
    per_case = {}
    for case in run.cases:
        calls = [m for c, m in per_call.items() if run.case_of(c) == case]
        per_case[case] = {m: mean([v[m] for v in calls]) for m in METRICS}
    bands: dict[str, dict] = {}
    for case, values in per_case.items():
        label = band(words[case]) or "outside bands"
        bands.setdefault(label, {})[case] = values
    summary = {
        "run_id": run.id,
        **run.fields,
        "set_version": run.set_version,
        "harness": run.spec["harness"],
        "judge_model": key["judge_model"],
        "judge_version": key["judge_version"],
        "n_cases": len(run.cases),
        "scheduled": len(run.records),
        "metrics": aggregate(per_case),
        "calls": call_stats(run),
        "bands": {b: {"n": len(v), "metrics": aggregate(v)} for b, v in sorted(bands.items())},
        "per_case": per_case,
    }
    write_text(run.judge_dir / "summary.json", json.dumps(summary, indent=2) + "\n")
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
        s = read_json(path)
        if s["set"] != "consistency":
            summaries.append(s)
    return summaries


Previous = Callable[[str], str | None]


def find_delta(s: dict, summaries: list[dict], previous: Previous) -> tuple[str, dict] | None:
    """Comparison row for s: the previous AGENTS.md change at the same cell, set version and judge version."""
    target = previous(s["prompt"])
    if target is None:
        return None
    cell = ("model", "effort", "dictionary", "set_version", "judge_version")
    for p in summaries:
        if p["prompt"] == target and all(p[k] == s[k] for k in cell):
            current = {c: v["kept"] for c, v in s["per_case"].items()}
            prior = {c: v["kept"] for c, v in p["per_case"].items()}
            m, se, deltas = paired_delta(current, prior)
            return f"{fmt(m, 1, se)} vs {p['prompt']}", deltas
    return None


COLUMNS = (
    "prompt", "model", "effort", "dictionary", "set", "n", "critical-free %", "kept %", "delta kept",
    "omissions /100", "distortions /100", "additions /case", "fillers /1k words", "wrong fixes /case",
    "missed fixes /case", "incomplete %", "flags /case", "tokens /call", "reasoning /call", "cost /call (USD)",
    "seconds /call", "judge", "harness",
)  # fmt: skip
DIGITS = {
    "critical_free": 1, "kept": 1, "omissions": 1, "distortions": 1, "additions": 2, "fillers": 1,
    "wrong_fixes": 2, "missed_fixes": 2, "incomplete": 1, "flags": 2,
}  # fmt: skip


def table_row(s: dict, delta: str) -> list[str]:
    m = {k: fmt(v[0], DIGITS[k], v[1]) for k, v in s["metrics"].items()}
    c = s["calls"]
    return [
        s["prompt"], s["model"], s["effort"], s["dictionary"], s["set_version"], str(s["n_cases"]),
        m["critical_free"], m["kept"], delta, m["omissions"], m["distortions"], m["additions"], m["fillers"],
        m["wrong_fixes"], m["missed_fixes"], m["incomplete"], m["flags"], fmt(c["tokens"], 0),
        fmt(c["reasoning"], 0), fmt(c["cost"], 4), fmt(c["seconds"], 1),
        f"{s['judge_model']} {s['judge_version']}", s["harness"],
    ]  # fmt: skip


def render_table(summaries: list[dict], previous: Previous) -> str:
    keys = [(s["prompt"], s["model"], s["effort"], s["dictionary"], s["set_version"], s["judge_version"]) for s in summaries]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise JudgeError(f"two judged runs share a results row: {sorted(dupes)}")
    order = sorted(summaries, key=lambda s: (s["set_version"], s["model"], s["effort"], s["dictionary"], s["prompt"]))
    rows = []
    for s in order:
        found = find_delta(s, summaries, previous)
        rows.append(table_row(s, found[0] if found else "n/a"))
    lines = [
        "# Results",
        "",
        "Generated by `uv run python src/judge.py score`; do not edit by hand. One row per prompt, model, effort,",
        "dictionary, set version and judge version. Values are per-case means with the standard error in brackets;",
        "rows are comparable only at the same set version and judge version. Cost is list price, a comparison measure.",
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
    parser.add_argument("--data", type=Path, default=DATA, help="data root holding cases/, sets/ and runs/")
    args = parser.parse_args()
    try:
        run = Run(args.data.resolve(), args.run)
        if args.command == "prepare":
            judged = sum(r["outcome"] in JUDGED for r in run.records.values())
            print(f"{run.id}: {len(run.records)} calls, {judged} to judge")
            paths = prepare(run)
            print(f"{len(paths)} batches in {run.judge_dir}")
            return 0
        summary = score(run)
        found = find_delta(summary, load_summaries(run.data / "runs"), previous_prompt)
        if found:
            print(f"delta kept {found[0]}")
            for case, d in found[1].items():
                print(f"  {case}  {d:+.1f}")
        m = summary["metrics"]
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
