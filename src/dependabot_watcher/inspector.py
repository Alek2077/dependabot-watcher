from __future__ import annotations

import copy
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


def _pull_request_relationship_evidence(
    pull_request: dict[str, Any],
) -> dict[str, Any]:
    identity = pull_request.get("candidate_identity") or {}
    trusted_identity = identity if isinstance(identity, dict) else {}
    return {
        "number": trusted_identity.get("number"),
        "expected_base": trusted_identity.get("expected_base"),
        "observed_base": trusted_identity.get("observed_base"),
        "head_ref": trusted_identity.get("head_ref"),
        "head_sha": trusted_identity.get("head_sha"),
        "classification": pull_request.get("classification"),
        "reasons": list(pull_request.get("reasons") or []),
        "checks": pull_request.get("checks"),
        "mergeable": pull_request.get("mergeable"),
        "merge_state_status": pull_request.get("merge_state_status"),
        "is_draft": pull_request.get("is_draft"),
        "path_assessment": copy.deepcopy(pull_request.get("path_assessment")),
        "recent_sensitive_overlap": list(pull_request.get("recent_sensitive_overlap") or []),
        "updated_at": pull_request.get("updated_at"),
        "mutation_authorized": False,
    }


def _attach_relationship_evidence(
    pull_requests: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> None:
    pull_requests_by_number: dict[int, dict[str, Any]] = {}
    for pull_request in pull_requests:
        identity = pull_request.get("candidate_identity") or {}
        number = identity.get("number") if isinstance(identity, dict) else None
        if isinstance(number, int):
            pull_requests_by_number[number] = pull_request

    related_by_pull_request: dict[int, list[dict[str, Any]]] = {}
    for alert in alerts:
        for relationship in alert.get("package_related_open_pull_requests") or []:
            number = relationship.get("number")
            if not isinstance(number, int):
                continue
            pull_request = pull_requests_by_number.get(number)
            relationship["pull_request_evidence"] = (
                _pull_request_relationship_evidence(pull_request)
                if pull_request is not None
                else None
            )
            related_by_pull_request.setdefault(number, []).append(
                {
                    "number": alert.get("number"),
                    "dependency_name": alert.get("dependency_name"),
                    "ecosystem": alert.get("ecosystem"),
                    "severity": alert.get("severity"),
                    "manifest_path": alert.get("manifest_path"),
                    "first_patched_version": alert.get("first_patched_version"),
                    "classification": alert.get("classification"),
                    "target_version": relationship.get("target_version"),
                    "manifest_path_matches": relationship.get("manifest_path_matches"),
                    "patched_version_relation": relationship.get("patched_version_relation"),
                    "meets_first_patched_version": relationship.get(
                        "meets_first_patched_version"
                    ),
                }
            )
    for pull_request in pull_requests:
        identity = pull_request.get("candidate_identity") or {}
        number = identity.get("number") if isinstance(identity, dict) else None
        pull_request["related_alerts"] = (
            related_by_pull_request.get(number, []) if isinstance(number, int) else []
        )


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
    _attach_relationship_evidence(pull_request_inspections, correlated_alerts)
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
