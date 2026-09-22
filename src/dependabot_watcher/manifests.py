from __future__ import annotations

import posixpath
import shlex
from collections import deque
from typing import Any, Iterable
from urllib.parse import urlsplit

from .github import GitHubClient, GitHubError, repository_relative_path


_INCLUDE_OPTIONS = {
    "-r": "requirement",
    "--requirement": "requirement",
    "-c": "constraint",
    "--constraint": "constraint",
}


def is_python_requirements_manifest(path: object) -> bool:
    normalized = str(path or "").replace("\\", "/").casefold()
    return posixpath.basename(normalized).endswith(".txt")


def _repository_relative_include(source_path: str, target: str) -> str | None:
    normalized_source = repository_relative_path(source_path)
    if normalized_source is None:
        return None
    candidate = target.strip().replace("\\", "/")
    parsed = urlsplit(candidate)
    if (
        not candidate
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or candidate.startswith("/")
        or candidate.startswith("~")
        or "$" in candidate
    ):
        return None
    normalized = posixpath.normpath(
        posixpath.join(posixpath.dirname(normalized_source), candidate)
    )
    return repository_relative_path(normalized)


def parse_python_requirements_includes(
    source_path: str,
    content: str,
) -> tuple[dict[str, str], ...]:
    """Parse local requirement/constraint includes without executing repository text."""

    includes: list[dict[str, str]] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if not parts:
            continue
        option = parts[0]
        target: str | None = None
        relation: str | None = None
        if option in _INCLUDE_OPTIONS and len(parts) == 2:
            relation = _INCLUDE_OPTIONS[option]
            target = parts[1]
        else:
            for prefix, relation_name in _INCLUDE_OPTIONS.items():
                if option.startswith(prefix + "="):
                    relation = relation_name
                    target = option[len(prefix) + 1 :]
                    break
                if prefix in {"-r", "-c"} and option.startswith(prefix) and len(option) > 2:
                    relation = relation_name
                    target = option[2:]
                    break
        if relation is None or target is None:
            continue
        normalized = _repository_relative_include(source_path, target)
        if normalized is None:
            continue
        includes.append({"path": normalized, "relation": relation})
    return tuple(
        {"path": path, "relation": relation}
        for path, relation in dict.fromkeys(
            (item["path"], item["relation"]) for item in includes
        )
    )


def collect_manifest_evidence(
    *,
    repository: str,
    base_sha: str,
    seed_paths: Iterable[str],
    client: GitHubClient,
    max_files: int,
    max_file_bytes: int,
    max_depth: int,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    seed_candidates: set[str] = set()
    for path in seed_paths:
        if not is_python_requirements_manifest(path):
            continue
        normalized = repository_relative_path(path)
        if normalized is None:
            errors.append(
                {
                    "path": str(path),
                    "message": "Manifest seed path is not a safe repository-relative path.",
                }
            )
            continue
        seed_candidates.add(normalized)
    all_seeds = sorted(seed_candidates)
    seeds = all_seeds[:max_files]
    truncations: list[dict[str, Any]] = []
    if len(all_seeds) > max_files:
        truncations.append(
            {
                "surface": "manifest_files",
                "limit": max_files,
                "message": "Manifest evidence exceeded the configured file ceiling.",
            }
        )
    if not seeds:
        return {
            "status": "partial" if errors or truncations else "not_applicable",
            "base_sha": base_sha,
            "files": [],
            "relationships": [],
            "errors": errors,
            "truncations": truncations,
        }

    queue = deque((path, 0) for path in seeds)
    queued = set(seeds)
    files: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    attempted_files = 0

    while queue:
        path, depth = queue.popleft()
        if attempted_files >= max_files:
            truncations.append(
                {
                    "surface": "manifest_files",
                    "limit": max_files,
                    "message": "Manifest evidence exceeded the configured file ceiling.",
                }
            )
            break
        attempted_files += 1
        try:
            content = client.fetch_text_file(
                repository,
                path,
                base_sha,
                max_file_bytes,
            )
        except GitHubError as exc:
            errors.append({"path": path, "message": str(exc)})
            continue
        parsed_includes = parse_python_requirements_includes(path, content)
        remaining_relationships = max_files - len(relationships)
        includes = parsed_includes[:remaining_relationships]
        if len(parsed_includes) > len(includes):
            truncations.append(
                {
                    "surface": "manifest_relationships",
                    "limit": max_files,
                    "message": "Manifest evidence exceeded the bounded relationship ceiling.",
                }
            )
        files.append(
            {
                "path": path,
                "depth": depth,
                "includes": [dict(item) for item in includes],
            }
        )
        for include in includes:
            included_path = include["path"]
            relationships.append(
                {
                    "source_manifest": path,
                    "included_manifest": included_path,
                    "relation": include["relation"],
                    "depth": depth + 1,
                }
            )
            if included_path in queued:
                continue
            if depth + 1 > max_depth:
                truncations.append(
                    {
                        "surface": "manifest_depth",
                        "limit": max_depth,
                        "path": included_path,
                        "message": "Manifest evidence exceeded the configured inclusion depth.",
                    }
                )
                continue
            if len(queued) >= max_files:
                truncations.append(
                    {
                        "surface": "manifest_files",
                        "limit": max_files,
                        "path": included_path,
                        "message": "Manifest evidence exceeded the configured file ceiling.",
                    }
                )
                continue
            queued.add(included_path)
            queue.append((included_path, depth + 1))

    return {
        "status": "partial" if errors or truncations else "complete",
        "base_sha": base_sha,
        "files": files,
        "relationships": relationships,
        "errors": errors,
        "truncations": truncations,
    }
