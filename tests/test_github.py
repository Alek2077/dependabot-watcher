from __future__ import annotations

import base64
import json
import subprocess
from typing import Any, Callable, Sequence
from urllib.parse import parse_qs, urlsplit

import pytest

from dependabot_watcher import github
from dependabot_watcher.github import GhCliClient, GitHubError, GitHubRequestRejected


def test_failed_cli_command_withholds_raw_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "credential=must-not-escape"
    monkeypatch.setattr(
        github.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout="", stderr=secret
        ),
    )

    with pytest.raises(GitHubError) as raised:
        github._run_command(["gh", "auth", "status"], 30)

    assert secret not in str(raised.value)
    assert "output was withheld" in str(raised.value)


@pytest.mark.parametrize('status,definite', [(403, True), (404, True), (422, True), (500, False)])
def test_http_failure_preserves_definite_rejection_without_leaking_output(
    monkeypatch: pytest.MonkeyPatch, status: int, definite: bool,
) -> None:
    monkeypatch.setattr(github.subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 1, stdout=f'HTTP/2.0 {status} Failed\n\nPRIVATE_RESPONSE', stderr='PRIVATE_ERROR'))
    with pytest.raises(GitHubError) as raised:
        github._run_command(['gh', 'api', '--include'], 30)
    assert isinstance(raised.value, GitHubRequestRejected) is definite
    assert 'PRIVATE' not in str(raised.value)


def open_pr(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Bump example-lib to {number}.0.0",
        "url": f"https://github.com/example-org/example-app/pull/{number}",
        "updatedAt": "2026-09-21T08:00:00Z",
        "headRefName": f"dependabot/pip/example-lib-{number}.0.0",
        "headRefOid": f"sha-{number}",
        "baseRefName": "main",
        "author": {"login": "app/dependabot"},
    }


def alert(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "dependency": {
            "package": {"name": "example-lib", "ecosystem": "pip"},
            "manifest_path": "pyproject.toml",
        },
        "security_vulnerability": {"severity": "high"},
    }


def pull_request_details(number: int) -> dict[str, Any]:
    return {
        **open_pr(number),
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "isDraft": False,
        "statusCheckRollup": [],
        "files": [{"path": "pyproject.toml"}],
    }


def test_branch_head_uses_read_only_commit_lookup() -> None:
    captured: list[str] = []

    def runner(command: Sequence[str], timeout: int) -> str:
        captured.extend(command)
        return json.dumps({"sha": "base123"})

    result = GhCliClient(runner=runner).fetch_branch_head(
        "example-org/example-app", "release/next"
    )

    assert result == "base123"
    assert captured == [
        "gh",
        "api",
        "/repos/example-org/example-app/commits/release%2Fnext",
    ]


def test_text_file_read_is_bounded_and_decodes_base64() -> None:
    captured: list[str] = []
    content = b"-r requirements.txt\n"

    def runner(command: Sequence[str], timeout: int) -> str:
        captured.extend(command)
        encoded = base64.b64encode(content).decode("ascii")
        return json.dumps(
            {
                "type": "file",
                "size": len(content),
                "encoding": "base64",
                "content": encoded[:8] + "\n" + encoded[8:],
            }
        )

    result = GhCliClient(runner=runner).fetch_text_file(
        "example-org/example-app",
        "requirements dev.txt",
        "base123",
        100,
    )

    assert result == content.decode("utf-8")
    assert captured == [
        "gh",
        "api",
        "/repos/example-org/example-app/contents/requirements%20dev.txt?ref=base123",
    ]


def test_text_file_read_rejects_oversized_metadata_before_decode() -> None:
    def runner(command: Sequence[str], timeout: int) -> str:
        return json.dumps(
            {"type": "file", "size": 101, "encoding": "base64", "content": ""}
        )

    with pytest.raises(GitHubError, match="configured ceiling"):
        GhCliClient(runner=runner).fetch_text_file(
            "example-org/example-app", "requirements.txt", "base123", 100
        )


def test_pull_request_state_lookup_is_read_only_and_identity_bound() -> None:
    captured: list[str] = []

    def runner(command: Sequence[str], timeout: int) -> str:
        captured.extend(command)
        return json.dumps(
            {
                "number": 42,
                "state": "OPEN",
                "mergedAt": None,
                "headRefOid": "a" * 40,
                "baseRefName": "main",
                "url": "https://github.com/example-org/example-app/pull/42",
            }
        )

    state = GhCliClient(runner=runner).fetch_pull_request_state(
        "example-org/example-app", 42
    )

    assert state["state"] == "OPEN"
    assert captured[:4] == ["gh", "pr", "view", "42"]
    assert "--repo" in captured
    assert "mergeCommit" in captured[captured.index("--json") + 1].split(",")


