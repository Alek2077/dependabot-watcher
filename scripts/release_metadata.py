"""Read-only release identity and publication-channel validation."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def metadata(root: Path, tag: str | None = None) -> dict[str, str]:
    source = (root / "src/dependabot_watcher/__init__.py").read_text()
    match = re.search(r'^__version__ = "(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:(a|b|rc)([1-9][0-9]*))?"$', source, re.M)
    if match is None:
        raise ValueError("Use major.minor.patch with optional aN, bN or rcN prerelease suffix.")
    version = match[0].split('"')[1]
    if tag is not None and tag != "v" + version:
        raise ValueError("Release tag must exactly match the package version.")
    notes = Path("docs/release-notes") / f"{version}.md"
    if not (root / notes).is_file() or not (root / notes).read_text().strip():
        raise ValueError("A release requires nonempty version-specific release notes.")
    return {"version": version, "prerelease": str(match[4] is not None).lower(), "notes": notes.as_posix()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tag")
    args = parser.parse_args()
    print(json.dumps(metadata(args.root, args.tag)))
