from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Generic, Protocol, Sequence, TypeVar
from urllib.parse import parse_qs, urlencode, urlsplit


class GitHubError(RuntimeError):
    """Raised when a read-only GitHub operation cannot be completed."""


T = TypeVar("T")


@dataclass(frozen=True)
class CollectionResult(Generic[T]):
    items: tuple[T, ...]
    truncated: bool = False
    limit: int | None = None


class GitHubClient(Protocol):
    def validate_auth(self) -> None: ...

    def list_open_dependabot_prs(self, repository: str, limit: int) -> CollectionResult[dict[str, Any]]: ...

    def list_open_dependabot_alerts(self, repository: str, limit: int) -> CollectionResult[dict[str, Any]]: ...

    def fetch_pull_request(
        self,
        repository: str,
        number: int,
        file_limit: int,
    ) -> dict[str, Any]: ...

    def list_recent_merged_prs(
        self,
        repository: str,
        *,
        lookback_days: int,
        limit: int,
        file_limit: int,
    ) -> CollectionResult[dict[str, Any]]: ...


CommandRunner = Callable[[Sequence[str], int], str]


def _run_command(command: Sequence[str], timeout_seconds: int) -> str:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise GitHubError("GitHub CLI (`gh`) was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(f"GitHub command timed out after {timeout_seconds} seconds.") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        if len(message) > 2000:
            message = message[:1997] + "..."
        raise GitHubError(message or f"GitHub CLI exited with status {result.returncode}.")
    return result.stdout


class GhCliClient:
    """Read-only GitHub client implemented through an authenticated `gh` CLI."""

    def __init__(self, *, timeout_seconds: int = 120, runner: CommandRunner = _run_command) -> None:
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    def _run(self, args: Sequence[str]) -> str:
        return self._runner(["gh", *args], self._timeout_seconds)

    def _json(self, args: Sequence[str]) -> Any:
        output = self._run(args)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise GitHubError("GitHub CLI returned invalid JSON.") from exc

    def _json_with_headers(self, args: Sequence[str]) -> tuple[Any, dict[str, str]]:
        output = self._run(args).replace("\r\n", "\n")
        if "\n\n" not in output:
            raise GitHubError("GitHub CLI returned a response without HTTP headers.")
        header_block, body = output.split("\n\n", 1)
        headers: dict[str, str] = {}
        for line in header_block.splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().casefold()] = value.strip()
        try:
            return (json.loads(body), headers)
        except json.JSONDecodeError as exc:
            raise GitHubError("GitHub CLI returned invalid JSON.") from exc

    @staticmethod
    def _next_cursor(headers: dict[str, str]) -> str | None:
        link = headers.get("link", "")
        for target, relation in re.findall(r'<([^>]+)>;\s*rel="([^"]+)"', link):
            if relation != "next":
                continue
            values = parse_qs(urlsplit(target).query).get("after")
            if values and values[0]:
                return values[0]
        return None

    @staticmethod
    def _bounded(items: list[dict[str, Any]], limit: int) -> CollectionResult[dict[str, Any]]:
        truncated = len(items) > limit
        return CollectionResult(items=tuple(items[:limit]), truncated=truncated, limit=limit)

    @staticmethod
    def _validated_items(
        value: object,
        *,
        surface: str,
        required_fields: tuple[str, ...],
        require_files: bool = False,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise GitHubError(f"GitHub returned an unexpected {surface} collection.")
        validated: list[dict[str, Any]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise GitHubError(f"GitHub returned a malformed {surface} item at index {index}.")
            missing = [field for field in required_fields if field not in item]
            if missing:
                raise GitHubError(
                    f"GitHub {surface} item at index {index} is missing: {', '.join(missing)}."
                )
            if "number" in required_fields and (
                isinstance(item.get("number"), bool) or not isinstance(item.get("number"), int)
            ):
                raise GitHubError(f"GitHub {surface} item at index {index} has a malformed number.")
            for text_field in (
                "title",
                "url",
                "headRefName",
                "headRefOid",
                "baseRefName",
                "mergedAt",
            ):
                if text_field in required_fields and not isinstance(item.get(text_field), str):
                    raise GitHubError(
                        f"GitHub {surface} item at index {index} has a malformed {text_field}."
                    )
                if text_field in required_fields and not str(item.get(text_field) or "").strip():
                    raise GitHubError(
                        f"GitHub {surface} item at index {index} has an empty {text_field}."
                    )
            if "author" in required_fields and not isinstance(item.get("author"), dict):
                raise GitHubError(f"GitHub {surface} item at index {index} has a malformed author.")
            if require_files:
                files = item.get("files")
                if not isinstance(files, list) or any(
                    not isinstance(file_item, dict) or not isinstance(file_item.get("path"), str)
                    for file_item in files
                ):
                    raise GitHubError(f"GitHub {surface} item at index {index} has malformed files.")
            validated.append(item)
        return validated

    @staticmethod
    def _non_empty_string(value: object, location: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise GitHubError(f"GitHub returned a malformed {location}.")
        return value

    @classmethod
    def _validate_alert(cls, item: dict[str, Any], index: int) -> None:
        dependency = item.get("dependency")
        vulnerability = item.get("security_vulnerability")
        if not isinstance(dependency, dict) or not isinstance(vulnerability, dict):
            raise GitHubError(
                f"GitHub Dependabot alert item at index {index} has malformed vulnerability data."
            )
        package = dependency.get("package")
        if not isinstance(package, dict):
            raise GitHubError(
                f"GitHub Dependabot alert item at index {index} has a malformed package."
            )
        cls._non_empty_string(package.get("name"), f"Dependabot alert package name at index {index}")
        cls._non_empty_string(
            package.get("ecosystem"),
            f"Dependabot alert package ecosystem at index {index}",
        )
        cls._non_empty_string(
            dependency.get("manifest_path"),
            f"Dependabot alert manifest path at index {index}",
        )
        first_patched = vulnerability.get("first_patched_version")
        if first_patched is not None:
            if not isinstance(first_patched, dict):
                raise GitHubError(
                    f"GitHub Dependabot alert item at index {index} has a malformed patched version."
                )
            cls._non_empty_string(
                first_patched.get("identifier"),
                f"Dependabot alert patched version identifier at index {index}",
            )
        advisory = item.get("security_advisory")
        if advisory is not None and not isinstance(advisory, dict):
            raise GitHubError(
                f"GitHub Dependabot alert item at index {index} has a malformed security advisory."
            )

    @classmethod
    def _validate_pull_request_details(
        cls,
        item: dict[str, Any],
        *,
        requested_number: int,
    ) -> None:
        if item.get("number") != requested_number:
            raise GitHubError(
                f"GitHub returned PR #{item.get('number')} when PR #{requested_number} was requested."
            )
        for field in (
            "title",
            "url",
            "headRefName",
            "headRefOid",
            "baseRefName",
            "mergeable",
            "mergeStateStatus",
        ):
            cls._non_empty_string(item.get(field), f"pull-request #{requested_number} {field}")
        if not isinstance(item.get("isDraft"), bool):
            raise GitHubError(f"GitHub pull-request #{requested_number} has malformed draft state.")
        author = item.get("author")
        if not isinstance(author, dict):
            raise GitHubError(f"GitHub pull-request #{requested_number} has a malformed author.")
        cls._non_empty_string(author.get("login"), f"pull-request #{requested_number} author login")
        checks = item.get("statusCheckRollup")
        if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
            raise GitHubError(f"GitHub pull-request #{requested_number} has malformed status checks.")

    def validate_auth(self) -> None:
        self._run(["auth", "status", "--active"])

    def _list_pull_request_files(
        self,
        repository: str,
        number: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page_number = 1
        while True:
            remaining = limit + 1 - len(files)
            per_page = min(100, remaining)
            page = self._json(
                [
                    "api",
                    (
                        f"/repos/{repository}/pulls/{number}/files"
                        f"?per_page={per_page}&page={page_number}"
                    ),
                ]
            )
            if not isinstance(page, list):
                raise GitHubError(f"GitHub returned an unexpected file list for PR #{number}.")
            normalized: list[dict[str, Any]] = []
            for index, item in enumerate(page):
                if not isinstance(item, dict):
                    raise GitHubError(
                        f"GitHub returned a malformed file item for PR #{number} at index {index}."
                    )
                path = item.get("filename")
                if not isinstance(path, str) or not path.strip():
                    raise GitHubError(
                        f"GitHub returned a malformed file path for PR #{number} at index {index}."
                    )
                normalized.append(
                    {
                        "path": path,
                        "additions": item.get("additions", 0),
                        "deletions": item.get("deletions", 0),
                        "changes": item.get("changes", 0),
                        "status": item.get("status"),
                    }
                )
            files.extend(normalized)
            if len(files) > limit:
                raise GitHubError(
                    f"PR #{number} changed-file collection exceeded the configured ceiling of {limit}."
                )
            if len(page) < per_page:
                break
            page_number += 1
        return files

    def list_open_dependabot_prs(
        self,
        repository: str,
        limit: int,
    ) -> CollectionResult[dict[str, Any]]:
        items = self._json(
            [
                "pr",
                "list",
                "--repo",
                repository,
                "--search",
                "author:app/dependabot",
                "--state",
                "open",
                "--limit",
                str(limit + 1),
                "--json",
                (
                    "number,title,url,updatedAt,headRefName,headRefOid,baseRefName,"
                    "mergeStateStatus,statusCheckRollup,author"
                ),
            ]
        )
        validated = self._validated_items(
            items,
            surface="open pull-request",
            required_fields=(
                "number",
                "title",
                "url",
                "headRefName",
                "headRefOid",
                "baseRefName",
                "author",
            ),
        )
        return self._bounded(validated, limit)

    def list_open_dependabot_alerts(
        self,
        repository: str,
        limit: int,
    ) -> CollectionResult[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            remaining = limit + 1 - len(items)
            query = {"state": "open", "per_page": str(min(100, remaining))}
            if cursor:
                query["after"] = cursor
            page, headers = self._json_with_headers(
                [
                    "api",
                    "--include",
                    f"/repos/{repository}/dependabot/alerts?{urlencode(query)}",
                ]
            )
            validated = self._validated_items(
                page,
                surface="Dependabot alert",
                required_fields=("number", "dependency", "security_vulnerability"),
            )
            for index, item in enumerate(validated):
                self._validate_alert(item, index)
            items.extend(validated)
            if len(items) > limit:
                break
            cursor = self._next_cursor(headers)
            if not cursor:
                break
        return self._bounded(items, limit)

    def fetch_pull_request(
        self,
        repository: str,
        number: int,
        file_limit: int,
    ) -> dict[str, Any]:
        item = self._json(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repository,
                "--json",
                (
                    "number,title,url,updatedAt,headRefName,baseRefName,headRefOid,author,"
                    "mergeable,mergeStateStatus,isDraft,statusCheckRollup"
                ),
            ]
        )
        if not isinstance(item, dict):
            raise GitHubError(f"GitHub returned unexpected details for PR #{number}.")
        validated = self._validated_items(
            [item],
            surface=f"pull-request #{number} detail",
            required_fields=(
                "number",
                "title",
                "url",
                "headRefName",
                "headRefOid",
                "baseRefName",
                "author",
                "mergeable",
                "mergeStateStatus",
                "isDraft",
                "statusCheckRollup",
            ),
        )
        self._validate_pull_request_details(validated[0], requested_number=number)
        validated[0]["files"] = self._list_pull_request_files(repository, number, file_limit)
        return validated[0]

    def list_recent_merged_prs(
        self,
        repository: str,
        *,
        lookback_days: int,
        limit: int,
        file_limit: int,
    ) -> CollectionResult[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
        items = self._json(
            [
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "merged",
                "--limit",
                str(limit + 1),
                "--search",
                f"merged:>={since}",
                "--json",
                "number,title,url,mergedAt,author,baseRefName,headRefName",
            ]
        )
        validated = self._validated_items(
            items,
            surface="merged pull-request",
            required_fields=(
                "number",
                "title",
                "url",
                "mergedAt",
                "author",
                "headRefName",
                "baseRefName",
            ),
        )
        bounded = self._bounded(validated, limit)
        with_files: list[dict[str, Any]] = []
        for item in bounded.items:
            enriched = dict(item)
            enriched["files"] = self._list_pull_request_files(
                repository,
                int(item["number"]),
                file_limit,
            )
            with_files.append(enriched)
        return CollectionResult(
            items=tuple(with_files),
            truncated=bounded.truncated,
            limit=bounded.limit,
        )
