import json
import shutil
from pathlib import Path

import pytest

import judge
from judge import JudgeError, Run, filler_count, paired_delta, regex_fillers, split_sentences, validate

FIXTURE = Path(__file__).parent / "fixtures" / "judge"
RUN_ID = "a1b2c3d_gpt-6-luna_high_quick_off"
CASES = [f"20990101T090000-0{i}" for i in range(1, 5)]
NONE = lambda prompt: None


@pytest.fixture
def data(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "data"
    shutil.copytree(FIXTURE, root)
    monkeypatch.setattr(judge, "RESULTS", tmp_path / "results.md")
    monkeypatch.setattr(judge, "judge_version", lambda: "abc1234")
    return root


def reply(n_source: int, n_output: int) -> dict:
    return {
        "source": [
            {"id": i, "kind": "content", "present": "yes", "missing": [], "hedges": "na", "mood": "yes",
             "roles": "na", "alternatives": "na", "output_ids": [min(i, n_output)], "note": ""}
            for i in range(1, n_source + 1)
        ],
        "output": [{"id": i, "sourced": "yes", "added": [], "note": ""} for i in range(1, n_output + 1)],
        "fillers_left": [], "stubs_left": [], "wrong_fixes": [], "missed_fixes": [],
    }  # fmt: skip


def answer_all(run: Run, edit=lambda case, r: r) -> None:
    key = json.loads((run.judge_dir / "key.json").read_text())
    for n, item in key["items"].items():
        _, source, output = judge.item_parts(run, item["case"])
        path = judge.reply_dir(run, key) / f"{n}.json"
        path.write_text(json.dumps(edit(item["case"], reply(len(source), len(output)))), encoding="utf-8")


def edit_record(data: Path, case: str, **fields) -> None:
    path = data / "runs" / RUN_ID / f"{case}.json"
    record = json.loads(path.read_text())
    record.update(fields)
    path.write_text(json.dumps(record))


def test_split_run_on_is_one_item():
    text = "so I was thinking we move the button and then maybe change the colour and also the font"
    assert split_sentences(text) == [text]


def test_split_keeps_abbreviations_and_splits_sentences():
    text = "Use a format, e.g. JSON or YAML. Ask Dr. Lee about it! Is it done? Fine... and then 3 more."
    assert split_sentences(text) == [
        "Use a format, e.g. JSON or YAML.",
        "Ask Dr. Lee about it!",
        "Is it done?",
        "Fine... and then 3 more.",
    ]


def test_split_ignores_capitals_and_line_wrapping_but_not_paragraphs():
    assert split_sentences("Check the latch. then paint it.") == ["Check the latch.", "then paint it."]
    assert split_sentences("Check the\nlatch.") == ["Check the latch."]
    assert split_sentences("- first item\n\n- second item.") == ["- first item", "- second item."]


def test_regex_fillers():
    text = "Okay, um, the build, like, fails. Do you know why? It is slow, you know. Uh, err, fine. I like it."
    # okay (opener), um, ", like,", "you know" at clause end, uh, err; not "Do you know" or "I like it"
    assert sum(sum(regex_fillers(s).values()) for s in split_sentences(text)) == 6


def test_judge_filler_is_not_counted_twice_but_other_occurrences_are():
    like = [{"output_id": 1, "token": "like"}]
    assert filler_count(["It is, like, broken."], like) == 1
    assert filler_count(["It is, like, broken and like slow."], like) == 2
    assert filler_count(["Yeah it is kind of slow."], [{"output_id": 1, "token": "yeah"}]) == 1
    assert filler_count(["It is kind of slow."], [{"output_id": 1, "token": "kind of"}]) == 1


def test_validate_accepts_valid_reply():
    assert validate(reply(3, 2), 3, 2) == []
    dropped = reply(3, 2)
    dropped["source"][2].update(present="no", missing=["the whole sentence"], mood="na", output_ids=[])
    assert validate(dropped, 3, 2) == []


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda r: r.pop("stubs_left"), "top-level keys"),
        (lambda r: r["source"].pop(), "expected 3 entries"),
        (lambda r: r["source"][0].update(mood="maybe"), "mood"),
        (lambda r: r["source"][0].update(mood=[]), "mood []"),
        (lambda r: r["output"][0].update(sourced={}), "sourced {}"),
        (lambda r: r["source"][0].update(present="no"), "missing must be non-empty"),
        (lambda r: r["source"][0].update(missing=[""]), "non-empty strings"),
        (lambda r: r["source"][1].update(output_ids=[9]), "output_ids"),
        (lambda r: r["source"][2].update(id=7), "id 7"),
        (lambda r: r["source"][0].update(kind="noise", hedges="yes"), "noise"),
        (lambda r: r["source"][0].update(kind="noise", mood="na"), "noise"),
        (lambda r: r["source"][0].update(output_ids=[]), "no output carries"),
        (lambda r: r["source"][0].update(present="no", missing=["x"], output_ids=[], hedges="yes"), "no output"),
        (lambda r: r["source"][0].update(mood="na"), "mood is na"),
        (lambda r: r["output"][0].update(added="x"), "added"),
        (lambda r: r["output"][0].update(sourced="no", added=["x"]), "unsourced"),
        (lambda r: r["fillers_left"].append({"output_id": 5, "token": "like"}), "bad output_id"),
        (lambda r: r["missed_fixes"].append({"source_id": True, "transcript": "x"}), "bad source_id"),
    ],
)
def test_validate_rejects(mutate, fragment):
    r = reply(3, 2)
    mutate(r)
    errors = validate(r, 3, 2)
    assert any(fragment in e for e in errors), errors


