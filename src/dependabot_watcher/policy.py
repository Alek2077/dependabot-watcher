from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .config import PathPolicyOverrides
from .github import repository_relative_path


DEPENDABOT_AUTHORS = frozenset({"app/dependabot", "dependabot", "dependabot[bot]"})

BUILTIN_PATH_PATTERNS: dict[str, tuple[str, ...]] = {
    "dependency_manifest": (
        "package.json",
        "**/package.json",
        "pyproject.toml",
        "**/pyproject.toml",
        "requirements*.txt",
        "**/requirements*.txt",
        "Pipfile",
        "**/Pipfile",
        "poetry.toml",
        "**/poetry.toml",
        "Cargo.toml",
        "**/Cargo.toml",
        "go.mod",
        "**/go.mod",
        "composer.json",
        "**/composer.json",
        "Gemfile",
        "**/Gemfile",
    ),
    "dependency_lockfile": (
        "package-lock.json",
        "**/package-lock.json",
        "pnpm-lock.yaml",
        "**/pnpm-lock.yaml",
        "yarn.lock",
        "**/yarn.lock",
        "uv.lock",
        "**/uv.lock",
        "poetry.lock",
        "**/poetry.lock",
        "Pipfile.lock",
        "**/Pipfile.lock",
        "Cargo.lock",
        "**/Cargo.lock",
        "go.sum",
        "**/go.sum",
        "composer.lock",
        "**/composer.lock",
        "Gemfile.lock",
        "**/Gemfile.lock",
    ),
    "ci_workflow": (
        ".github/workflows/*.yml",
        ".github/workflows/*.yaml",
        ".gitlab-ci.yml",
        "azure-pipelines.yml",
        "Jenkinsfile",
    ),
    "deployment_infrastructure": (
        "Dockerfile",
        "Dockerfile.*",
        "**/Dockerfile",
        "**/Dockerfile.*",
        "docker-compose*.yml",
        "docker-compose*.yaml",
        "**/*.tf",
        "**/*.tfvars",
        "**/Chart.yaml",
        "**/values*.yaml",
        "wrangler.toml",
        "wrangler.json",
        "wrangler.jsonc",
        "**/wrangler.toml",
        "**/wrangler.json",
        "**/wrangler.jsonc",
    ),
    "auth_security": (
        ".github/CODEOWNERS",
        "CODEOWNERS",
        "SECURITY.md",
        "**/SECURITY.md",
        ".npmrc",
        "**/.npmrc",
        "pip.conf",
        "**/pip.conf",
    ),
    "runtime_configuration": (
        ".env*",
        "**/.env*",
        "*.config.*",
        "**/*.config.*",
        "settings.*",
        "**/settings.*",
    ),
    "dependabot_configuration": (
        ".github/dependabot.yml",
        ".github/dependabot.yaml",
    ),
}

DEPENDENCY_CATEGORIES = frozenset({"dependency_manifest", "dependency_lockfile", "user_dependency"})
SENSITIVE_CATEGORIES = frozenset(
    {
        "ci_workflow",
        "deployment_infrastructure",
        "auth_security",
        "runtime_configuration",
        "dependabot_configuration",
        "user_sensitive",
    }
)

DEPENDABOT_ECOSYSTEM_BRANCH_SEGMENTS = {
    "actions": "github_actions",
    "bundler": "bundler",
    "cargo": "cargo",
    "composer": "composer",
    "devcontainers": "devcontainers",
    "docker": "docker",
    "elm": "elm",
    "github_actions": "github_actions",
    "gitsubmodule": "gitsubmodule",
    "go": "go_modules",
    "gomod": "go_modules",
    "gradle": "gradle",
    "maven": "maven",
    "mix": "mix",
    "npm": "npm_and_yarn",
    "nuget": "nuget",
    "pip": "pip",
    "pub": "pub",
    "rubygems": "bundler",
    "rust": "cargo",
    "swift": "swift",
}


@dataclass(frozen=True)
class PathPolicy:
    dependency_patterns: tuple[str, ...]
    sensitive_patterns: tuple[str, ...]


def build_path_policy(
    global_overrides: PathPolicyOverrides,
    repository_overrides: PathPolicyOverrides,
) -> PathPolicy:
    return PathPolicy(
        dependency_patterns=tuple(
            dict.fromkeys((*global_overrides.dependency_patterns, *repository_overrides.dependency_patterns))
        ),
        sensitive_patterns=tuple(
            dict.fromkeys((*global_overrides.sensitive_patterns, *repository_overrides.sensitive_patterns))
        ),
    )


