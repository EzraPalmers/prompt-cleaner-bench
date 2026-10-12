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
    monkeypatch.setattr(judge, "commit_of", lambda paths: "abc1234")
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


@pytest.mark.parametrize("method", ["sentence", "direct", "chunked"])
def test_batch_is_blind_and_bounded(data, method):
    run = Run(data, RUN_ID)
    paths = judge.prepare(run, method)
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
    monkeypatch.setattr(judge, "commit_of", lambda paths: "def5678")
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
        "dictionary": "off", "case_id": row["case_id"], "method": "sentence", "outcome": row["outcome"],
        "kept": row["kept"], "omissions": 1, "distortions": 0, "additions": 0, "fillers": row["fillers"],
        "critical": 1, "content": 8, "judge_content": None,
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


# Direct and chunked methods. Faults per fixture case; every transcript has eight sentences in one paragraph.
TALLIES = {
    CASES[0]: {"faults": [{"s": 8, "type": "dropped", "quote": "Let's keep the old export path around"}],
               "content_sentences": 7},
    CASES[1]: {"faults": [{"s": 1, "type": "clause", "quote": "because it takes ages on big projects"}]},
    CASES[2]: {"faults": [{"s": 2, "type": "hedge", "quote": "I think the slow part is probably"}]},
    CASES[3]: {"added": ["over a gigabyte"], "fillers_left": {"so": 1}},
}  # fmt: skip


def tally(case: str = "") -> dict:
    empty = {"noise": [], "content_sentences": 8, "faults": [], "added": [], "fillers_left": {}, "stubs_left": [],
             "wrong_fixes": [], "missed_fixes": []}  # fmt: skip
    return empty | json.loads(json.dumps(TALLIES.get(case, {})))


def answer_tallies(run: Run, method: str) -> None:
    key = json.loads((run.judge_dir / "key.json").read_text())
    for n, item in key["items"].items():
        r = tally(item["case"])
        if method == "chunked":
            _, _, output = judge.item_parts(run, item["case"])
            r = {"parts": [{"part": 1, "output": [1, len(output)], **r}]}
        (judge.reply_dir(run, key) / f"{n}.json").write_text(json.dumps(r), encoding="utf-8")


@pytest.mark.parametrize("method", ["direct", "chunked"])
def test_tally_methods_score_and_record_the_method(data, method):
    scores = data / "scores.jsonl"
    run = Run(data, RUN_ID)
    paths = judge.prepare(run, method)
    key = json.loads((run.judge_dir / "key.json").read_text())
    assert key["method"] == method and key["judge_version"] == f"{method} abc1234"
    text = paths[0].read_text(encoding="utf-8")
    assert "[S1] Okay so I want" in text and "[S8] Let's keep" in text and "S1:" not in text
    assert ("Part 1:\n[S1]" in text and "[O1] I want" in text) if method == "chunked" else "[O1]" not in text
    answer_tallies(run, method)
    with pytest.raises(JudgeError, match="prepared with --method"):
        judge.score(run, NONE)
    m = judge.score(run, NONE, method)["metrics"]
    # one dropped, one clause, one hedge, one addition, each in a different case of eight content sentences
    assert m["kept"][0] == pytest.approx((87.5 + 87.5 + 87.5 + 100) / 4)
    assert m["omissions"][0] == pytest.approx(25 / 4)
    assert m["distortions"][0] == pytest.approx(12.5 / 4)
    assert m["additions"][0] == 0.25 and m["critical_free"][0] == 0.0
    rows = {r["case_id"]: r for r in map(json.loads, scores.read_text(encoding="utf-8").splitlines())}
    assert {r["method"] for r in rows.values()} == {method}
    first = rows[CASES[0]]  # kept over the split's 8 sentences, not the judge's 7
    assert (first["kept"], first["content"], first["judge_content"], first["omissions"]) == (87.5, 8, 7, 1)
    assert rows[CASES[3]]["fillers"] == 0  # the judge's "so" does not occur in the output
    judge.regenerate(data, NONE)
    assert f"| opus/{method} abc1234 |" in judge.RESULTS.read_text(encoding="utf-8")


