"""Runs a frozen case set through the cleaner prompt with `codex exec`, one subprocess per call (design section 4).

    uv run python src/run.py --set quick --model gpt-6-luna --effort high
    uv run python src/run.py --set full --model gpt-6-sol --effort low --prompt a1b2c3d --dictionary

--set NAME         cases from data/sets/NAME.txt, one case id per line (quick, full, consistency, or a small dev set)
--model SLUG       model passed to every call, and recorded as the call's model
--effort LEVEL     reasoning effort passed to every call
--repeats N        outputs per case (default 1; 5 for the consistency set)
--prompt REV|PATH  cleaner commit whose AGENTS.md is staged (default HEAD), or a prompt file (run label file-<sha8>)
--dictionary       also stage cleaner/dictionary.md beside the prompt
--timeout SEC      limit per codex subprocess (default 600); a timeout or non-zero exit makes the call failed

Writes the call cwd data/stage/<run_id>/ and data/runs/<run_id>/: spec.json, then <case_id>.<k>.json per call.
A rerun skips complete records (failed ones run again) and refuses when spec.json differs, naming the fields.
Exit 0 when every call has a record that is not failed; 1 otherwise or on an input error.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from config import ROOT

COUNTS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")
RECORD_FIELDS = (
    "output", "suspect_lists", "replies", "unmatched", "outcome", *COUNTS,
    "seconds", "model", "effort", "codex_version", "session_id", "started",
)  # fmt: skip
LIST_HEAD = "Suspect transcription issues:"
NONE_NEEDED = "None of these need correcting."
MAX_LISTS = 3
SLUG = re.compile(r"[\w.-]+")
ITEM = re.compile(r"\s*(\d+)[.)]\s+(.*)")
BOLD = re.compile(r"\*\*(.+?)\*\*")
FENCE_OPEN = re.compile(r"```[\w-]*")
FENCE_CLOSE = re.compile(r"```")


class RunError(Exception):
    pass


@dataclass(frozen=True)
class Case:
    case_id: str
    transcript: str
    resolution: dict | None
    words: int | None


@dataclass(frozen=True)
class Prompt:
    label: str
    text: bytes
    developer: str


@dataclass(frozen=True)
class Turn:
    thread_id: str | None
    reply: str | None
    usage: dict | None
    error: str | None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True)
    if proc.returncode:
        raise RunError(f"git {' '.join(args)} in {repo}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def load_resolution(path: Path, raw: bytes) -> dict:
    try:
        resolution = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RunError(f"{path}: not valid JSON ({exc})") from exc
    corrections = resolution.get("corrections") if isinstance(resolution, dict) else None
    accepted = resolution.get("accepted") if isinstance(resolution, dict) else None
    if (
        not isinstance(corrections, list)
        or not all(isinstance(c, dict) and isinstance(c.get("heard"), str) and isinstance(c.get("meant"), str)
                   for c in corrections)
        or not isinstance(accepted, list)
        or not all(isinstance(a, str) for a in accepted)
    ):  # fmt: skip
        raise RunError(f"{path}: expected corrections [{{heard, meant, kind}}] and accepted [str]")
    return resolution


def load_set(data: Path, name: str) -> tuple[list[Case], str]:
    """Cases of data/sets/<name>.txt and the set version <name>@<sha6> over ids, transcripts and resolutions."""
    path = data / "sets" / f"{name}.txt"
    if not path.is_file():
        raise RunError(f"set not found: {path}")
    ids = [s for line in path.read_text(encoding="utf-8").splitlines() if (s := line.strip())]
    if not ids:
        raise RunError(f"empty set: {path}")
    if len(set(ids)) != len(ids):
        raise RunError(f"duplicate case ids in {path}")
    words: dict[str, int] = {}
    manifest = data / "manifest.jsonl"
    if manifest.is_file():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                words[row["case_id"]] = row["words"]
    cases, digest = [], []
    for case_id in ids:
        folder = data / "cases" / case_id
        transcript = folder / "transcript.txt"
        if not transcript.is_file():
            raise RunError(f"{case_id}: missing {transcript}")
        resolution = folder / "resolution.json"
        if (folder / "exchange.txt").is_file() and not resolution.is_file():
            raise RunError(f"{case_id}: has a correction exchange but no resolution.json")
        t_bytes = transcript.read_bytes()
        r_bytes = resolution.read_bytes() if resolution.is_file() else None
        parsed = load_resolution(resolution, r_bytes) if r_bytes is not None else None
        cases.append(Case(case_id, t_bytes.decode("utf-8"), parsed, words.get(case_id)))
        digest.append(f"{case_id} {sha256(t_bytes)} {sha256(r_bytes) if r_bytes is not None else '-'}\n")
    return cases, f"{name}@{sha256(''.join(sorted(digest)).encode())[:6]}"


def load_prompt(cleaner: Path, prompt: str) -> Prompt:
    """AGENTS.md at a cleaner rev, or a prompt file; the developer line comes from .codex/config.toml at the rev (HEAD
    for a file)."""
    path = Path(prompt)
    is_file = path.is_file()
    commit = git(cleaner, "rev-parse", "--verify", f"{'HEAD' if is_file else prompt}^{{commit}}").decode().strip()
    text = path.read_bytes() if is_file else git(cleaner, "show", f"{commit}:AGENTS.md")
    config = tomllib.loads(git(cleaner, "show", f"{commit}:.codex/config.toml").decode("utf-8"))
    developer = config.get("developer_instructions")
    if not isinstance(developer, str) or not developer:
        raise RunError(f"no developer_instructions in cleaner .codex/config.toml at {commit[:7]}")
    return Prompt(f"file-{sha256(text)[:8]}" if is_file else commit[:7], text, developer)


def harness(root: Path) -> str:
    head = git(root, "rev-parse", "--short=7", "HEAD").decode().strip()
    return head + ("+dirty" if git(root, "status", "--porcelain", "--", "src").strip() else "")


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]|_", " ", text.lower()).split())


def matches(phrase: str, heard: str) -> bool:
    """Containment either way after lowercasing and stripping punctuation."""
    a, b = normalize(phrase), normalize(heard)
    return bool(a and b) and (a in b or b in a)


def parse_list(reply: str) -> list[tuple[int, list[str]]] | None:
    """Numbered items of a suspect list with their bold phrases (the whole item when nothing is bold); None if the
    reply is not a list."""
    text = reply.strip()
    if not text.startswith(LIST_HEAD):
        return None
    items: list[tuple[int, str]] = []
    for line in text[len(LIST_HEAD) :].splitlines():
        m = ITEM.fullmatch(line)
        if m:
            items.append((int(m[1]), m[2]))
        elif items and line.strip():
            items[-1] = (items[-1][0], f"{items[-1][1]} {line.strip()}")
    return [(n, BOLD.findall(body) or [body.strip()]) for n, body in items] or None


def answer(items: list[tuple[int, list[str]]], resolution: dict | None) -> tuple[str, list[str]]:
    """The reply to a suspect list and the phrases matching neither a correction nor an accepted phrase."""
    corrections = (resolution or {}).get("corrections", [])
    accepted = (resolution or {}).get("accepted", [])
    lines, unmatched = [], []
    for n, phrases in items:
        meant: list[str] = []
        for phrase in phrases:
            hits = [c["meant"] for c in corrections if matches(phrase, c["heard"])]
            meant += [h for h in hits if h not in meant]
            if not hits and not any(matches(phrase, a) for a in accepted):
                unmatched.append(phrase)
        if meant:
            lines.append(f"{n}. {'; '.join(meant)}")
    return ("\n".join(lines) if lines else NONE_NEEDED), unmatched


def fenced(reply: str) -> str | None:
    """Text of the reply's final complete fenced block, without the fences."""
    lines = reply.splitlines()
    blocks: list[list[str]] = []
    start = None
    for i, line in enumerate(lines):
        if start is None and FENCE_OPEN.fullmatch(line.strip()):
            start = i
        elif start is not None and FENCE_CLOSE.fullmatch(line.strip()):
            blocks.append(lines[start + 1 : i])
            start = None
    return "\n".join(blocks[-1]) if blocks else None


