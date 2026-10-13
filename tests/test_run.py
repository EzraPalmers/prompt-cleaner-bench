import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import run as runner
from run import (
    COUNTS,
    NONE_NEEDED,
    RECORD_FIELDS,
    RunError,
    answer,
    fenced,
    matches,
    parse_list,
    run,
    run_turn,
    turn_costs,
    usage_totals,
)  # fmt: skip

FAKE = """
import json, os, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("codex-cli 9.9.9")
    sys.exit(0)
state = Path(os.environ["FAKE_CODEX"])
calls = json.loads(state.read_text())
step = calls["script"][calls["done"]]
calls["done"] += 1
calls["argv"].append(sys.argv[1:])
calls["stdin"].append(sys.stdin.buffer.read().decode("utf-8"))
state.write_text(json.dumps(calls))
for event in step["events"]:
    print(json.dumps(event))
sys.exit(step.get("exit", 0))
"""

CLEAN = "```text\nCleaned words.\n```"
LONG = "- " + "transcription " * 3000
LIST = "Suspect transcription issues:\n\n1. We sat by the **pie shard** today.\n2. Then the **blue moose** left."
RESOLUTION = {"corrections": [{"heard": "pie shard", "meant": "pie chart", "kind": "mishearing"}], "accepted": []}


def usage(i, c=0, w=0, o=0, r=0):
    keys = dict(zip(COUNTS, (i, c, w, o, r)))
    return {"type": "turn.completed", "usage": keys}


