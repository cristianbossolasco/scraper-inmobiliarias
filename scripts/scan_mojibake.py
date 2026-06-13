from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIRS = {
    ".git",
    ".tools",
    ".venv",
    "__pycache__",
    "node_modules",
    ".codex-remote-attachments",
}
EXCLUDED_SUFFIXES = {
    ".exe",
    ".gz",
    ".jpeg",
    ".jpg",
    ".log",
    ".png",
    ".pyc",
    ".sqlite3",
    ".xlsx",
    ".zip",
}
EXCLUDED_PREFIXES = {
    ("data", "raw"),
}
EXCLUDED_FILENAMES = {
    "db.sqlite3",
}

MARKERS = {
    "latin1_utf8_c3": "\u00c3",
    "latin1_utf8_c2": "\u00c2",
    "cp1252_punct_e2": "\u00e2",
    "replacement_char": "\ufffd",
}


def is_excluded(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    parts = relative.parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    if any(parts[: len(prefix)] == prefix for prefix in EXCLUDED_PREFIXES):
        return True
    if path.name in EXCLUDED_FILENAMES or path.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    if path.name.startswith("db.sqlite3"):
        return True
    return False


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    hits = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for marker_name, marker in MARKERS.items():
            if marker in line:
                rendered = line.encode("unicode_escape").decode("ascii")
                hits.append((line_number, marker_name, rendered))
    return hits


def main() -> int:
    failures = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or is_excluded(path):
            continue
        hits = scan_file(path)
        if hits:
            failures.append((path, hits))

    for path, hits in failures:
        relative = path.relative_to(ROOT)
        print(f"{relative}")
        for line_number, marker_name, line in hits[:10]:
            print(f"  {line_number}: {marker_name}: {line}")
        if len(hits) > 10:
            print(f"  ... {len(hits) - 10} more")

    if failures:
        print(f"mojibake scan failed: {len(failures)} files", file=sys.stderr)
        return 1
    print("mojibake scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