@pytest.mark.parametrize("status,behind,merge_base,expected", [
    ("ahead", 0, "a" * 40, True), ("identical", 0, "a" * 40, True),
    ("behind", 1, "b" * 40, False), ("diverged", 2, "c" * 40, False),
    ("ahead", 0, "c" * 40, False),
])
def test_exact_commit_lineage_is_read_only_and_does_not_follow_branch_names(
    status: str, behind: int, merge_base: str, expected: bool,
) -> None:
    commands: list[Sequence[str]] = []

    def runner(command: Sequence[str], timeout: int) -> str:
        commands.append(command)
        return json.dumps({"status": status, "behind_by": behind,
                           "base_sha": "a" * 40, "merge_base_sha": merge_base})

    assert GhCliClient(runner=runner).commit_is_ancestor("org/repo", "a" * 40, "b" * 40) is expected
    assert commands[0][0:2] == ["gh", "api"]
    assert commands[0][2] == "/repos/org/repo/compare/" + "a" * 40 + "..." + "b" * 40 + "?per_page=1"


@pytest.mark.parametrize("changes", [{"status": []}, {"behind_by": True}, {"base_sha": "c" * 40},
                                       {"merge_base_sha": "invalid"}, {"status": "unknown"}])
def test_invalid_commit_lineage_is_not_treated_as_success(changes: dict[str, Any]) -> None:
    value = {"status": "ahead", "behind_by": 0, "base_sha": "a" * 40, "merge_base_sha": "a" * 40, **changes}
    with pytest.raises(GitHubError, match="lineage"):
        GhCliClient(runner=lambda *args: json.dumps(value)).commit_is_ancestor("org/repo", "a" * 40, "b" * 40)


def test_commit_lineage_rejects_symbolic_or_short_revisions_before_network() -> None:
    def runner(*args: Any) -> str:
        raise AssertionError("Invalid revision must not cause a request")
    with pytest.raises(GitHubError, match="full revision"):
        GhCliClient(runner=runner).commit_is_ancestor("org/repo", "main", "b" * 40)


def test_merge_uses_exact_repository_pr_head_and_method() -> None:
    captured: list[str] = []

    def runner(command: Sequence[str], timeout: int) -> str:
        captured.extend(command)
        return json.dumps({"merged": True, "message": "merged", "sha": "c" * 40})

    result = GhCliClient(runner=runner).merge_pull_request(
        "example-org/example-app",
        42,
        head_sha="a" * 40,
        merge_method="squash",
    )

    assert result["merged"] is True
    assert captured == [
        "gh",
        "api",
        "--method",
        "PUT",
        "/repos/example-org/example-app/pulls/42/merge",
        "-f",
        f"sha={'a' * 40}",
        "-f",
        "merge_method=squash",
    ]


def test_merge_rejects_unbound_sha_before_github_call() -> None:
    def runner(command: Sequence[str], timeout: int) -> str:
        pytest.fail("Invalid execution identity must fail before a GitHub call")

    with pytest.raises(GitHubError, match="full Git commit SHA"):
        GhCliClient(runner=runner).merge_pull_request(
            "example-org/example-app", 42, head_sha="short", merge_method="squash"
        )


def test_merge_rejects_invalid_response_sha() -> None:
    def runner(command: Sequence[str], timeout: int) -> str:
        return json.dumps({"merged": True, "message": "merged", "sha": "not-a-sha"})

    with pytest.raises(GitHubError, match="invalid merge commit SHA"):
        GhCliClient(runner=runner).merge_pull_request(
            "example-org/example-app",
            42,
            head_sha="a" * 40,
            merge_method="squash",
        )

@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../private.txt",
        "/requirements.txt",
        "https://example.invalid/requirements.txt",
        "$DEPENDENCY_FILE.txt",
        "config//requirements.txt",
    ],
)
def test_text_file_read_rejects_unsafe_path_before_github_call(
    unsafe_path: str,
) -> None:
    def runner(command: Sequence[str], timeout: int) -> str:
        pytest.fail("Unsafe paths must be rejected before a GitHub call")

    with pytest.raises(GitHubError, match="repository-relative"):
        GhCliClient(runner=runner).fetch_text_file(
            "example-org/example-app", unsafe_path, "base123", 100
        )