def usage_totals(usage: dict | None) -> dict | None:
    """Cumulative session totals of a turn.completed event; an all-zero event is missing usage (None)."""
    if not usage:
        return None
    totals = {k: usage.get(k) for k in COUNTS}
    return totals if any(totals.values()) else None


def turn_costs(totals: list[dict | None]) -> list[dict | None]:
    """Per-turn cost: each turn's cumulative totals minus the previous turn's; None where either is missing."""
    costs: list[dict | None] = []
    prev: dict | None = dict.fromkeys(COUNTS, 0)
    for t in totals:
        if t is None or prev is None:
            costs.append(None)
        else:
            costs.append({k: None if t[k] is None or prev[k] is None else t[k] - prev[k] for k in COUNTS})
        prev = t
    return costs


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], check=False, capture_output=True)
    else:
        proc.kill()


def run_turn(cmd: list[str], text: str, cwd: Path, timeout: float) -> Turn:
    """One codex subprocess with `text` as the prompt on stdin, which is then closed (no command-line length limit).
    `error` events are transient and ignored; a timeout, non-zero exit or missing turn.completed is an error."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(text.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate()
        return Turn(None, None, None, f"timeout after {timeout:g} s")
    except BaseException:
        kill_tree(proc)
        proc.wait()
        raise
    thread_id = reply = usage = failed = None
    completed = False
    for line in out.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type", item.get("item_type")) in ("agent_message", "assistant_message"):
                reply = item.get("text")
        elif kind == "turn.completed":
            usage = event.get("usage")
            completed = True
        elif kind == "turn.failed":
            failed = (event.get("error") or {}).get("message", "turn.failed")
    error = None
    if proc.returncode:
        error = f"exit {proc.returncode}: {err.decode('utf-8', errors='replace').strip()[-500:]}"
    elif failed:
        error = f"turn failed: {failed}"
    elif reply is None:
        error = "no agent message in the event stream"
    elif not completed:
        error = "no turn.completed in the event stream"
    return Turn(thread_id, reply, usage, error)


def run_call(case: Case, codex: list[str], stage: Path, prompt: Prompt, args: argparse.Namespace, version: str) -> dict:
    base = [
        *codex, "exec", "--json", "--skip-git-repo-check",
        "--model", args.model,
        "-c", f"model_reasoning_effort={json.dumps(args.effort)}",
        "-c", f"developer_instructions={json.dumps(prompt.developer)}",
        "-c", 'approval_policy="never"',
        "-c", 'windows.sandbox="unelevated"',
        "--sandbox", "read-only",
    ]  # fmt: skip
    started = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    t0 = time.monotonic()
    turns = [run_turn([*base, "-"], case.transcript, stage, args.timeout)]
    lists: list[str] = []
    replies: list[str] = []
    unmatched: list[str] = []
    output = error = None
    while True:
        turn = turns[-1]
        if turn.error or turn.reply is None:
            outcome, error = "failed", turn.error
            break
        items = parse_list(turn.reply)
        if items is None:
            output = fenced(turn.reply)
            outcome = "other" if output is None else ("suspect-then-clean" if lists else "clean")
            break
        lists.append(turn.reply)
        reply, missed = answer(items, case.resolution)
        unmatched += missed
        if len(lists) > MAX_LISTS:
            outcome = "other"
            break
        if turns[0].thread_id is None:
            outcome, error = "failed", "suspect list but no thread id to resume"
            break
        replies.append(reply)
        turns.append(run_turn([*base, "resume", turns[0].thread_id, "-"], reply, stage, args.timeout))
    totals = [usage_totals(t.usage) for t in turns]
    record = {
        "output": output,
        "suspect_lists": lists,
        "replies": replies,
        "unmatched": unmatched,
        "outcome": outcome,
        **(totals[-1] or dict.fromkeys(COUNTS)),
        "turn_usage": turn_costs(totals),
        "seconds": round(time.monotonic() - t0, 1),
        "model": args.model,
        "effort": args.effort,
        "codex_version": version,
        "session_id": turns[0].thread_id,
        "started": started,
    }
    if error:
        record["error"] = error
    return record


def complete(path: Path) -> bool:
    if not path.is_file():
        return False
    record = json.loads(path.read_text(encoding="utf-8"))
    return record.get("outcome") != "failed" and all(k in record for k in RECORD_FIELDS)


def write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def check_spec(path: Path, spec: dict) -> None:
    if not path.is_file():
        return
    old = json.loads(path.read_text(encoding="utf-8"))
    diff = [f"{k} ({old.get(k)!r} -> {spec.get(k)!r})" for k in sorted(old.keys() | spec.keys()) if old.get(k) != spec.get(k)]
    if diff:
        raise RunError(
            f"spec differs from {path}: {', '.join(diff)}; move {path.parent} to the trash before rerunning"
        )


def codex_version(codex: list[str]) -> str:
    cmd = [*codex, "--version"]
    proc = subprocess.run(cmd, check=False, capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    text = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode or not text:
        raise RunError(f"codex --version failed: {proc.stderr.decode(errors='replace').strip()}")
    return text.split()[-1]


def run(args: argparse.Namespace, root: Path, codex: list[str]) -> int:
    data, cleaner = root / "data", root / "cleaner"
    for name, value in (("set", args.set), ("model", args.model), ("effort", args.effort)):
        if not SLUG.fullmatch(value):
            raise RunError(f"--{name} must match {SLUG.pattern}, got {value!r}")
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
    print(f"codex home {home}")
    cases, set_version = load_set(data, args.set)
    prompt = load_prompt(cleaner, args.prompt)
    dictionary = None
    if args.dictionary:
        dict_path = cleaner / "dictionary.md"
        if not dict_path.is_file():
            raise RunError(f"--dictionary: missing {dict_path}")
        dictionary = dict_path.read_bytes()
    repeats = args.repeats or (5 if args.set == "consistency" else 1)
    version = codex_version(codex)
    dict_hash = sha256(dictionary) if dictionary is not None else None
    spec = {
        "prompt": prompt.label,
        "prompt_sha256": sha256(prompt.text),
        "developer_sha256": sha256(prompt.developer.encode("utf-8")),
        "dictionary": dict_hash or "off",
        "set_version": set_version,
        "repeats": repeats,
        "codex_version": version,
        "codex_home": str(home),
        "harness": harness(root),
    }
    run_id = f"{prompt.label}_{args.model}_{args.effort}_{args.set}_{f'd{dict_hash[:6]}' if dict_hash else 'off'}"
    run_dir = data / "runs" / run_id
    check_spec(run_dir / "spec.json", spec)

    stage = data / "stage" / run_id
    (stage / ".git").mkdir(parents=True, exist_ok=True)
    (stage / "AGENTS.md").write_bytes(prompt.text)
    if dictionary is not None:
        (stage / "dictionary.md").write_bytes(dictionary)

    calls = [(case, k) for case in cases for k in range(1, repeats + 1)]
    todo = [(case, k) for case, k in calls if not complete(run_dir / f"{case.case_id}.{k}.json")]
    print(f"{run_id}: {len(cases)} cases x {repeats} = {len(calls)} calls, {len(calls) - len(todo)} done, {len(todo)} to run")
    for case, k in todo:
        print(f"{case.case_id}.{k} ({case.words if case.words is not None else '?'} words) ...", end="", flush=True)
        if not (run_dir / "spec.json").is_file():
            run_dir.mkdir(parents=True, exist_ok=True)
            write_json(run_dir / "spec.json", spec)
        record = run_call(case, codex, stage, prompt, args, version)
        write_json(run_dir / f"{case.case_id}.{k}.json", record)
        print(f" {record['outcome']} {record['seconds']:.1f} s")
        if record.get("error"):
            print(f"{case.case_id}.{k}: {record['error']}", file=sys.stderr)

    records = {}
    for case, k in calls:
        path = run_dir / f"{case.case_id}.{k}.json"
        if path.is_file():
            records[f"{case.case_id}.{k}"] = json.loads(path.read_text(encoding="utf-8"))
    outcomes = Counter(r["outcome"] for r in records.values())
    missing = sum(1 for r in records.values() if r["input_tokens"] is None)
    unmatched = [(key, p) for key, r in records.items() for p in r["unmatched"]]
    seconds = sum(r["seconds"] for r in records.values())
    print(
        f"summary: {len(records)}/{len(calls)} records; "
        + ", ".join(f"{o} {n}" for o, n in sorted(outcomes.items()))
        + f"; {seconds:.1f} s; missing usage {missing}; unmatched {len(unmatched)}"
    )
    for key, phrase in unmatched:
        print(f"unmatched {key}: {phrase}")
    return 0 if len(records) == len(calls) and not outcomes["failed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a case set through the cleaner prompt with codex exec.")
    parser.add_argument("--set", required=True, help="data/sets/NAME.txt")
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--repeats", type=int, help="outputs per case (default 1; 5 for consistency)")
    parser.add_argument("--prompt", default="HEAD", help="cleaner rev or prompt file (default HEAD)")
    parser.add_argument("--dictionary", action="store_true", help="stage cleaner/dictionary.md")
    parser.add_argument("--timeout", type=float, default=600, help="seconds per codex subprocess (default 600)")
    args = parser.parse_args(argv)
    if args.repeats is not None and args.repeats < 1:
        parser.error("--repeats must be at least 1")
    codex = shutil.which("codex")
    try:
        if codex is None:
            raise RunError("codex not found on PATH")
        return run(args, ROOT, [codex])
    except RunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