def _matches(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return fnmatch.fnmatchcase(normalized, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(normalized, pattern[3:])
    )


def classify_path(path: str, policy: PathPolicy) -> dict[str, Any]:
    categories = [
        category
        for category, patterns in BUILTIN_PATH_PATTERNS.items()
        if any(_matches(path, pattern) for pattern in patterns)
    ]
    if any(_matches(path, pattern) for pattern in policy.dependency_patterns):
        categories.append("user_dependency")
    if any(_matches(path, pattern) for pattern in policy.sensitive_patterns):
        categories.append("user_sensitive")
    categories = list(dict.fromkeys(categories))
    return {
        "path": path,
        "categories": categories,
        "dependency_related": bool(DEPENDENCY_CATEGORIES.intersection(categories)),
        "sensitive": bool(SENSITIVE_CATEGORIES.intersection(categories)),
        "classified": bool(categories),
    }


def assess_paths(paths: Iterable[str], policy: PathPolicy) -> dict[str, Any]:
    files = [classify_path(path, policy) for path in paths]
    if not files or any(not item["classified"] for item in files):
        classification = "unclassified"
    elif any(item["sensitive"] for item in files):
        classification = "sensitive"
    elif all(item["dependency_related"] for item in files):
        classification = "dependency_only"
    else:
        classification = "unclassified"
    return {
        "classification": classification,
        "files": files,
        "unclassified_paths": [item["path"] for item in files if not item["classified"]],
        "sensitive_paths": [item["path"] for item in files if item["sensitive"]],
    }


def check_state(checks: list[dict[str, Any]] | None) -> str:
    if not checks:
        return "missing"
    pending = False
    unknown = False
    for check in checks:
        conclusion = str(check.get("conclusion") or "").upper()
        state = str(check.get("state") or "").upper()
        status = str(check.get("status") or "").upper()
        if conclusion in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}:
            return "failing"
        if state in {"FAILURE", "ERROR"}:
            return "failing"
        if (
            status in {"QUEUED", "REQUESTED", "WAITING", "PENDING", "IN_PROGRESS"}
            or state in {"EXPECTED", "PENDING"}
            or (not conclusion and not state)
        ):
            pending = True
            continue
        effective = conclusion or state
        if effective not in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            unknown = True
    if pending:
        return "pending"
    return "unknown" if unknown else "passing"


def sensitive_recent_merges(
    merged_pull_requests: Iterable[dict[str, Any]],
    policy: PathPolicy,
) -> list[dict[str, Any]]:
    sensitive: list[dict[str, Any]] = []
    for pull_request in merged_pull_requests:
        author = str((pull_request.get("author") or {}).get("login") or "").casefold()
        if author in DEPENDABOT_AUTHORS:
            continue
        file_assessment = assess_paths(
            (str(item.get("path") or "") for item in pull_request.get("files") or [] if item.get("path")),
            policy,
        )
        matched = [
            item for item in file_assessment["files"] if item["sensitive"]
        ]
        if not matched:
            continue
        sensitive.append(
            {
                "number": pull_request.get("number"),
                "title": pull_request.get("title"),
                "url": pull_request.get("url"),
                "merged_at": pull_request.get("mergedAt"),
                "matched_files": matched,
            }
        )
    return sensitive