def test_open_pr_collection_uses_read_only_command_and_reports_truncation() -> None:
    captured: list[str] = []

    def runner(command: Sequence[str], timeout: int) -> str:
        captured.extend(command)
        assert timeout == 30
        return json.dumps([open_pr(1), open_pr(2)])

    result = GhCliClient(timeout_seconds=30, runner=runner).list_open_dependabot_prs(
        "example-org/example-app", 1
    )

    assert result.truncated is True
    assert [item["number"] for item in result.items] == [1]
    assert captured[:3] == ["gh", "pr", "list"]
    assert not {"merge", "comment", "close", "edit"}.intersection(captured)


def test_alert_collection_stops_after_limit_plus_one() -> None:
    calls = 0

    def runner(command: Sequence[str], timeout: int) -> str:
        nonlocal calls
        calls += 1
        assert "--include" in command
        assert "page" not in parse_qs(urlsplit(command[-1]).query)
        return "HTTP/2.0 200 OK\n\n" + json.dumps([alert(1), alert(2)])

    result = GhCliClient(runner=runner).list_open_dependabot_alerts(
        "example-org/example-app", 1
    )

    assert [item["number"] for item in result.items] == [1]
    assert result.truncated is True
    assert calls == 1


def test_alert_collection_follows_link_cursor_until_limit_plus_one() -> None:
    calls: list[str] = []

    def runner(command: Sequence[str], timeout: int) -> str:
        endpoint = command[-1]
        calls.append(endpoint)
        if len(calls) == 1:
            next_link = (
                "https://api.github.com/repositories/1/dependabot/alerts"
                "?state=open&per_page=100&after=cursor%2Btoken"
            )
            return (
                f'HTTP/2.0 200 OK\nlink: <{next_link}>; rel="next"\n\n'
                + json.dumps([alert(number) for number in range(1, 101)])
            )
        return "HTTP/2.0 200 OK\n\n" + json.dumps([alert(101)])

    result = GhCliClient(runner=runner).list_open_dependabot_alerts(
        "example-org/example-app", 100
    )

    assert len(result.items) == 100
    assert result.truncated is True
    assert len(calls) == 2
    assert "after=cursor%2Btoken" in calls[1]
    assert "per_page=1" in calls[1]


def test_malformed_alert_is_rejected_instead_of_discarded() -> None:
    def runner(command: Sequence[str], timeout: int) -> str:
        return "HTTP/2.0 200 OK\n\n" + json.dumps([alert(1), "malformed"])

    with pytest.raises(GitHubError, match="malformed Dependabot alert item"):
        GhCliClient(runner=runner).list_open_dependabot_alerts(
            "example-org/example-app", 10
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item["dependency"].pop("manifest_path"),
        lambda item: item["dependency"].update({"package": "invalid"}),
        lambda item: item["security_vulnerability"].update(
            {"first_patched_version": "invalid"}
        ),
        lambda item: item.update({"security_advisory": "invalid"}),
    ],
)
def test_malformed_nested_alert_data_is_rejected(
    mutation: Callable[[dict[str, Any]], object],
) -> None:
    item = alert(1)
    mutation(item)

    def runner(command: Sequence[str], timeout: int) -> str:
        return "HTTP/2.0 200 OK\n\n" + json.dumps([item])

    with pytest.raises(GitHubError, match="malformed|manifest"):
        GhCliClient(runner=runner).list_open_dependabot_alerts(
            "example-org/example-app", 10
        )


def test_malformed_open_pr_is_rejected_at_collection_boundary() -> None:
    def runner(command: Sequence[str], timeout: int) -> str:
        return json.dumps([open_pr(1), {"number": 2}])

    with pytest.raises(GitHubError, match="is missing"):
        GhCliClient(runner=runner).list_open_dependabot_prs(
            "example-org/example-app", 10
        )


