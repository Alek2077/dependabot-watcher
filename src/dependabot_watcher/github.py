from __future__ import annotations

import base64
import json
import posixpath
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Generic, Protocol, Sequence, TypeVar
from urllib.parse import parse_qs, quote, urlencode, urlsplit


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

    def fetch_branch_head(self, repository: str, branch: str) -> str: ...

    def fetch_text_file(
        self,
        repository: str,
        path: str,
        ref: str,
        max_bytes: int,
    ) -> str: ...

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


def repository_relative_path(value: object) -> str | None:
    """Return one canonical repository-relative path or reject unsafe/ambiguous input."""

    if not isinstance(value, str) or not value or value != value.strip():
        return None
    candidate = value.replace("\\", "/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or candidate.startswith(("/", "~"))
        or "$" in candidate
        or any(ord(character) < 32 for character in candidate)
    ):
        return None
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    normalized = posixpath.normpath(candidate)
    if normalized != candidate or normalized in {"", ".", ".."} or normalized.startswith("../"):
        return None
    return normalized


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

    _MAX_ALERT_PAGES: int = 500

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
                "updatedAt",
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
        manifest_path = cls._non_empty_string(
            dependency.get("manifest_path"),
            f"Dependabot alert manifest path at index {index}",
        )
        if repository_relative_path(manifest_path) is None:
            raise GitHubError(
                f"GitHub Dependabot alert item at index {index} has an unsafe manifest path."
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
            "updatedAt",
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

    def fetch_branch_head(self, repository: str, branch: str) -> str:
        value = self._json(
            [
                "api",
                f"/repos/{repository}/commits/{quote(branch, safe='')}",
            ]
        )
        if not isinstance(value, dict):
            raise GitHubError(
                f"GitHub returned unexpected branch-head data for {repository}:{branch}."
            )
        return self._non_empty_string(
            value.get("sha"),
            f"branch-head SHA for {repository}:{branch}",
        )

    def fetch_text_file(
        self,
        repository: str,
        path: str,
        ref: str,
        max_bytes: int,
    ) -> str:
        normalized = repository_relative_path(path)
        if normalized is None:
            raise GitHubError("Repository file path must be a safe repository-relative path.")
        value = self._json(
            [
                "api",
                f"/repos/{repository}/contents/{quote(normalized, safe='/')}?{urlencode({'ref': ref})}",
            ]
        )
        if not isinstance(value, dict) or value.get("type") != "file":
            raise GitHubError(
                f"GitHub returned unexpected file content for {repository}:{normalized}."
            )
        size = value.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise GitHubError(f"GitHub returned malformed file size for {repository}:{normalized}.")
        if size > max_bytes:
            raise GitHubError(
                f"Repository file {normalized} exceeds the configured ceiling of {max_bytes} bytes."
            )
        if value.get("encoding") != "base64" or not isinstance(value.get("content"), str):
            raise GitHubError(
                f"GitHub returned unsupported file encoding for {repository}:{normalized}."
            )
        try:
            encoded = "".join(value["content"].split())
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise GitHubError(
                f"GitHub returned malformed base64 content for {repository}:{normalized}."
            ) from exc
        if len(raw) > max_bytes:
            raise GitHubError(
                f"Repository file {normalized} exceeds the configured ceiling of {max_bytes} bytes."
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitHubError(
                f"Repository file {normalized} is not valid UTF-8 text."
            ) from exc

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
                path = repository_relative_path(item.get("filename"))
                if path is None:
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
                "updatedAt",
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
        seen_cursors: set[str] = set()
        page_count = 0
        while True:
            if page_count >= self._MAX_ALERT_PAGES:
                raise GitHubError(
                    "Dependabot alert pagination exceeded the bounded page ceiling."
                )
            if cursor is not None:
                if cursor in seen_cursors:
                    raise GitHubError("Dependabot alert pagination repeated a cursor.")
                seen_cursors.add(cursor)
            page_count += 1
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
            prior_count = len(items)
            items.extend(validated)
            if len(items) > limit:
                break
            next_cursor = self._next_cursor(headers)
            if not next_cursor:
                break
            if len(items) == prior_count:
                raise GitHubError(
                    "Dependabot alert pagination returned no items but advertised another page."
                )
            cursor = next_cursor
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
                "updatedAt",
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
