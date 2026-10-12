import json
from pathlib import Path

import pytest

from config import Config, ConfigError, load_config
from extract import ExtractError, extract

CWD = "C:\\Work\\Cleaner"
CFG = Config(cwds=(CWD.lower(),), since=None, until=None)
START = "rollout-2026-03-04T10-20-30-0000aaaa-0000-0000-0000-000000000001.jsonl"


def meta(cwd=CWD, source="user"):
    return {"type": "session_meta", "payload": {"cwd": cwd, "thread_source": source}}


def msg(role, *texts, ts="2026-03-04T00:00:00Z"):
    kind = "output_text" if role == "assistant" else "input_text"
    content = [{"type": kind, "text": t} for t in texts]
    return {"timestamp": ts, "type": "response_item", "payload": {"type": "message", "role": role, "content": content}}


def ctx(model="model-a", effort="high"):
    return {"type": "turn_context", "payload": {"model": model, "effort": effort}}


def write_rollout(sessions: Path, name: str, lines: list[dict], day="2026/03/04") -> Path:
    path = sessions / day / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8", newline="")
    return path


def session_lines():
    return [
        meta(),
        msg("developer", "<permissions instructions> sandbox"),
        msg("user", "# AGENTS.md instructions for the folder", "<environment_context> cwd </environment_context>"),
        ctx("model-a", "high"),
        msg("user", "please move the blue box to the left shelf", ts="2026-03-04T00:01:00Z"),
        msg("assistant", "Suspect transcription issues:\n\n1. blue box"),
        ctx("model-b", "low"),
        msg("user", "1 correct"),
        msg("assistant", "```text\nPlease move the blue box to the left shelf.\n```"),
        msg("user", "not a prompt: keep my wording"),
        msg("assistant", "```text\nplease move the blue box to the left shelf\n```"),
        msg("user", "what time is the train tomorrow"),
        msg("assistant", "I am checking the timetable now."),
        msg("user", "um water the plants on friday"),
        msg("assistant", "```text\nWater the plants on Friday.\n```"),
        msg("user", "and also call the plumber about the"),
        msg("assistant", "Suspect transcription issues:\n\n1. about the"),
    ]


@pytest.fixture
def env(tmp_path):
    sessions = tmp_path / "codex" / "sessions"
    data = tmp_path / "data"
    write_rollout(sessions, START, session_lines())
    return sessions, data


def manifest(data: Path) -> dict[str, dict]:
    rows = [json.loads(line) for line in (data / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    return {r["case_id"]: r for r in rows}


def snapshot(data: Path) -> dict[str, bytes]:
    return {str(p.relative_to(data)): p.read_bytes() for p in data.rglob("*") if p.is_file()}


def test_extracts_episodes_into_cases(env):
    sessions, data = env
    _, skips = extract(sessions, CFG, data)
    rows = manifest(data)
    assert sorted(rows) == ["20260304T102030-01", "20260304T102030-03", "20260304T102030-04"]
    assert skips == {"episode: no clean block or suspect list": 1}

    first = rows["20260304T102030-01"]
    assert first["outcome"] == "suspect-then-clean"
    assert (first["model"], first["effort"], first["suspect_rounds"]) == ("model-b", "low", 1)
    assert first["has_feedback"] and first["cwd"] == CWD.lower() and first["started"] == "2026-03-04T00:01:00Z"
    case = data / "cases" / "20260304T102030-01"
    assert (case / "transcript.txt").read_text(encoding="utf-8") == "please move the blue box to the left shelf"
    assert (case / "output.txt").read_text(encoding="utf-8") == "Please move the blue box to the left shelf.\n"
    exchange = "--- assistant ---\nSuspect transcription issues:\n\n1. blue box\n\n--- user ---\n1 correct\n"
    assert (case / "exchange.txt").read_text(encoding="utf-8") == exchange
    assert (case / "feedback.txt").read_text(encoding="utf-8") == "--- user ---\nnot a prompt: keep my wording\n"

    assert rows["20260304T102030-03"]["outcome"] == "clean"
    assert not (data / "cases" / "20260304T102030-03" / "exchange.txt").exists()
    last = rows["20260304T102030-04"]
    assert last["outcome"] == "incomplete" and last["band"] is None
    assert not (data / "cases" / "20260304T102030-04" / "output.txt").exists()


def test_skips_other_cwds_and_subagent_threads(env):
    sessions, data = env
    write_rollout(sessions, START.replace("10-20-30", "11-00-00"), [meta(cwd="C:\\Elsewhere"), *session_lines()[1:]])
    write_rollout(sessions, START.replace("10-20-30", "12-00-00"), [meta(source="subagent"), *session_lines()[1:]])
    counts, skips = extract(sessions, CFG, data)
    assert counts["matched"] == 2 and counts["cases"] == 3
    assert skips["session: subagent thread"] == 1


def test_second_run_changes_nothing(env):
    sessions, data = env
    extract(sessions, CFG, data)
    before = snapshot(data)
    counts, _ = extract(sessions, CFG, data)
    assert snapshot(data) == before
    assert (counts["new"], counts["existing"]) == (0, 3)


def test_pruned_log_keeps_case_and_marks_missing(env):
    sessions, data = env
    extract(sessions, CFG, data)
    other = START.replace("10-20-30", "11-00-00")
    write_rollout(sessions, other, session_lines())
    (sessions / "2026/03/04" / START).unlink()
    counts, _ = extract(sessions, CFG, data)
    rows = manifest(data)
    assert counts["missing"] == 3
    assert rows["20260304T102030-01"]["missing_log"] is True
    assert (data / "cases" / "20260304T102030-01" / "transcript.txt").exists()
    assert rows["20260304T110000-01"]["missing_log"] is False


def test_case_id_collision_fails(env):
    sessions, data = env
    write_rollout(sessions, START.replace("000000000001", "000000000002"), session_lines(), day="2026/03/05")
    with pytest.raises(ExtractError, match="case id 20260304T102030-01"):
        extract(sessions, CFG, data)
    assert not data.exists()


def test_changed_text_fails_without_rewrite(env):
    sessions, data = env
    extract(sessions, CFG, data)
    transcript = data / "cases" / "20260304T102030-03" / "transcript.txt"
    transcript.write_text("edited by hand", encoding="utf-8")
    before = snapshot(data)
    with pytest.raises(ExtractError, match="20260304T102030-03/transcript.txt"):
        extract(sessions, CFG, data)
    assert snapshot(data) == before


def test_no_matching_session_fails(env):
    sessions, data = env
    with pytest.raises(ExtractError, match="no session"):
        extract(sessions, Config(cwds=("c:\\nowhere",), since=None, until=None), data)
    with pytest.raises(ExtractError, match="not found"):
        extract(sessions.parent / "missing", CFG, data)


def test_config_fails_fast(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "bench.toml")
    path = tmp_path / "bench.toml"
    path.write_text("[extract]\nsince = 2026-01-01\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="cwds"):
        load_config(path)
    path.write_text("[extract]\ncwds = ['C:\\Work\\Cleaner\\']\nuntil = 2026-02-01\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.cwds == ("c:\\work\\cleaner",) and str(cfg.until) == "2026-02-01"