def test_pull_request_details_do_not_fetch_diff_or_checkout() -> None:
    captured: list[str] = []

    def runner(command: Sequence[str], timeout: int) -> str:
        captured.extend(command)
        if command[1:3] == ["api", "/repos/example-org/example-app/pulls/42/files?per_page=100&page=1"]:
            return json.dumps([{"filename": "pyproject.toml"}])
        return json.dumps(pull_request_details(42))

    result = GhCliClient(runner=runner).fetch_pull_request(
        "example-org/example-app", 42, 1000
    )

    assert captured[:3] == ["gh", "pr", "view"]
    assert "diff" not in captured
    assert "checkout" not in captured
    assert result["files"] == [
        {
            "path": "pyproject.toml",
            "additions": 0,
            "deletions": 0,
            "changes": 0,
            "status": None,
        }
    ]


def test_pull_request_file_collection_rejects_unsafe_path() -> None:
    def runner(command: Sequence[str], timeout: int) -> str:
        if command[1] == "api":
            return json.dumps([{"filename": "/requirements.txt"}])
        return json.dumps(pull_request_details(42))

    with pytest.raises(GitHubError, match="malformed file path"):
        GhCliClient(runner=runner).fetch_pull_request(
            "example-org/example-app", 42, 1000
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update({"number": 99}),
        lambda item: item.update({"headRefName": ""}),
        lambda item: item.update({"headRefOid": None}),
        lambda item: item.update({"updatedAt": None}),
        lambda item: item.update({"isDraft": None}),
        lambda item: item.update({"statusCheckRollup": ["invalid"]}),
        lambda item: item.update({"author": {"login": ""}}),
    ],
)
def test_malformed_pull_request_details_are_rejected(
    mutation: Callable[[dict[str, Any]], object],
) -> None:
    item = pull_request_details(42)
    mutation(item)

    def runner(command: Sequence[str], timeout: int) -> str:
        return json.dumps(item)

    with pytest.raises(GitHubError):
        GhCliClient(runner=runner).fetch_pull_request(
            "example-org/example-app", 42, 1000
        )


def test_pull_request_files_paginate_to_limit_plus_one() -> None:
    calls: list[str] = []

    def runner(command: Sequence[str], timeout: int) -> str:
        endpoint = command[-1]
        calls.append(endpoint)
        if len(calls) == 1:
            return json.dumps([{"filename": f"file-{index}.txt"} for index in range(100)])
        return json.dumps([{"filename": "file-100.txt"}])

    with pytest.raises(GitHubError, match="configured ceiling of 100"):
        GhCliClient(runner=runner)._list_pull_request_files(
            "example-org/example-app", 42, 100
        )

    assert len(calls) == 2
    assert "per_page=100&page=1" in calls[0]
    assert "per_page=1&page=2" in calls[1]


def test_alert_collection_rejects_repeated_cursor() -> None:
    calls = 0

    def runner(command: Sequence[str], timeout: int) -> str:
        nonlocal calls
        calls += 1
        next_link = (
            "https://api.github.com/repositories/1/dependabot/alerts"
            "?state=open&per_page=100&after=repeated"
        )
        return (
            f'HTTP/2.0 200 OK\nlink: <{next_link}>; rel="next"\n\n'
            + json.dumps([alert(calls)])
        )

    with pytest.raises(GitHubError, match="repeated a cursor"):
        GhCliClient(runner=runner).list_open_dependabot_alerts(
            "example-org/example-app", 10
        )

    assert calls == 2


def test_alert_collection_rejects_empty_page_with_next_cursor() -> None:
    def runner(command: Sequence[str], timeout: int) -> str:
        next_link = (
            "https://api.github.com/repositories/1/dependabot/alerts"
            "?state=open&per_page=100&after=next"
        )
        return f'HTTP/2.0 200 OK\nlink: <{next_link}>; rel="next"\n\n[]'

    with pytest.raises(GitHubError, match="returned no items"):
        GhCliClient(runner=runner).list_open_dependabot_alerts(
            "example-org/example-app", 10
        )


def test_alert_collection_enforces_page_ceiling() -> None:
    calls = 0

    def runner(command: Sequence[str], timeout: int) -> str:
        nonlocal calls
        calls += 1
        next_link = (
            "https://api.github.com/repositories/1/dependabot/alerts"
            f"?state=open&per_page=100&after=cursor-{calls}"
        )
        return (
            f'HTTP/2.0 200 OK\nlink: <{next_link}>; rel="next"\n\n'
            + json.dumps([alert(calls)])
        )

    client = GhCliClient(runner=runner)
    client._MAX_ALERT_PAGES = 2
    with pytest.raises(GitHubError, match="page ceiling"):
        client.list_open_dependabot_alerts("example-org/example-app", 10)

    assert calls == 2
