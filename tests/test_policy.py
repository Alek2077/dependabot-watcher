from __future__ import annotations

import pytest

from dependabot_watcher.config import PathPolicyOverrides
from dependabot_watcher.policy import (
    assess_paths,
    assess_pull_request,
    build_path_policy,
    check_state,
    correlate_alerts,
    normalized_status_checks,
    sensitive_recent_merges,
)


def policy():
    return build_path_policy(
        PathPolicyOverrides(sensitive_patterns=("ops/production/**",)),
        PathPolicyOverrides(dependency_patterns=("deps/*.lock",)),
    )


def pull_request(*, files: list[str] | None = None) -> dict[str, object]:
    return {
        "number": 42,
        "title": "Bump example-lib from 1.0.0 to 1.1.0",
        "url": "https://github.com/example-org/example-app/pull/42",
        "updatedAt": "2026-09-19T10:00:00Z",
        "headRefName": "dependabot/pip/example-lib-1.1.0",
        "headRefOid": "abc123",
        "baseRefName": "main",
        "author": {"login": "app/dependabot"},
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "isDraft": False,
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "files": [{"path": path} for path in (files or ["pyproject.toml", "uv.lock"])],
    }


def test_dependency_only_paths_are_recognized_across_ecosystems() -> None:
    assessment = assess_paths(["package.json", "uv.lock", "services/api/go.mod"], policy())

    assert assessment["classification"] == "dependency_only"
    assert assessment["unclassified_paths"] == []


def test_dot_prefixed_workflow_path_is_sensitive() -> None:
    assessment = assess_paths([".github/workflows/release.yml"], policy())

    assert assessment["classification"] == "sensitive"
    assert assessment["files"][0]["categories"] == ["ci_workflow"]


def test_unknown_path_remains_unclassified() -> None:
    assessment = assess_paths(["src/runtime.py"], policy())

    assert assessment["classification"] == "unclassified"
    assert assessment["unclassified_paths"] == ["src/runtime.py"]


def test_user_overrides_extend_builtins() -> None:
    assessment = assess_paths(["deps/custom.lock", "ops/production/service.yml"], policy())

    assert assessment["classification"] == "sensitive"
    assert assessment["files"][0]["dependency_related"] is True
    assert assessment["files"][1]["sensitive"] is True


def test_check_state_is_conservative() -> None:
    assert check_state([]) == "missing"
    assert check_state([{"status": "IN_PROGRESS", "conclusion": None}]) == "pending"
    assert check_state([{"status": "COMPLETED", "conclusion": "FAILURE"}]) == "failing"
    assert check_state([{"status": "COMPLETED", "conclusion": "SUCCESS"}]) == "passing"
    assert check_state([{"state": "PENDING"}]) == "pending"