def assess_pull_request(
    *,
    repository: str,
    expected_base: str,
    details: dict[str, Any],
    recent_sensitive: Iterable[dict[str, Any]],
    policy: PathPolicy,
    sensitive_history_complete: bool = True,
) -> dict[str, Any]:
    changed_paths = [
        str(item.get("path"))
        for item in details.get("files") or []
        if item.get("path")
    ]
    path_assessment = assess_paths(changed_paths, policy)
    checks = check_state(details.get("statusCheckRollup"))
    author = str((details.get("author") or {}).get("login") or "")
    identity = {
        "repository": repository,
        "number": details.get("number"),
        "url": details.get("url"),
        "title": details.get("title"),
        "expected_base": expected_base,
        "observed_base": details.get("baseRefName"),
        "head_ref": details.get("headRefName"),
        "head_sha": details.get("headRefOid"),
    }
    reasons: list[str] = []
    if identity["observed_base"] != expected_base:
        reasons.append("Pull request targets a branch other than the configured default branch.")
    if author.casefold() not in DEPENDABOT_AUTHORS:
        reasons.append("Pull-request author is not a recognized Dependabot identity.")
    if details.get("isDraft"):
        reasons.append("Pull request is a draft.")
    if not identity["head_sha"]:
        reasons.append("Pull request has no collected head commit identity.")
    mergeable = str(details.get("mergeable") or "UNKNOWN").upper()
    merge_state = str(details.get("mergeStateStatus") or "UNKNOWN").upper()
    if mergeable != "MERGEABLE":
        reasons.append(f"Pull request mergeability is {mergeable.lower()}.")
    if merge_state != "CLEAN":
        reasons.append(f"Pull request merge state is {merge_state.lower()}.")
    if checks != "passing":
        reasons.append(f"Status checks are {checks}.")
    if path_assessment["classification"] != "dependency_only":
        reasons.append(
            f"Changed paths are classified as {path_assessment['classification'].replace('_', ' ')}."
        )
    if not sensitive_history_complete:
        reasons.append("Recent sensitive-change history is incomplete.")

    sensitive_paths = {
        matched["path"]
        for merge in recent_sensitive
        for matched in merge.get("matched_files", [])
        if matched.get("path")
    }
    overlap = sorted(set(changed_paths).intersection(sensitive_paths))
    if overlap:
        reasons.append("Changed paths overlap recent sensitive non-Dependabot merges.")

    return {
        "candidate_identity": identity,
        "author": author,
        "updated_at": details.get("updatedAt"),
        "mergeable": details.get("mergeable"),
        "merge_state_status": details.get("mergeStateStatus"),
        "is_draft": bool(details.get("isDraft")),
        "checks": checks,
        "path_assessment": path_assessment,
        "recent_sensitive_overlap": overlap,
        "classification": "baseline_candidate" if not reasons else "attention_required",
        "reasons": reasons,
        "mutation_authorized": False,
    }


