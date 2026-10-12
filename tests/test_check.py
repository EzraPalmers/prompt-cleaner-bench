import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "check.py"
CASE_TEXT = "the purple walrus sang quietly beneath a frozen lighthouse every winter evening"
QUOTE = "walrus sang quietly beneath a frozen lighthouse every"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                   capture_output=True)


@pytest.fixture
def tool(tmp_path: Path) -> Path:
    """A tool layout with the script, an invented denylist and one invented case."""
    root = tmp_path / "tool"
    (root / "src").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "src" / "check.py")
    (root / "data" / "cases" / "c1").mkdir(parents=True)
    (root / "data" / "private-terms.txt").write_text("# comment\nZorblax\nquantum ferret\n", encoding="utf-8")
    (root / "data" / "cases" / "c1" / "transcript.txt").write_text(CASE_TEXT, encoding="utf-8")
    (root / "data" / "cases" / "c1" / "output.txt").write_text("An unrelated output.", encoding="utf-8")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / ".gitignore").write_text("secret/\n", encoding="utf-8")
    return r


def run(tool: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(tool / "src" / "check.py"), *args], check=False, cwd=cwd,
                          capture_output=True, text=True)


def stage(repo: Path, name: str, content: str | bytes) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "-f", name)


def test_clean_file_passes(tool, repo):
    stage(repo, "a.txt", "Nothing private here at all.")
    r = run(tool, repo)
    assert r.returncode == 0, r.stderr
    assert "passed" in r.stdout


@pytest.mark.parametrize("text", ["we met the ZORBLAX team", "a Quantum-Ferret run", "quantum_ferret"])
def test_private_term_fails(tool, repo, text):
    stage(repo, "a.txt", text)
    r = run(tool, repo)
    assert r.returncode == 1
    assert "FAIL a.txt: private term" in r.stderr


def test_term_is_whole_word(tool, repo):
    stage(repo, "a.txt", "Zorblaxian is a different word.")
    assert run(tool, repo).returncode == 0


def test_term_in_path_fails(tool, repo):
    stage(repo, "zorblax/notes.txt", "fine")
    r = run(tool, repo)
    assert r.returncode == 1
    assert "(path): private term" in r.stderr


def test_eight_word_quote_fails_seven_passes(tool, repo):
    stage(repo, "a.md", f"Example: '{QUOTE.upper()}!'")
    r = run(tool, repo)
    assert r.returncode == 1
    assert "8-word sequence" in r.stderr
    stage(repo, "a.md", " ".join(QUOTE.split()[:7]))
    assert run(tool, repo).returncode == 0


def test_ignored_path_staged_fails(tool, repo):
    stage(repo, "secret/x.txt", "fine")
    r = run(tool, repo)
    assert r.returncode == 1
    assert "FAIL secret/x.txt: ignored path staged" in r.stderr


def test_blob_judged_not_working_copy(tool, repo):
    stage(repo, "a.txt", "clean text")
    (repo / "a.txt").write_text("Zorblax", encoding="utf-8")
    assert run(tool, repo).returncode == 0
    stage(repo, "b.txt", "Zorblax")
    (repo / "b.txt").write_text("clean text", encoding="utf-8")
    assert run(tool, repo).returncode == 1


def test_all_covers_committed_files(tool, repo):
    stage(repo, "a.txt", "Zorblax")
    git(repo, "commit", "-q", "--no-verify", "-m", "x")
    assert run(tool, repo).returncode == 0
    r = run(tool, repo, "--all")
    assert r.returncode == 1
    assert "FAIL a.txt: private term" in r.stderr


def test_warnings_do_not_fail(tool, repo):
    stage(repo, "a.txt", r"C:\Users\someone\x and 123e4567-e89b-12d3-a456-426614174000")
    r = run(tool, repo)
    assert r.returncode == 0
    assert "WARN a.txt: windows user path" in r.stderr
    assert "WARN a.txt: uuid" in r.stderr


def test_binary_skipped(tool, repo):
    stage(repo, "a.bin", b"\x00\x01Zorblax")
    assert run(tool, repo).returncode == 0


def test_message(tool, repo, tmp_path):
    msg = tmp_path / "MSG"
    msg.write_text("feat: add thing\n# Zorblax in a git comment line\n", encoding="utf-8")
    assert run(tool, repo, "--message", str(msg)).returncode == 0
    msg.write_text("feat: support zorblax\n", encoding="utf-8")
    r = run(tool, repo, "--message", str(msg))
    assert r.returncode == 1
    assert "FAIL commit message: private term" in r.stderr


def test_nested_repo_checks_own_index(tool, repo):
    stage(repo, "a.txt", "Zorblax")
    nested = repo / "inner"
    nested.mkdir()
    git(nested, "init", "-q", "-b", "main")
    stage(nested, "b.txt", "clean")
    assert run(tool, nested).returncode == 0
    stage(nested, "c.txt", "Zorblax")
    r = run(tool, nested)
    assert r.returncode == 1
    assert "FAIL c.txt" in r.stderr


@pytest.mark.parametrize("broken", ["no_terms", "empty_terms", "no_cases", "empty_cases"])
def test_missing_inputs_fail(tool, repo, broken):
    data = tool / "data"
    if broken == "no_terms":
        (data / "private-terms.txt").unlink()
    elif broken == "empty_terms":
        (data / "private-terms.txt").write_text("# only a comment\n\n", encoding="utf-8")
    else:
        shutil.rmtree(data / "cases")
        if broken == "empty_cases":
            (data / "cases").mkdir()
    stage(repo, "a.txt", "clean")
    r = run(tool, repo)
    assert r.returncode == 1
    assert "FAIL check.py:" in r.stderr
