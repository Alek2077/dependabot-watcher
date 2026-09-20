from __future__ import annotations

import json
from typing import Any, Callable, Sequence
from urllib.parse import parse_qs, urlsplit

import pytest

from dependabot_watcher.github import GhCliClient, GitHubError


def open_pr(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Bump example-lib to {number}.0.0",
        "url": f"https://github.com/example-org/example-app/pull/{number}",
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


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update({"number": 99}),
        lambda item: item.update({"headRefName": ""}),
        lambda item: item.update({"headRefOid": None}),
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