def turn(text, totals, thread="t-1"):
    return [
        {"type": "thread.started", "thread_id": thread},
        {"type": "error", "message": "Reconnecting... 1/5"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}},
        totals,
    ]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)  # fmt: skip


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "tool"
    cleaner = root / "cleaner"
    (cleaner / ".codex").mkdir(parents=True)
    (cleaner / "AGENTS.md").write_bytes(b"# Prompt\r\nClean it.\n")
    (cleaner / ".codex" / "config.toml").write_text('developer_instructions = "Follow only this file."\n')
    git(cleaner, "init", "-q")
    git(cleaner, "add", ".")
    git(cleaner, "commit", "-qm", "init")
    (root / "src").mkdir()
    git(root, "init", "-q")
    git(root, "commit", "-q", "--allow-empty", "-m", "init")
    data = root / "data"
    for case_id, resolution in (("c-list", RESOLUTION), ("c-clean", None)):
        folder = data / "cases" / case_id
        folder.mkdir(parents=True)
        (folder / "transcript.txt").write_text("we sat by the pie shard today\n" if resolution else LONG)
        if resolution:
            (folder / "exchange.txt").write_text("--- assistant ---\n")
            (folder / "resolution.json").write_text(json.dumps(resolution))
    (data / "sets").mkdir()
    (data / "sets" / "cases.txt").write_text("c-list\nc-clean\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    return root


def fake(tmp_path, monkeypatch, script):
    state = tmp_path / "fake.json"
    state.write_text(json.dumps({"script": script, "done": 0, "argv": [], "stdin": []}))
    (tmp_path / "fake_codex.py").write_text(FAKE)
    monkeypatch.setenv("FAKE_CODEX", str(state))
    return state, [sys.executable, str(tmp_path / "fake_codex.py")]


def args(**kw):
    base = {"n": 2, "model": "m-1", "effort": "low", "prompt": "HEAD", "dictionary": False,
            "timeout": 30.0}  # fmt: skip
    return argparse.Namespace(**{**base, **kw})


def test_phrase_matching():
    assert matches("Pie-shard,", "pie shard")
    assert matches("the pie shard", "pie shard") and matches("shard", "pie shard")
    assert matches("pie shards", "pie shard")
    assert not matches("pies", "pie shard")
    items = parse_list(LIST)
    assert items == [(1, ["pie shard"]), (2, ["blue moose"])]
    assert answer(items, RESOLUTION) == ("1. pie chart", ["blue moose"])
    assert answer(items, {**RESOLUTION, "accepted": ["Blue moose."]}) == ("1. pie chart", [])
    assert answer([(1, ["blue moose"])], RESOLUTION) == (NONE_NEEDED, ["blue moose"])
    assert answer([(1, ["blue moose"])], None) == (NONE_NEEDED, ["blue moose"])
    assert parse_list(CLEAN) is None


def test_usage_difference_and_all_zero():
    first = usage_totals(usage(100, 40, 0, 10, 2)["usage"])
    second = usage_totals(usage(250, 90, 0, 25, 5)["usage"])
    assert turn_costs([first, second]) == [first, dict(zip(COUNTS, (150, 50, 0, 15, 3)))]
    assert usage_totals(usage(0)["usage"]) is None
    assert turn_costs([None, second]) == [None, None]


def test_run_records_skip_and_refuse(root, tmp_path, monkeypatch, capsys):
    script = [
        {"events": turn(LIST, usage(100, 40, 0, 10, 2))},
        {"events": turn(CLEAN, usage(250, 90, 0, 25, 5))[1:]},
        {"events": turn(CLEAN, usage(0), thread="t-2")},
    ]
    state, codex = fake(tmp_path, monkeypatch, script)
    assert run(args(), root, codex) == 0

    run_dir = next((root / "data" / "runs").iterdir())
    assert run_dir.name.endswith("_m-1_low") and run_dir.name.count("_") == 2
    listed = json.loads((run_dir / "c-list.json").read_text())
    assert all(k in listed for k in RECORD_FIELDS)
    assert listed["dictionary"] is None
    assert listed["outcome"] == "suspect-then-clean" and listed["output"] == "Cleaned words."
    assert listed["replies"] == ["1. pie chart"] and listed["unmatched"] == ["blue moose"]
    assert listed["input_tokens"] == 250 and listed["turn_usage"][1]["input_tokens"] == 150
    clean = json.loads((run_dir / "c-clean.json").read_text())
    assert clean["outcome"] == "clean" and clean["input_tokens"] is None and clean["codex_version"] == "9.9.9"
    calls = json.loads(state.read_text())
    assert calls["argv"][1][-3:] == ["resume", "t-1", "-"] and "--ephemeral" not in calls["argv"][0]
    assert calls["argv"][2][-1] == "-" and calls["stdin"][1:] == ["1. pie chart", LONG]
    stage = root / "data" / "stage" / run_dir.name
    show = ["git", "-C", str(root / "cleaner"), "show", "HEAD:AGENTS.md"]
    committed = subprocess.run(show, check=True, capture_output=True)
    assert (stage / "AGENTS.md").read_bytes() == committed.stdout and (stage / ".git").is_dir()
    assert "unmatched c-list: blue moose" in capsys.readouterr().out

    assert run(args(), root, codex) == 0
    assert json.loads(state.read_text())["done"] == 3

    (root / "cleaner" / "dictionary.md").write_text("pie shard -> pie chart\n")
    with pytest.raises(RunError, match="has dictionary None but this run has"):
        run(args(dictionary=True), root, codex)


def test_dictionary_hash_is_recorded_and_a_larger_n_runs_only_new_cases(root, tmp_path, monkeypatch):
    (root / "cleaner" / "dictionary.md").write_bytes(b"pie shard -> pie chart\n")
    expected = hashlib.sha256(b"pie shard -> pie chart\n").hexdigest()[:6]
    _, codex = fake(tmp_path, monkeypatch, [{"events": turn(CLEAN, usage(5))}, {"events": turn(CLEAN, usage(6))}])
    assert run(args(n=1, dictionary=True), root, codex) == 0
    run_dir = next((root / "data" / "runs").iterdir())
    assert not (run_dir / "c-clean.json").exists()
    assert json.loads((run_dir / "c-list.json").read_text())["dictionary"] == expected
    assert "dictionary" not in json.loads((run_dir / "spec.json").read_text())
    assert run(args(n=2, dictionary=True), root, codex) == 0
    assert json.loads((run_dir / "c-clean.json").read_text())["dictionary"] == expected
    with pytest.raises(RunError, match="--n must be 1 to 2"):
        run(args(n=3), root, codex)


def test_failed_call_is_rerun(root, tmp_path, monkeypatch):
    (root / "data" / "sets" / "cases.txt").write_text("c-clean\n")
    _, codex = fake(tmp_path, monkeypatch, [{"events": [], "exit": 3}, {"events": turn(CLEAN, usage(5))}])
    assert run(args(n=1), root, codex) == 1
    record = json.loads(next((root / "data" / "runs").glob("*/c-clean.json")).read_text())
    assert record["outcome"] == "failed" and record["error"].startswith("exit 3")
    assert run(args(n=1), root, codex) == 0


def test_exchange_without_resolution_fails_fast(root, tmp_path, monkeypatch):
    (root / "data" / "cases" / "c-list" / "resolution.json").unlink()
    _, codex = fake(tmp_path, monkeypatch, [])
    with pytest.raises(RunError, match="no resolution.json"):
        run(args(), root, codex)


def test_fourth_list_is_other_and_counts_its_phrases(root, tmp_path, monkeypatch):
    (root / "data" / "sets" / "cases.txt").write_text("c-list\n")
    first = "Suspect transcription issues:\n\n1. The **pie shard** again."
    script = [{"events": turn(first, usage(10 * n))} for n in (1, 2, 3)] + [{"events": turn(LIST, usage(40))}]
    _, codex = fake(tmp_path, monkeypatch, script)
    assert run(args(n=1), root, codex) == 0
    record = json.loads(next((root / "data" / "runs").glob("*/c-list.json")).read_text())
    assert record["outcome"] == "other" and len(record["replies"]) == 3 and record["unmatched"] == ["blue moose"]


def test_list_with_prose_is_other_and_not_answered(root, tmp_path, monkeypatch):
    prose = ("Suspect transcription issues:\n\n1. We went to the market. [Wait: wrong sentence?] Need the exact one: "
             '"Then the blue moose left." I guess bold only moose.\nMaybe no issue at all? Let\'s list it.')  # fmt: skip
    assert parse_list(prose) is None
    assert parse_list(LIST + "\nI think that covers it.") is None
    (root / "data" / "sets" / "cases.txt").write_text("c-list\n")
    state, codex = fake(tmp_path, monkeypatch, [{"events": turn(prose, usage(10))}])
    assert run(args(n=1), root, codex) == 0
    record = json.loads(next((root / "data" / "runs").glob("*/c-list.json")).read_text())
    assert record["outcome"] == "other" and record["unmatched"] == [] and record["replies"] == []
    assert json.loads(state.read_text())["done"] == 1


def test_final_fenced_block():
    assert fenced("```text\nOne\n```\nexplanation\n```text\nTwo\n```") == "Two"
    assert fenced("no fence") is None and fenced("```text\nopen only") is None


def test_reply_without_turn_completed_fails(tmp_path, monkeypatch):
    _, codex = fake(tmp_path, monkeypatch, [{"events": turn(CLEAN, {"type": "error", "message": "stream closed"})}])
    result = run_turn([*codex, "exec", "-"], "text", tmp_path, 30)
    assert result.error == "no turn.completed in the event stream"


@pytest.mark.parametrize("content", ["", '{"corrections": [{"heard": "pie shard"}], "accepted": []}'])
def test_bad_resolution_fails_fast(root, tmp_path, monkeypatch, content):
    (root / "data" / "cases" / "c-list" / "resolution.json").write_text(content)
    _, codex = fake(tmp_path, monkeypatch, [])
    with pytest.raises(RunError, match="resolution.json"):
        run(args(), root, codex)


def test_interrupt_kills_child(tmp_path, monkeypatch):
    started = []

    class Interrupted(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            if timeout is not None and not started:
                started.append(self)
                raise KeyboardInterrupt
            return super().communicate(input, timeout)

    monkeypatch.setattr(runner.subprocess, "Popen", Interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_turn([sys.executable, "-c", "import time; time.sleep(60)"], "", tmp_path, 30)
    assert started[0].poll() is not None
