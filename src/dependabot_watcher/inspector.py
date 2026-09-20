from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .config import RepositoryConfig, WatcherConfig
from .github import CollectionResult, GitHubClient, GitHubError
from .policy import (
    assess_pull_request,
    build_path_policy,
    correlate_alerts,
    sensitive_recent_merges,
)


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _collection_error(repository: str, surface: str, exc: GitHubError) -> dict[str, str]:
    return {"repository": repository, "surface": surface, "message": str(exc)}


def _truncation(surface: str, result: CollectionResult[dict[str, Any]]) -> dict[str, Any] | None:
    if not result.truncated:
        return None
    return {
        "surface": surface,
        "limit": result.limit,
        "message": f"{surface} exceeded the configured collection limit.",
    }


def _inspect_repository(
    config: WatcherConfig,
    repository: RepositoryConfig,
    client: GitHubClient,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    truncations: list[dict[str, Any]] = []
    open_prs: tuple[dict[str, Any], ...] = ()
    alerts: tuple[dict[str, Any], ...] = ()
    recent_merged: tuple[dict[str, Any], ...] = ()

    collection_calls = (
        (
            "open_pull_requests",
            lambda: client.list_open_dependabot_prs(
                repository.name,
                config.collection.max_open_prs,
            ),
        ),
        (
            "open_alerts",
            lambda: client.list_open_dependabot_alerts(
                repository.name,
                config.collection.max_open_alerts,
            ),
        ),
        (
            "recent_merged_pull_requests",
            lambda: client.list_recent_merged_prs(
                repository.name,
                lookback_days=config.collection.history_lookback_days,
                limit=config.collection.max_history_items,
                file_limit=config.collection.max_files_per_pull_request,
            ),
        ),
    )
    collected: dict[str, CollectionResult[dict[str, Any]]] = {}
    for surface, call in collection_calls:
        try:
            result = call()
            collected[surface] = result
            truncation = _truncation(surface, result)
            if truncation:
                truncations.append(truncation)
        except GitHubError as exc:
            errors.append(_collection_error(repository.name, surface, exc))

    if "open_pull_requests" in collected:
        open_prs = collected["open_pull_requests"].items
    if "open_alerts" in collected:
        alerts = collected["open_alerts"].items
    if "recent_merged_pull_requests" in collected:
        recent_merged = collected["recent_merged_pull_requests"].items

    path_policy = build_path_policy(config.path_policy, repository.path_policy)
    recent_sensitive = sensitive_recent_merges(recent_merged, path_policy)
    pull_request_inspections: list[dict[str, Any]] = []
    correlation_pull_requests: list[dict[str, Any]] = []
    for pull_request in open_prs:
        number = pull_request.get("number")
        if not isinstance(number, int):
            errors.append(
                {
                    "repository": repository.name,
                    "surface": "pull_request_details",
                    "message": "Open pull-request entry has no integer number.",
                }
            )
            continue
        try:
            details = client.fetch_pull_request(
                repository.name,
                number,
                config.collection.max_files_per_pull_request,
            )
            correlation_pull_requests.append(details)
            pull_request_inspections.append(
                assess_pull_request(
                    repository=repository.name,
                    expected_base=repository.default_branch,
                    details=details,
                    recent_sensitive=recent_sensitive,
                    policy=path_policy,
                    sensitive_history_complete=(
                        "recent_merged_pull_requests" in collected
                        and not collected["recent_merged_pull_requests"].truncated
                    ),
                )
            )
        except GitHubError as exc:
            errors.append(_collection_error(repository.name, f"pull_request_details:{number}", exc))
            pull_request_inspections.append(
                {
                    "candidate_identity": {
                        "repository": repository.name,
                        "number": number,
                        "url": pull_request.get("url"),
                        "title": pull_request.get("title"),
                        "expected_base": repository.default_branch,
                        "observed_base": pull_request.get("baseRefName"),
                        "head_ref": pull_request.get("headRefName"),
                        "head_sha": pull_request.get("headRefOid"),
                    },
                    "classification": "collection_error",
                    "reasons": [str(exc)],
                    "mutation_authorized": False,
                }
            )

    correlated_alerts = correlate_alerts(
        repository.name,
        alerts,
        correlation_pull_requests,
        recent_merged,
    )
    return {
        "repository": repository.name,
        "expected_base": repository.default_branch,
        "pull_requests": pull_request_inspections,
        "alerts": correlated_alerts,
        "recent_sensitive_merges": recent_sensitive,
        "errors": errors,
        "truncations": truncations,
        "summary": {
            "open_pull_request_count": len(open_prs),
            "open_alert_count": len(alerts),
            "attention_required_count": sum(
                item.get("classification") != "baseline_candidate"
                for item in pull_request_inspections
            ),
            "unresolved_alert_count": sum(
                item.get("classification") != "likely_covered_by_open_pr"
                for item in correlated_alerts
            ),
            "error_count": len(errors),
            "truncation_count": len(truncations),
        },
    }


def inspect(
    config: WatcherConfig,
    client: GitHubClient,
    *,
    clock: Clock = _utc_now,
) -> dict[str, Any]:
    """Return a model-free, read-only inspection report without persistent writes."""

    client.validate_auth()
    repositories = [
        _inspect_repository(config, repository, client)
        for repository in config.repositories
    ]
    error_count = sum(item["summary"]["error_count"] for item in repositories)
    truncation_count = sum(item["summary"]["truncation_count"] for item in repositories)
    return {
        "schema_version": 1,
        "mode": "inspect",
        "generated_at": _timestamp(clock()),
        "status": "partial" if error_count or truncation_count else "complete",
        "capabilities": {
            "model_calls": False,
            "persistent_local_writes": False,
            "github_mutations": False,
            "notifications": False,
        },
        "summary": {
            "repository_count": len(repositories),
            "open_pull_request_count": sum(
                item["summary"]["open_pull_request_count"] for item in repositories
            ),
            "open_alert_count": sum(item["summary"]["open_alert_count"] for item in repositories),
            "attention_required_count": sum(
                item["summary"]["attention_required_count"] for item in repositories
            ),
            "unresolved_alert_count": sum(
                item["summary"]["unresolved_alert_count"] for item in repositories
            ),
            "error_count": error_count,
            "truncation_count": truncation_count,
        },
        "repositories": repositories,
    }