def test_status_check_normalization_preserves_github_check_kinds_and_duplicates() -> None:
    normalized = normalized_status_checks(
        [
            {"name": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "CI", "status": "COMPLETED", "conclusion": "SKIPPED"},
            {"context": "legacy-ci", "state": "SUCCESS"},
            {"name": "Malformed", "status": "BOGUS", "conclusion": "SUCCESS"},
            {"name": 123, "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": " CI ", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
    )

    assert [item["name"] for item in normalized] == [
        "CI",
        "CI",
        "legacy-ci",
        "Malformed",
    ]
    assert normalized[0]["kind"] == "check_run"
    assert normalized[1]["kind"] == "check_run"
    assert normalized[2] == {
        "name": "legacy-ci",
        "kind": "status_context",
        "state": "passing",
        "status": "",
        "conclusion": "SUCCESS",
    }
    assert normalized[3]["state"] == "unknown"


def test_malformed_status_context_cannot_look_passing_after_normalization() -> None:
    normalized = normalized_status_checks(
        [{"context": "legacy-ci", "state": "SUCCESS", "conclusion": "SKIPPED"}]
    )

    assert normalized == [
        {
            "name": "legacy-ci",
            "kind": "status_context",
            "state": "unknown",
            "status": "",
            "conclusion": "SUCCESS",
        }
    ]


@pytest.mark.parametrize(
    "check",
    [
        {"name": "CI", "status": "COMPLETED", "conclusion": False},
        {"name": "CI", "status": False, "conclusion": "SUCCESS"},
        {"name": "CI", "context": 123, "status": "COMPLETED", "conclusion": "SUCCESS"},
        {
            "__typename": "StatusContext",
            "name": "CI",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        },
        {
            "__typename": "CheckRun",
            "name": "CI",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "state": "NEUTRAL",
        },
        {"context": "CI", "state": "SUCCESS", "conclusion": False},
        {"context": "CI", "state": "SUCCESS", "status": "COMPLETED"},
    ],
)
def test_non_string_status_fields_normalize_as_unknown(
    check: dict[str, object],
) -> None:
    normalized = normalized_status_checks([check])

    assert len(normalized) == 1
    assert normalized[0]["name"] == "CI"
    assert normalized[0]["state"] == "unknown"


def test_pull_request_assessment_never_authorizes_mutation() -> None:
    result = assess_pull_request(
        repository="example-org/example-app",
        expected_base="main",
        details=pull_request(),
        recent_sensitive=[],
        policy=policy(),
    )

    assert result["classification"] == "baseline_candidate"
    assert result["candidate_identity"]["head_sha"] == "abc123"
    assert result["mutation_authorized"] is False


def test_recent_sensitive_overlap_requires_attention() -> None:
    details = pull_request(files=["pyproject.toml", ".github/workflows/test.yml"])
    result = assess_pull_request(
        repository="example-org/example-app",
        expected_base="main",
        details=details,
        recent_sensitive=[
            {
                "matched_files": [
                    {"path": ".github/workflows/test.yml", "categories": ["ci_workflow"]}
                ]
            }
        ],
        policy=policy(),
    )

    assert result["classification"] == "attention_required"
    assert result["recent_sensitive_overlap"] == [".github/workflows/test.yml"]


def test_conflicting_pull_request_requires_attention() -> None:
    details = pull_request()
    details["mergeable"] = "CONFLICTING"
    details["mergeStateStatus"] = "DIRTY"

    result = assess_pull_request(
        repository="example-org/example-app",
        expected_base="main",
        details=details,
        recent_sensitive=[],
        policy=policy(),
    )

    assert result["classification"] == "attention_required"
    assert "conflicting" in " ".join(result["reasons"])
    assert "dirty" in " ".join(result["reasons"])


def test_sensitive_history_excludes_dependabot_and_keeps_custom_change() -> None:
    recent = [
        {
            "number": 1,
            "author": {"login": "app/dependabot"},
            "files": [{"path": ".github/workflows/test.yml"}],
        },
        {
            "number": 2,
            "author": {"login": "maintainer"},
            "files": [{"path": "wrangler.jsonc"}],
        },
    ]

    sensitive = sensitive_recent_merges(recent, policy())

    assert [item["number"] for item in sensitive] == [2]


def test_alert_correlation_uses_exact_package_name() -> None:
    alerts = [
        {
            "number": 7,
            "state": "open",
            "dependency": {
                "package": {"name": "ai", "ecosystem": "npm"},
                "manifest_path": "package.json",
            },
            "security_vulnerability": {
                "severity": "high",
                "first_patched_version": {"identifier": "4.1.0"},
            },
        }
    ]
    open_prs = [
        {
            "number": 1,
            "title": "Bump tailwind from 1.0.0 to 1.1.0",
            "url": "example",
            "author": {"login": "app/dependabot"},
            "headRefName": "dependabot/npm_and_yarn/tailwind-1.1.0",
            "files": [{"path": "package.json"}],
        },
        {
            "number": 2,
            "title": "Bump ai from 4.0.0 to 4.1.0",
            "url": "example",
            "author": {"login": "app/dependabot"},
            "headRefName": "dependabot/npm_and_yarn/ai-4.1.0",
            "files": [{"path": "package.json"}],
        },
    ]

    result = correlate_alerts("example-org/example-app", alerts, open_prs, [])

    assert result[0]["classification"] == "likely_covered_by_open_pr"
    assert [item["number"] for item in result[0]["matching_open_pull_requests"]] == [2]
    assert [item["number"] for item in result[0]["package_related_open_pull_requests"]] == [2]
    assert result[0]["package_related_open_pull_requests"][0]["manifest_path_matches"] is True


def test_alert_correlation_does_not_trust_title_alone() -> None:
    alerts = [
        {
            "number": 7,
            "state": "open",
            "dependency": {
                "package": {"name": "ai", "ecosystem": "npm"},
                "manifest_path": "package.json",
            },
            "security_vulnerability": {
                "severity": "high",
                "first_patched_version": {"identifier": "2.0.0"},
            },
        }
    ]
    same_title_wrong_author = {
        "number": 2,
        "title": "Bump ai from 1.0.0 to 2.0.0",
        "url": "example",
        "author": {"login": "untrusted-user"},
        "headRefName": "dependabot/npm_and_yarn/ai-2.0.0",
        "files": [{"path": "package.json"}],
    }

    result = correlate_alerts(
        "example-org/example-app",
        alerts,
        [same_title_wrong_author],
        [],
    )

    assert result[0]["classification"] == "unresolved_without_pr"
    assert result[0]["matching_open_pull_requests"] == []


def test_related_pr_with_unverified_target_remains_unresolved() -> None:
    alerts = [
        {
            "number": 7,
            "state": "open",
            "dependency": {
                "package": {"name": "ai", "ecosystem": "npm"},
                "manifest_path": "package.json",
            },
            "security_vulnerability": {
                "severity": "high",
                "first_patched_version": {"identifier": "2.0.0"},
            },
        }
    ]
    unpatched = {
        "number": 2,
        "title": "Bump ai from 1.0.0 to 1.1.0",
        "url": "example",
        "author": {"login": "app/dependabot"},
        "headRefName": "dependabot/npm_and_yarn/ai-1.1.0",
        "files": [{"path": "package.json"}],
    }

    result = correlate_alerts("example-org/example-app", alerts, [unpatched], [])

    assert result[0]["classification"] == "related_open_pr_unverified"
    match = result[0]["matching_open_pull_requests"][0]
    assert match["patched_version_relation"] == "older_release"
    assert match["meets_first_patched_version"] is False


def test_package_related_pr_with_different_manifest_remains_unresolved() -> None:
    alerts = [
        {
            "number": 7,
            "state": "open",
            "dependency": {
                "package": {"name": "example-lib", "ecosystem": "pip"},
                "manifest_path": "requirements-dev.txt",
            },
            "security_vulnerability": {
                "severity": "high",
                "first_patched_version": {"identifier": "1.1.0"},
            },
        }
    ]
    same_package_other_manifest = {
        "number": 42,
        "title": "Bump example-lib from 1.0.0 to 1.1.0",
        "url": "example",
        "author": {"login": "app/dependabot"},
        "headRefName": "dependabot/pip/example-lib-1.1.0",
        "files": [{"path": "requirements.txt"}],
    }

    result = correlate_alerts(
        "example-org/example-app",
        alerts,
        [same_package_other_manifest],
        [],
    )

    assert result[0]["classification"] == "unresolved_without_pr"
    assert result[0]["matching_open_pull_requests"] == []
    relationship = result[0]["package_related_open_pull_requests"][0]
    assert relationship["number"] == 42
    assert relationship["manifest_path_matches"] is False
    assert relationship["patched_version_relation"] == "exact"
    assert relationship["meets_first_patched_version"] is True


def test_alert_correlation_accepts_equivalent_release_segments() -> None:
    alerts = [
        {
            "number": 7,
            "state": "open",
            "dependency": {
                "package": {"name": "soupsieve", "ecosystem": "pip"},
                "manifest_path": "requirements.txt",
            },
            "security_vulnerability": {
                "severity": "medium",
                "first_patched_version": {"identifier": "2.9.0"},
            },
        }
    ]
    open_prs = [
        {
            "number": 501,
            "title": "Bump soupsieve from 2.8.4 to 2.9",
            "url": "example",
            "author": {"login": "app/dependabot"},
            "headRefName": "dependabot/pip/soupsieve-2.9",
            "files": [{"path": "requirements.txt"}],
        }
    ]

    result = correlate_alerts("example-org/example-app", alerts, open_prs, [])

    assert result[0]["classification"] == "likely_covered_by_open_pr"
    match = result[0]["matching_open_pull_requests"][0]
    assert match["patched_version_relation"] == "equivalent_release"
    assert match["meets_first_patched_version"] is True


def test_alert_correlation_does_not_normalize_qualified_versions() -> None:
    alerts = [
        {
            "number": 7,
            "state": "open",
            "dependency": {
                "package": {"name": "example", "ecosystem": "pip"},
                "manifest_path": "requirements.txt",
            },
            "security_vulnerability": {
                "severity": "high",
                "first_patched_version": {"identifier": "2.9.0"},
            },
        }
    ]
    prerelease_pr = {
        "number": 8,
        "title": "Bump example from 2.8.0 to 2.9.0rc1",
        "url": "example",
        "author": {"login": "app/dependabot"},
        "headRefName": "dependabot/pip/example-2.9.0rc1",
        "files": [{"path": "requirements.txt"}],
    }

    result = correlate_alerts("example-org/example-app", alerts, [prerelease_pr], [])

    assert result[0]["classification"] == "related_open_pr_unverified"
    match = result[0]["matching_open_pull_requests"][0]
    assert match["patched_version_relation"] == "unverified"
    assert match["meets_first_patched_version"] is False


def test_alert_correlation_does_not_accept_identical_qualified_versions() -> None:
    alerts = [
        {
            "number": 7,
            "state": "open",
            "dependency": {
                "package": {"name": "example", "ecosystem": "pip"},
                "manifest_path": "requirements.txt",
            },
            "security_vulnerability": {
                "severity": "high",
                "first_patched_version": {"identifier": "2.9.0rc1"},
            },
        }
    ]
    same_qualified_pr = {
        "number": 8,
        "title": "Bump example from 2.8.0 to 2.9.0rc1",
        "url": "example",
        "author": {"login": "app/dependabot"},
        "headRefName": "dependabot/pip/example-2.9.0rc1",
        "files": [{"path": "requirements.txt"}],
    }

    result = correlate_alerts("example-org/example-app", alerts, [same_qualified_pr], [])

    assert result[0]["classification"] == "related_open_pr_unverified"
    match = result[0]["matching_open_pull_requests"][0]
    assert match["patched_version_relation"] == "unverified"
    assert match["meets_first_patched_version"] is False


def test_alert_correlation_accepts_newer_plain_release() -> None:
    alerts = [
        {
            "number": 7,
            "state": "open",
            "dependency": {
                "package": {"name": "example", "ecosystem": "pip"},
                "manifest_path": "requirements.txt",
            },
            "security_vulnerability": {
                "severity": "high",
                "first_patched_version": {"identifier": "2.9.0"},
            },
        }
    ]
    newer_pr = {
        "number": 8,
        "title": "Bump example from 2.8.0 to 2.10.0",
        "url": "example",
        "author": {"login": "app/dependabot"},
        "headRefName": "dependabot/pip/example-2.10.0",
        "files": [{"path": "requirements.txt"}],
    }

    result = correlate_alerts("example-org/example-app", alerts, [newer_pr], [])

    assert result[0]["classification"] == "likely_covered_by_open_pr"
    match = result[0]["matching_open_pull_requests"][0]
    assert match["patched_version_relation"] == "newer_release"
    assert match["meets_first_patched_version"] is True


@pytest.mark.parametrize("manifest_path", ["/requirements.txt", "../requirements.txt"])
def test_alert_correlation_does_not_promote_unsafe_manifest_path(
    manifest_path: str,
) -> None:
    alerts = [
        {
            "number": 7,
            "state": "open",
            "dependency": {
                "package": {"name": "example", "ecosystem": "pip"},
                "manifest_path": manifest_path,
            },
            "security_vulnerability": {
                "severity": "high",
                "first_patched_version": {"identifier": "2.9.0"},
            },
        }
    ]
    pull_request = {
        "number": 8,
        "title": "Bump example from 2.8.0 to 2.9.0",
        "url": "example",
        "author": {"login": "app/dependabot"},
        "headRefName": "dependabot/pip/example-2.9.0",
        "files": [{"path": "requirements.txt"}],
    }

    result = correlate_alerts(
        "example-org/example-app", alerts, [pull_request], []
    )

    assert result[0]["classification"] != "likely_covered_by_open_pr"
    assert result[0]["matching_open_pull_requests"] == []