def test_batch_is_blind_and_bounded(data):
    run = Run(data, RUN_ID)
    paths = judge.prepare(run)
    text = "".join(p.read_text(encoding="utf-8") for p in paths)
    for leak in ("gpt-6-luna", "high", "a1b2c3d", "quick", "2f95c0", "off", "2099", RUN_ID, "dictionary"):
        assert leak not in text.replace(str(data), "")
    assert text.count("## Item") == 4
    with pytest.raises(JudgeError, match="already prepared"):
        judge.prepare(run)


def test_batches_split_at_item_limit(data, monkeypatch):
    monkeypatch.setattr(judge, "MAX_ITEMS", 3)
    assert len(judge.prepare(Run(data, RUN_ID))) == 2


def test_item_over_token_ceiling_is_refused(data, monkeypatch):
    monkeypatch.setattr(judge, "MAX_TOKENS", 1_000)
    with pytest.raises(JudgeError, match="alone needs"):
        judge.prepare(Run(data, RUN_ID))


def test_missing_failed_record_blocks_the_run(data):
    run_dir = data / "runs" / RUN_ID
    (run_dir / f"{CASES[3]}.json").unlink()
    with pytest.raises(JudgeError, match=f"{CASES[3]}.json: missing"):
        Run(data, RUN_ID)
    record = json.loads((run_dir / f"{CASES[2]}.json").read_text())
    record["outcome"] = "failed"
    (run_dir / f"{CASES[3]}.json").write_text(json.dumps(record))
    with pytest.raises(JudgeError, match="outcome failed"):
        Run(data, RUN_ID)


def test_record_without_required_fields_blocks_the_run(data):
    path = data / "runs" / RUN_ID / f"{CASES[0]}.json"
    record = json.loads(path.read_text())
    del record["output"]
    path.write_text(json.dumps(record))
    with pytest.raises(JudgeError, match="missing fields"):
        Run(data, RUN_ID)


def test_set_changed_since_the_run_is_refused(data):
    (data / "cases" / CASES[0] / "transcript.txt").write_text("Paint it blue.", encoding="utf-8")
    with pytest.raises(JudgeError, match="set is now"):
        Run(data, RUN_ID)


