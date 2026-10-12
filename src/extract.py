"""Extract dictation cases from Codex rollout logs into data/cases/ and data/manifest.jsonl.

Usage:
    uv run python src/extract.py
        Read every rollout under $CODEX_HOME/sessions (default ~/.codex/sessions) whose cwd is listed in
        bench.toml, add new cases, and mark cases whose rollout is gone as missing_log. Existing case files
        are never rewritten or deleted; a case whose extracted text differs from disk is an error.
    uv run python src/extract.py --legacy data/legacy/tests
        Also match cases by content against the old raw/ and baseline/ files and record each old number
        as legacy_id in the manifest. Fails if any old case has no match.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from config import ROOT, Config, ConfigError, load_config

DATA = ROOT / "data"
FEEDBACK = ("not a prompt:", "so now re clean")
WRAPPERS = (
    "<user_instructions",
    "<environment_context",
    "<external_codex_apps_open_page",
    "<recommended_plugins",
    "<subagent_notification",
    "<turn_aborted",
    "# AGENTS.md instructions",
)
SUSPECT = "Suspect transcription issues"
FENCE = re.compile(r"```text\n(.*?)\n?```", re.DOTALL)
NAME = re.compile(r"rollout-(\d{4})-(\d\d)-(\d\d)T(\d\d)-(\d\d)-(\d\d)-.+\.jsonl")
BANDS = ((40, 199), (200, 799), (800, 1499), (1500, 3000))
CASE_FILES = ("transcript.txt", "output.txt", "exchange.txt", "feedback.txt")


class ExtractError(Exception):
    pass


@dataclass
class Episode:
    transcript: str
    started: str
    exchange: list[tuple[str, str]] = field(default_factory=list)
    feedback: list[str] = field(default_factory=list)
    output: str | None = None
    suspect_rounds: int = 0
    model: str | None = None
    effort: str | None = None


def codex_sessions() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "sessions"


def session_start(name: str) -> str:
    m = NAME.fullmatch(name)
    if not m:
        raise ExtractError(f"unexpected rollout file name: {name}")
    y, mo, d, h, mi, s = m.groups()
    return f"{y}{mo}{d}T{h}{mi}{s}"


def episodes(path: Path) -> list[Episode]:
    """Split one rollout into episodes by payload type and role; a session_meta line is required first."""
    eps: list[Episode] = []
    cur: Episode | None = None
    state = "idle"
    model = effort = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            p = d.get("payload") or {}
            if d.get("type") == "turn_context":
                model, effort = p.get("model"), p.get("effort")
                continue
            if d.get("type") != "response_item" or p.get("type") != "message":
                continue
            role = p.get("role")
            text = "".join(c.get("text", "") for c in p.get("content", []))
            if role == "user":
                if text.lstrip().startswith(WRAPPERS):
                    continue
                if cur and state in ("clean", "feedback") and text.lower().startswith(FEEDBACK):
                    cur.feedback.append(text)
                    state = "feedback"
                elif cur and state == "suspect":
                    cur.exchange.append(("user", text))
                    state = "corrected"
                else:
                    cur = Episode(transcript=text, started=d.get("timestamp", ""))
                    eps.append(cur)
                    state = "dictated"
            elif role == "assistant" and cur:
                if state in ("dictated", "suspect", "corrected") and text.startswith(SUSPECT):
                    cur.exchange.append(("assistant", text))
                    cur.suspect_rounds += 1
                    cur.model, cur.effort = model, effort
                    state = "suspect"
                elif state in ("dictated", "corrected") and (m := FENCE.match(text)):
                    cur.output = m.group(1) + "\n"
                    cur.model, cur.effort = model, effort
                    state = "clean"
    return eps


def band(words: int) -> str | None:
    for lo, hi in BANDS:
        if lo <= words <= hi:
            return f"{lo}-{hi}"
    return None


def case_files(ep: Episode) -> dict[str, str | None]:
    exchange = "\n".join(f"--- {r} ---\n{t.rstrip()}\n" for r, t in ep.exchange)
    feedback = "\n".join(f"--- user ---\n{t.rstrip()}\n" for t in ep.feedback)
    return {
        "transcript.txt": ep.transcript,
        "output.txt": ep.output,
        "exchange.txt": exchange or None,
        "feedback.txt": feedback or None,
    }


def record(case_id: str, rollout: str, cwd: str, ep: Episode) -> dict:
    words = len(ep.transcript.split())
    if ep.output is None:
        outcome = "incomplete"
    else:
        outcome = "suspect-then-clean" if ep.suspect_rounds else "clean"
    return {
        "case_id": case_id,
        "rollout": rollout,
        "cwd": cwd,
        "started": ep.started,
        "model": ep.model,
        "effort": ep.effort,
        "words": words,
        "band": band(words),
        "outcome": outcome,
        "suspect_rounds": ep.suspect_rounds,
        "has_feedback": bool(ep.feedback),
        "legacy_id": None,
        "missing_log": False,
    }


def read_manifest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    return {r["case_id"]: r for r in rows}


def write_if_changed(path: Path, text: str) -> None:
    if path.exists() and path.read_bytes() == text.encode("utf-8"):
        return
    path.write_text(text, encoding="utf-8", newline="")


def check_case(case_dir: Path, files: dict[str, str | None]) -> None:
    for name, expected in files.items():
        f = case_dir / name
        on_disk = f.read_bytes().decode("utf-8") if f.exists() else None
        if on_disk != expected:
            raise ExtractError(f"{case_dir.name}/{name}: extracted text differs from the file on disk")


def map_legacy(manifest: dict[str, dict], content: dict[str, tuple[str, str | None]], legacy: Path) -> int:
    by_content: dict[tuple[str, str | None], list[str]] = {}
    for cid, key in content.items():
        by_content.setdefault(key, []).append(cid)
    raws = sorted((legacy / "raw").glob("*.txt"))
    if not raws:
        raise ExtractError(f"no legacy raw files under {legacy}")
    for raw in raws:
        number = int(raw.name.split("_")[0])
        key = (raw.read_bytes().decode("utf-8"), (legacy / "baseline" / raw.name).read_bytes().decode("utf-8"))
        hits = by_content.get(key, [])
        if len(hits) != 1:
            raise ExtractError(f"legacy case {raw.name} matches {len(hits)} cases by content")
        existing = manifest[hits[0]]["legacy_id"]
        if existing not in (None, number):
            raise ExtractError(f"{hits[0]} already has legacy_id {existing}, content matches {number}")
        manifest[hits[0]]["legacy_id"] = number
    return len(raws)


def in_dates(start: str, cfg: Config) -> bool:
    day = dt.date(int(start[:4]), int(start[4:6]), int(start[6:8]))
    return (cfg.since is None or day >= cfg.since) and (cfg.until is None or day <= cfg.until)


def extract(
    sessions: Path, cfg: Config, data: Path, legacy: Path | None = None
) -> tuple[dict[str, int], dict[str, int]]:
    """Merge cases from sessions into data/; returns counts and skips by reason for the summary."""
    if not sessions.is_dir():
        raise ExtractError(f"sessions folder not found: {sessions}")
    cases = data / "cases"
    manifest_path = data / "manifest.jsonl"
    manifest = read_manifest(manifest_path)
    rollouts = sorted(sessions.rglob("rollout-*.jsonl"), key=lambda f: f.name)
    on_disk = {f.name for f in rollouts}
    counts = {"matched": 0, "episodes": 0, "new": 0, "existing": 0, "missing": 0, "legacy": 0}
    skips: dict[str, int] = {}
    seen: dict[str, str] = {}
    content: dict[str, tuple[str, str | None]] = {}
    pending: list[tuple[Path, dict[str, str | None]]] = []

    def skip(reason: str) -> None:
        skips[reason] = skips.get(reason, 0) + 1

    for f in rollouts:
        with f.open(encoding="utf-8") as fh:
            first = json.loads(fh.readline())
        if first.get("type") != "session_meta":
            raise ExtractError(f"{f.name}: first line is not session_meta")
        meta = first["payload"]
        cwd = meta["cwd"].lower().rstrip("\\/")
        start = session_start(f.name)
        if cwd not in cfg.cwds or not in_dates(start, cfg):
            continue
        counts["matched"] += 1
        if meta.get("thread_source") != "user":
            skip(f"session: {meta.get('thread_source')} thread")
            continue
        for n, ep in enumerate(episodes(f), 1):
            counts["episodes"] += 1
            if ep.output is None and not ep.exchange:
                skip("episode: no clean block or suspect list")
                continue
            case_id = f"{start}-{n:02d}"
            if case_id in seen:
                raise ExtractError(f"case id {case_id} from both {seen[case_id]} and {f.name}")
            seen[case_id] = f.name
            files = case_files(ep)
            content[case_id] = (ep.transcript, ep.output)
            case_dir = cases / case_id
            if case_id in manifest:
                if manifest[case_id]["rollout"] != f.name:
                    raise ExtractError(f"case id {case_id} is recorded from {manifest[case_id]['rollout']}, not {f.name}")
                check_case(case_dir, files)
                manifest[case_id]["missing_log"] = False
                counts["existing"] += 1
                continue
            if case_dir.exists():
                raise ExtractError(f"{case_dir} exists but has no manifest record")
            pending.append((case_dir, files))
            manifest[case_id] = record(case_id, f.name, cwd, ep)
            counts["new"] += 1
    if counts["matched"] == 0:
        raise ExtractError(f"no session under {sessions} matches the cwds and dates in bench.toml")
    for cid, row in manifest.items():
        if row["rollout"] not in on_disk:
            row["missing_log"] = True
            counts["missing"] += 1
            print(f"WARNING: {cid}: rollout {row['rollout']} is no longer on disk", file=sys.stderr)
    if legacy is not None:
        counts["legacy"] = map_legacy(manifest, content, legacy)
    for case_dir, files in pending:
        case_dir.mkdir(parents=True)
        for name, text in files.items():
            if text:
                (case_dir / name).write_text(text, encoding="utf-8", newline="")
    data.mkdir(parents=True, exist_ok=True)
    write_if_changed(manifest_path, "".join(json.dumps(manifest[c], ensure_ascii=False) + "\n" for c in sorted(manifest)))
    counts["cases"] = len(manifest)
    return counts, skips


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--legacy", type=Path, help="old tests folder holding raw/ and baseline/")
    args = parser.parse_args()
    sessions = codex_sessions()
    print(f"sessions root: {sessions}")
    try:
        c, skipped = extract(sessions, load_config(), DATA, args.legacy)
    except (ConfigError, ExtractError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    skips = ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())) or "none"
    print(f"sessions matched {c['matched']}, episodes {c['episodes']}, skipped: {skips}")
    print(f"cases {c['cases']}: new {c['new']}, unchanged {c['existing']}, missing logs {c['missing']}")
    if args.legacy:
        print(f"legacy cases matched {c['legacy']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