def test_invalid_tally_reply_is_listed_for_retry(data):
    run = Run(data, RUN_ID)
    judge.prepare(run, "direct")
    answer_tallies(run, "direct")
    key = json.loads((run.judge_dir / "key.json").read_text())
    bad = tally() | {"faults": [{"s": 3, "type": "mood", "quote": "a sentence nobody said"}]}
    (judge.reply_dir(run, key) / "3.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(JudgeError, match="1 of 4 replies"):
        judge.score(run, NONE, "direct")
    assert (run.judge_dir / "retry-01.md").read_text(encoding="utf-8").count("## Item 3\n") == 1


TRANSCRIPT = (FIXTURE / "cases" / CASES[2] / "transcript.txt").read_text(encoding="utf-8")
OUTPUT = json.loads((FIXTURE / "runs" / RUN_ID / f"{CASES[2]}.json").read_text(encoding="utf-8"))["output"]


def check_direct(r) -> list[str]:
    return judge.validate_direct(r, 8, judge.norm(TRANSCRIPT), judge.norm(OUTPUT))


def fault(s, kind="hedge", quote="I think") -> dict:
    return {"s": s, "type": kind, "quote": quote}


def test_validate_direct_accepts_valid_reply():
    r = tally(CASES[2])
    r["faults"] += [fault(1, "clause", "um, because"), fault(1, "clause", "i  WANT")]
    assert check_direct(r) == []


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda r: r.pop("added"), "top-level keys"),
        (lambda r: r.update(note=""), "top-level keys"),
        (lambda r: r.update(noise=[9]), "noise must be"),
        (lambda r: r.update(noise=[1, 1]), "noise must be"),
        (lambda r: r.update(content_sentences="8"), "content_sentences"),
        (lambda r: r["faults"].append({"s": 2, "type": "mood"}), "keys must be"),
        (lambda r: r["faults"].append(fault(3, [])), "type []"),
        (lambda r: r["faults"].append(fault(3, "tone")), "type 'tone'"),
        (lambda r: r["faults"].append(fault(True)), "s True"),
        (lambda r: r["faults"].append(fault(3, "mood", "the build fails")), "not a quote from the transcript"),
        (lambda r: r["added"].append("over a terabyte"), "not a quote from the output"),
        (lambda r: r["wrong_fixes"].append({"transcript": "x", "output": "nowhere"}), "not a quote from the output"),
        (lambda r: r["missed_fixes"].append({"s": 0, "transcript": "I think"}), "s 0"),
        (lambda r: r.update(fillers_left={"um": 1}), "fillers_left"),
        (lambda r: r.update(fillers_left={"so": 0}), "fillers_left"),
        (lambda r: r.update(fillers_left=[]), "fillers_left"),
        (lambda r: r.update(noise=[2]), "listed as noise"),
        (lambda r: r["faults"].append(fault(2, "dropped")), "stands alone"),
        (lambda r: r["faults"].append(fault(2)), "repeats ['hedge']"),
    ],
)
def test_validate_direct_rejects(mutate, fragment):
    r = tally(CASES[2])
    mutate(r)
    errors = check_direct(r)
    assert any(fragment in e for e in errors), errors


def chunked_reply() -> dict:
    return {"parts": [
        tally() | {"part": 1, "output": [1, 2], "content_sentences": 2, "fillers_left": {"so": 1}},
        tally() | {"part": 2, "output": [2, 3], "content_sentences": 1, "noise": [4], "fillers_left": {"so": 2}},
    ]}  # fmt: skip


def check_chunked(r) -> list[str]:
    transcript = "Paint the gate. Then the fence. Then the shed. Um so."
    output = "Paint the gate. Then the fence. And the shed."
    return judge.validate_chunked(r, [range(1, 3), range(3, 5)], 3, judge.norm(transcript), judge.norm(output))


def test_validate_chunked_accepts_valid_reply_and_merges_parts():
    r = chunked_reply()
    assert check_chunked(r) == []
    r["parts"][0]["output"], r["parts"][1]["output"] = [1, 3], []
    assert check_chunked(r) == []
    merged = judge.merge_parts(chunked_reply())
    assert (merged["content_sentences"], merged["noise"], merged["fillers_left"]) == (3, [4], {"so": 3})


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda r: r.update(extra=[]), "top-level keys"),
        (lambda r: r["parts"].pop(), "expected 2 entries"),
        (lambda r: r["parts"][1].update(part=3), "part 3 != 2"),
        (lambda r: r["parts"][0].pop("noise"), "keys must be"),
        (lambda r: r["parts"][0].update(output=[1]), "output must be"),
        (lambda r: r["parts"][0].update(output=[2, 1]), "output must be"),
        (lambda r: r["parts"][1].update(output=[2, 4]), "output must be"),
        (lambda r: r["parts"][1].update(output=[1, 3]), "spans must run"),
        (lambda r: r["parts"][0].update(output=[1, 1]) or r["parts"][1].update(output=[3, 3]), "spans must run"),
        (lambda r: r["parts"][1].update(output=[]), "spans must run"),
        (lambda r: r["parts"][0].update(noise=[3]), "noise must be"),
        (lambda r: r["parts"][1]["faults"].append(fault(1, "clause", "Paint the gate")), "s 1 bad"),
        (lambda r: r["parts"][1]["faults"].append(fault(4, "clause", "Um so")), "listed as noise"),
    ],
)
def test_validate_chunked_rejects(mutate, fragment):
    r = chunked_reply()
    mutate(r)
    errors = check_chunked(r)
    assert any(fragment in e for e in errors), errors


def test_chunks_split_long_paragraphs_at_sentence_ends():
    sentence = " ".join(["word"] * 39) + " end."
    long = " ".join([sentence] * 6)  # 240 words: the first block closes after the fourth sentence, at 160
    assert [len(p) for p in judge.chunks(f"{long}\n\nShort one. Short two.")] == [4, 2, 2]
    assert [len(p) for p in judge.chunks(" ".join([sentence] * 5))] == [5]  # 200 words stays whole
    run_on = " ".join(["word"] * 250)
    assert judge.chunks(run_on) == [[run_on]]
    text = f"{long}\n\nShort one."
    assert [s for part in judge.chunks(text) for s in part] == split_sentences(text)


def test_tally_fillers_cap_judge_tokens_at_uncounted_occurrences():
    assert judge.tally_fillers("Okay, it is like slow and like broken.", {"like": 3}) == 3
    assert judge.tally_fillers("It is, like, slow and like broken.", {"Like ": 2}) == 2
    assert judge.tally_fillers("It is kind of slow.", {"kind of": 1, "so": 2}) == 1