def test_inputs_or_judge_changed_since_prepare_are_refused(data, monkeypatch):
    run = Run(data, RUN_ID)
    judge.prepare(run)
    original = run.records[CASES[1]]["output"]
    edit_record(data, CASES[1], output="Paint it red. Then paint it blue.")
    run = Run(data, RUN_ID)
    answer_all(run)
    with pytest.raises(JudgeError, match="inputs of items"):
        judge.score(run, NONE)
    edit_record(data, CASES[1], output=original)
    run = Run(data, RUN_ID)
    answer_all(run)
    monkeypatch.setattr(judge, "judge_version", lambda: "def5678")
    with pytest.raises(JudgeError, match="judge opus abc1234 -> opus def5678"):
        judge.score(run, NONE)


def test_missing_reply_is_listed_then_scored_and_row_replaced(data):
    run = Run(data, RUN_ID)
    judge.prepare(run)
    answer_all(run)
    key = json.loads((run.judge_dir / "key.json").read_text())
    (judge.reply_dir(run, key) / "2.json").unlink()
    with pytest.raises(JudgeError, match="1 of 4 replies"):
        judge.score(run, NONE)
    assert (run.judge_dir / "retry-01.md").read_text(encoding="utf-8").count("## Item 2\n") == 1
    answer_all(run)
    summary = judge.score(run, NONE)
    assert not list(run.judge_dir.glob("retry-*.md"))
    assert summary["metrics"]["kept"][0] == 100.0
    assert summary["judge_version"] == "abc1234"
    for _ in range(2):
        judge.regenerate(data, lambda p: None)
    table = judge.RESULTS.read_text(encoding="utf-8")
    assert table.count("| a1b2c3d |") == 1
    assert "20990101" not in table
    header = "| " + " | ".join(judge.COLUMNS) + " |"
    assert header.startswith("| prompt | model | effort | dictionary | set | n | kept % (SE) | critical faults")
    assert header.endswith("| tokens /call | cost /call (USD) | seconds /call | judge | harness |")
    row = next(line for line in table.splitlines() if line.startswith("| a1b2c3d |")).split(" | ")
    assert len(row) == len(judge.COLUMNS)
    assert row[6:9] == ["100.0 (0.0)", "0.0", "0.0"] and row[12:14] == ["100.0", "n/a"]


def test_retries_are_bounded_like_batches(data, monkeypatch):
    monkeypatch.setattr(judge, "MAX_ITEMS", 3)
    run = Run(data, RUN_ID)
    judge.prepare(run)
    with pytest.raises(JudgeError, match="4 of 4 replies"):
        judge.score(run, NONE)
    retries = sorted(run.judge_dir.glob("retry-*.md"))
    assert [p.read_text(encoding="utf-8").count("## Item") for p in retries] == [3, 1]


def test_other_call_scores_as_all_absent_and_incomplete(data):
    edit_record(data, CASES[0], outcome="other", output=None)
    run = Run(data, RUN_ID)
    judge.prepare(run)
    assert len(json.loads((run.judge_dir / "key.json").read_text())["items"]) == 3

    def drop_first(case, r):
        if case == CASES[1]:
            r["source"][0].update(present="no", missing=["a clause"])
        return r

    answer_all(run, drop_first)
    m = judge.score(run, NONE)["metrics"]
    # four cases: other (kept 0, omissions 100), one omission in eight, two perfect
    assert m["kept"][0] == pytest.approx((0 + 87.5 + 100 + 100) / 4)
    assert m["omissions"][0] == pytest.approx((100 + 12.5) / 4)
    assert m["critical"][0] == pytest.approx((100 + 12.5) / 4)
    assert m["incomplete"][0] == 25.0
    assert m["critical_free"][0] == 50.0
    assert m["additions"][0] == 0.0  # the other case is excluded, not counted as zero-fault


