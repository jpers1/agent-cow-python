#!/usr/bin/env python3
"""Reject project-specific branding in the current tracked release tree."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROHIBITED_TOKEN = b"sl" + b"aif"


def tracked_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        REPOSITORY_ROOT / path.decode("utf-8", errors="surrogateescape")
        for path in result.stdout.split(b"\0")
        if path
    ]


def main() -> int:
    matches: list[str] = []
    for path in tracked_paths():
        relative = path.relative_to(REPOSITORY_ROOT)
        if PROHIBITED_TOKEN in str(relative).encode().lower():
            matches.append(f"path: {relative}")
            continue
        if path.is_file() and PROHIBITED_TOKEN in path.read_bytes().lower():
            matches.append(f"content: {relative}")

    if matches:
        print("ERROR: project-specific branding found in the release tree")
        for match in matches:
            print(f"- {match}")
        return 1

    print("PASS: tracked release tree contains no project-specific branding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
