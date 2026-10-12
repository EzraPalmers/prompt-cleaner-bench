"""Public-content guard for the tool and cleaner repositories (design section 9).

Checks the git repository of the current working directory against the tool's data/,
found next to this script.

    uv run python src/check.py                  staged files (pre-commit hook)
    uv run python src/check.py --all            every file in the index
    uv run python src/check.py --message PATH   a commit message (commit-msg hook)

From cleaner/: uv run --project .. python ../src/check.py [same flags].
Exit 0 on pass (warnings allowed), 1 on any FAIL or missing input.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
SEQ_LEN = 8

WARN_PATTERNS = {
    "windows user path": re.compile(r"(?:\b[A-Za-z]:|/mnt/[a-z]|/[a-z])[\\/]+Users[\\/]+[^\\/\s\"'`]+", re.IGNORECASE),
    "uuid": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
    "rollout file name": re.compile(r"\brollout-\d{4}-\d{2}-\d{2}T[\w.-]*"),
}


class InputError(Exception):
    pass


def load_terms(path: Path) -> list[tuple[str, re.Pattern[str]]]:
    if not path.is_file():
        raise InputError(f"missing denylist: {path}")
    terms = [s for line in path.read_text(encoding="utf-8").splitlines() if (s := line.split("#", 1)[0].strip())]
    if not terms:
        raise InputError(f"empty denylist: {path}")
    out = []
    for term in terms:
        parts = [re.escape(p) for p in re.split(r"[\s_-]+", term) if p]
        out.append((term, re.compile(r"(?<![^\W_])" + r"[\s_-]+".join(parts) + r"(?![^\W_])", re.IGNORECASE)))
    return out


def words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.lower().replace("'", "").replace("’", ""))


def sequences(text: str) -> set[str]:
    w = words(text)
    return {" ".join(w[i : i + SEQ_LEN]) for i in range(len(w) - SEQ_LEN + 1)}


def load_case_sequences(cases: Path) -> set[str]:
    if not cases.is_dir():
        raise InputError(f"missing cases folder: {cases}")
    seqs: set[str] = set()
    n = 0
    for case in cases.iterdir():
        for name in ("transcript.txt", "output.txt"):
            f = case / name
            if f.is_file():
                n += 1
                seqs |= sequences(f.read_text(encoding="utf-8", errors="replace"))
    if n == 0:
        raise InputError(f"no transcripts or outputs under {cases}")
    return seqs


def git(root: Path, *args: str, ok: tuple[int, ...] = (0,), stdin: bytes | None = None) -> bytes:
    r = subprocess.run(["git", *args], check=False, cwd=root, input=stdin, capture_output=True)
    if r.returncode not in ok:
        raise InputError(f"git {' '.join(args)} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def index_paths(root: Path, staged_only: bool) -> list[str]:
    if staged_only:
        out = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z")
        return [p for p in out.decode().split("\0") if p]
    out = git(root, "ls-files", "-s", "-z")
    return [e.split("\t", 1)[1] for e in out.decode().split("\0") if e and not e.startswith("160000")]


def read_blobs(root: Path, paths: list[str]) -> dict[str, bytes]:
    """Read each path's index blob (`:path`) in one git cat-file call."""
    if not paths:
        return {}
    out = git(root, "cat-file", "--batch", stdin="".join(f":{p}\n" for p in paths).encode())
    blobs, pos = {}, 0
    for p in paths:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].split()
        if header[-1] == b"missing":
            pos = nl + 1
            continue
        size = int(header[2])
        blobs[p] = out[nl + 1 : nl + 1 + size]
        pos = nl + 1 + size + 1
    return blobs


def check_text(name: str, text: str, terms, case_seqs: set[str], fails: list[str], warns: list[str]) -> None:
    for term, rx in terms:
        if rx.search(text):
            fails.append(f"FAIL {name}: private term: {term}")
    for seq in sorted(sequences(text) & case_seqs):
        fails.append(f"FAIL {name}: {SEQ_LEN}-word sequence from data/cases: {seq}")
    for rule, rx in WARN_PATTERNS.items():
        hits = sorted(set(rx.findall(text)))
        if hits:
            warns.append(f"WARN {name}: {rule}: {', '.join(hits[:3])}{' ...' if len(hits) > 3 else ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Block private text from the public repositories.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="check every file in the index, not only staged changes")
    mode.add_argument("--message", type=Path, metavar="PATH", help="check a commit message file")
    args = ap.parse_args()

    start = time.perf_counter()
    fails: list[str] = []
    warns: list[str] = []
    try:
        terms = load_terms(DATA / "private-terms.txt")
        case_seqs = load_case_sequences(DATA / "cases")
        if args.message:
            lines = args.message.read_text(encoding="utf-8", errors="replace").splitlines()
            check_text("commit message", "\n".join(ln for ln in lines if not ln.startswith("#")), terms, case_seqs,
                       fails, warns)
            n = 1
        else:
            root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
            paths = index_paths(root, staged_only=not args.all)
            if paths:
                ignored = git(root, "check-ignore", "--no-index", "-z", "--stdin", ok=(0, 1),
                              stdin="\0".join(paths).encode() + b"\0")
                fails += [f"FAIL {p}: ignored path staged" for p in ignored.decode().split("\0") if p]
            blobs = read_blobs(root, paths)
            for p in paths:
                check_text(f"{p} (path)", p, terms, set(), fails, [])
                blob = blobs.get(p)
                if blob is None or b"\0" in blob[:8000]:
                    continue
                check_text(p, blob.decode("utf-8", errors="replace"), terms, case_seqs, fails, warns)
            n = len(paths)
    except (InputError, OSError) as e:
        print(f"FAIL check.py: {e}", file=sys.stderr)
        return 1

    for line in warns + fails:
        print(line, file=sys.stderr)
    verdict = "blocked" if fails else "passed"
    print(f"check: {verdict}, {n} item(s), {len(fails)} fail, {len(warns)} warn, {time.perf_counter() - start:.1f}s")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