def test_scores_file_has_one_line_per_case_replaced_on_rerun(data):
    scores = data / "scores.jsonl"
    scores.write_text(json.dumps({"run_id": "other-run", "case_id": "x"}) + "\n", encoding="utf-8")
    run = Run(data, RUN_ID)
    judge.prepare(run)
    answer_all(run, lambda case, r: r["source"][0].update(present="no", missing=["a clause"]) or r)
    judge.score(run, NONE)
    judge.score(run, NONE)
    lines = [json.loads(ln) for ln in scores.read_text(encoding="utf-8").splitlines()]
    assert [ln["run_id"] for ln in lines] == ["other-run"] + [RUN_ID] * 4
    row = lines[1]
    assert row == {
        "run_id": RUN_ID, "prompt": "a1b2c3d", "model": "gpt-6-luna", "effort": "high", "set": "quick",
        "dictionary": "off", "case_id": row["case_id"], "outcome": row["outcome"], "kept": row["kept"],
        "omissions": 1, "distortions": 0, "additions": 0, "fillers": row["fillers"], "critical": 1,
    }  # fmt: skip
    assert row["kept"] < 100 and isinstance(row["fillers"], int)


def test_score_prints_per_case_deltas_against_the_comparison_row(data, capsys):
    run = Run(data, RUN_ID)
    judge.prepare(run)
    answer_all(run)
    summary = judge.score(run, NONE)
    assert "delta kept" not in capsys.readouterr().out
    prior = json.loads(json.dumps(summary)) | {"prompt": "0000000"}
    prior["per_case"][CASES[0]]["kept"] = 75.0
    prior_dir = data / "runs" / "0000000_gpt-6-luna_high_quick_off" / "judge"
    prior_dir.mkdir(parents=True)
    (prior_dir / "summary.json").write_text(json.dumps(prior), encoding="utf-8")
    judge.score(run, {"a1b2c3d": "0000000"}.get)
    out = capsys.readouterr().out
    assert "delta kept 6.2 (6.2) vs 0000000" in out
    assert f"  {CASES[0]}  +25.0" in out and f"  {CASES[1]}  +0.0" in out


def test_paired_delta():
    mean, se, deltas = paired_delta({"a": 90.0, "b": 80.0, "c": 70.0}, {"a": 80.0, "b": 80.0, "c": 60.0})
    assert deltas == {"a": 10.0, "b": 0.0, "c": 10.0}
    assert mean == pytest.approx(20 / 3)
    assert se == pytest.approx((100 / 3) ** 0.5 / 3**0.5)
    with pytest.raises(JudgeError):
        paired_delta({"a": 1.0}, {"b": 1.0})


def test_paired_delta_skips_cases_without_content():
    assert paired_delta({"a": None, "b": 90.0}, {"a": None, "b": 80.0}) == (10.0, None, {"b": 10.0})
    assert paired_delta({"a": None}, {"a": 50.0}) == (None, None, {})


def test_previous_change_is_relative_to_the_row_commit():
    assert judge.previous_change("bbbbbbb", ["bbbbbbb", "aaaaaaa"]) == "aaaaaaa"
    # a commit that did not touch AGENTS.md compares with the last change before it
    assert judge.previous_change("ccccccc", ["aaaaaaa"]) == "aaaaaaa"
    assert judge.previous_change("aaaaaaa", ["aaaaaaa"]) is None


def test_delta_finds_previous_prompt_at_same_cell():
    def summary(prompt, kept, **over):
        s = {"prompt": prompt, "model": "m", "effort": "high", "dictionary": "off", "set_version": "quick@1",
             "judge_version": "j1", "per_case": {"a": {"kept": kept[0]}, "b": {"kept": kept[1]}}}
        return s | over  # fmt: skip

    new, old = summary("bbbbbbb", (100.0, 50.0)), summary("aaaaaaa", (80.0, 50.0))
    other_judge = summary("aaaaaaa", (0.0, 0.0), judge_version="j2")
    previous = {"bbbbbbb": "aaaaaaa"}.get
    found = judge.find_delta(new, [new, other_judge, old], previous)
    assert found is not None and found[0] == "10.0 (10.0) vs aaaaaaa"
    assert judge.find_delta(old, [new, old], previous) is None
    assert judge.find_delta(new, [new, other_judge], previous) is None
