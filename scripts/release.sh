#!/usr/bin/env bash
set -euo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    echo "Usage: ./scripts/release.sh [patch|minor|major]"
    echo ""
    echo "Bumps the package version, commits, tags, and pushes to trigger PyPI publish."
    echo "Defaults to 'patch' if no argument is given."
    exit 0
fi

BUMP="${1:-patch}"

if [ -n "$(git status --porcelain)" ]; then
    echo "error: working directory is not clean, commit or stash changes first"
    exit 1
fi

VERSION=$(python - "$BUMP" <<'PY'
import re
import sys
from pathlib import Path

path = Path("agentcow/__init__.py")
source = path.read_text()
match = re.search(r'^__version__ = "([0-9]+)\.([0-9]+)\.([0-9]+)"$', source, re.M)
if match is None:
    raise SystemExit("error: could not find a numeric __version__ assignment")
major, minor, patch = map(int, match.groups())
bump = sys.argv[1]
if bump == "major":
    major, minor, patch = major + 1, 0, 0
elif bump == "minor":
    minor, patch = minor + 1, 0
elif bump == "patch":
    patch += 1
else:
    raise SystemExit(f"error: unsupported version bump: {bump}")
version = f"{major}.{minor}.{patch}"
path.write_text(source[: match.start(1)] + version + source[match.end(1) :])
print(version)
PY
)

git add agentcow/__init__.py
git commit -m "bump to $VERSION"
git tag "v$VERSION"
git push origin HEAD
git push origin "v$VERSION"

echo "released v$VERSION"