def _dependabot_update_from_title(title: str) -> tuple[str, str] | None:
    match = re.search(
        r"(?:^|:\s*)bump\s+`?(?P<package>[^`\s]+)`?\s+from\s+\S+\s+to\s+`?(?P<target>[^`\s]+)`?",
        title,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return (match.group("package").casefold(), match.group("target").casefold().lstrip("v"))


def _normalized_repository_path(value: object) -> str | None:
    return repository_relative_path(value)


def _release_version_key(value: object) -> tuple[int, ...] | None:
    normalized = str(value or "").strip().casefold().lstrip("v")
    if not re.fullmatch(r"\d+(?:\.\d+)*", normalized):
        return None
    segments = [int(item) for item in normalized.split(".")]
    while len(segments) > 1 and segments[-1] == 0:
        segments.pop()
    return tuple(segments)


def compare_patched_versions(target: object, first_patched: object) -> str:
    target_text = str(target or "").strip().casefold().lstrip("v")
    patched_text = str(first_patched or "").strip().casefold().lstrip("v")
    if not target_text or not patched_text:
        return "unverified"
    target_key = _release_version_key(target_text)
    patched_key = _release_version_key(patched_text)
    if target_key is None or patched_key is None:
        return "unverified"
    if target_text == patched_text:
        return "exact"
    if target_key == patched_key:
        return "equivalent_release"
    if target_key > patched_key:
        return "newer_release"
    return "older_release"


def _dependabot_pr_relationship(
    pull_request: dict[str, Any],
    *,
    package_name: str,
    ecosystem: str,
) -> tuple[str, frozenset[str]] | None:
    author = str((pull_request.get("author") or {}).get("login") or "").casefold()
    if author not in DEPENDABOT_AUTHORS:
        return None
    update = _dependabot_update_from_title(str(pull_request.get("title") or ""))
    if not update or update[0] != package_name:
        return None
    expected_ecosystem = DEPENDABOT_ECOSYSTEM_BRANCH_SEGMENTS.get(ecosystem)
    head_ref = str(pull_request.get("headRefName") or "")
    head_parts = head_ref.split("/", 2)
    observed_ecosystem = head_parts[1] if len(head_parts) >= 3 and head_parts[0] == "dependabot" else ""
    if not expected_ecosystem or observed_ecosystem != expected_ecosystem:
        return None
    changed_paths = frozenset(
        normalized
        for item in pull_request.get("files") or []
        if isinstance(item, dict)
        and (normalized := _normalized_repository_path(item.get("path"))) is not None
    )
    return (update[1], changed_paths)


def _related_dependabot_pr(
    pull_request: dict[str, Any],
    *,
    package_name: str,
    ecosystem: str,
    manifest_path: str | None,
) -> tuple[bool, str | None]:
    relationship = _dependabot_pr_relationship(
        pull_request,
        package_name=package_name,
        ecosystem=ecosystem,
    )
    if not relationship or not manifest_path:
        return (False, None)
    target_version, changed_paths = relationship
    if manifest_path not in changed_paths:
        return (False, None)
    return (True, target_version)


def normalize_alert(alert: dict[str, Any], repository: str) -> dict[str, Any]:
    dependency_value = alert.get("dependency")
    dependency = dependency_value if isinstance(dependency_value, dict) else {}
    package_value = dependency.get("package")
    package = package_value if isinstance(package_value, dict) else {}
    vulnerability_value = alert.get("security_vulnerability")
    vulnerability = vulnerability_value if isinstance(vulnerability_value, dict) else {}
    patched_value = vulnerability.get("first_patched_version")
    patched = patched_value if isinstance(patched_value, dict) else {}
    advisory_value = alert.get("security_advisory")
    advisory = advisory_value if isinstance(advisory_value, dict) else {}
    return {
        "repository": repository,
        "number": alert.get("number"),
        "state": alert.get("state"),
        "dependency_name": package.get("name"),
        "ecosystem": package.get("ecosystem"),
        "manifest_path": dependency.get("manifest_path"),
        "scope": dependency.get("scope"),
        "relationship": dependency.get("relationship"),
        "severity": vulnerability.get("severity"),
        "vulnerable_version_range": vulnerability.get("vulnerable_version_range"),
        "first_patched_version": patched.get("identifier"),
        "url": vulnerability.get("html_url") or alert.get("html_url"),
        "advisory_summary": advisory.get("summary"),
    }


def correlate_alerts(
    repository: str,
    alerts: Iterable[dict[str, Any]],
    open_pull_requests: Iterable[dict[str, Any]],
    recent_merged: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    open_prs = list(open_pull_requests)
    merged_prs = list(recent_merged)
    correlated: list[dict[str, Any]] = []
    for raw_alert in alerts:
        alert = normalize_alert(raw_alert, repository)
        package = str(alert.get("dependency_name") or "").casefold()
        ecosystem = str(alert.get("ecosystem") or "").casefold().replace("-", "_")
        manifest_path = _normalized_repository_path(alert.get("manifest_path"))
        first_patched = str(alert.get("first_patched_version") or "").casefold().lstrip("v")
        matching_open: list[dict[str, Any]] = []
        package_related_open: list[dict[str, Any]] = []
        for item in open_prs:
            relationship = _dependabot_pr_relationship(
                item,
                package_name=package,
                ecosystem=ecosystem,
            )
            if not relationship:
                continue
            target_version, changed_paths = relationship
            patched_version_relation = compare_patched_versions(
                target_version,
                first_patched,
            )
            evidence = {
                "number": item.get("number"),
                "title": item.get("title"),
                "url": item.get("url"),
                "target_version": target_version,
                "manifest_path_matches": bool(manifest_path and manifest_path in changed_paths),
                "patched_version_relation": patched_version_relation,
                "meets_first_patched_version": patched_version_relation
                in {"exact", "equivalent_release", "newer_release"},
            }
            package_related_open.append(evidence)
            if evidence["manifest_path_matches"]:
                matching_open.append(evidence)
        matching_merged = [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "url": item.get("url"),
                "merged_at": item.get("mergedAt"),
            }
            for item in merged_prs
            if _related_dependabot_pr(
                item,
                package_name=package,
                ecosystem=ecosystem,
                manifest_path=manifest_path,
            )[0]
        ]
        if any(item["meets_first_patched_version"] for item in matching_open):
            classification = "likely_covered_by_open_pr"
        elif matching_open:
            classification = "related_open_pr_unverified"
        elif matching_merged:
            classification = "possibly_covered_by_recent_merge"
        else:
            classification = "unresolved_without_pr"
        correlated.append(
            {
                **alert,
                "classification": classification,
                "matching_open_pull_requests": matching_open,
                "package_related_open_pull_requests": package_related_open,
                "matching_recent_merges": matching_merged[:5],
            }
        )
    return correlated
