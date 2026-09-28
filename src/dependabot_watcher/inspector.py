from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from .config import RepositoryConfig, WatcherConfig
from .github import (
    CollectionResult,
    GitHubClient,
    GitHubError,
    public_github_error_message,
)
from .manifests import collect_manifest_evidence
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
    return {
        "repository": repository,
        "surface": surface,
        "message": public_github_error_message(f"collection for {surface}"),
    }


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
                    "coverage_evidence": relationship.get("coverage_evidence"),
                    "manifest_relationship_path": list(
                        relationship.get("manifest_relationship_path") or []
                    ),
                }
            )
    for pull_request in pull_requests:
        identity = pull_request.get("candidate_identity") or {}
        number = identity.get("number") if isinstance(identity, dict) else None
        pull_request["related_alerts"] = (
            related_by_pull_request.get(number, []) if isinstance(number, int) else []
        )


def _manifest_relationship_path(
    evidence: dict[str, Any],
    source: object,
    target: object,
) -> list[str]:
    source_path = str(source or "").replace("\\", "/").lstrip("/")
    target_path = str(target or "").replace("\\", "/").lstrip("/")
    if not source_path or not target_path:
        return []
    if source_path == target_path:
        return [source_path]
    adjacency: dict[str, set[str]] = {}
    for relationship in evidence.get("relationships") or []:
        if not isinstance(relationship, dict):
            continue
        parent = str(relationship.get("source_manifest") or "")
        child = str(relationship.get("included_manifest") or "")
        if parent and child:
            adjacency.setdefault(parent, set()).add(child)
    queue: list[tuple[str, list[str]]] = [(source_path, [source_path])]
    visited = {source_path}
    while queue:
        current, path = queue.pop(0)
        for child in sorted(adjacency.get(current, set())):
            if child == target_path:
                return [*path, child]
            if child not in visited:
                visited.add(child)
                queue.append((child, [*path, child]))
    return []


def _classify_relationship_evidence(
    alerts: list[dict[str, Any]],
    manifest_evidence: dict[str, Any],
    pull_requests: list[dict[str, Any]],
) -> None:
    pull_requests_by_number: dict[int, dict[str, Any]] = {}
    for pull_request in pull_requests:
        identity = pull_request.get("candidate_identity") or {}
        number = identity.get("number") if isinstance(identity, dict) else None
        if isinstance(number, int):
            pull_requests_by_number[number] = pull_request
    for alert in alerts:
        alert_manifest = alert.get("manifest_path")
        for relationship in alert.get("package_related_open_pull_requests") or []:
            if not isinstance(relationship, dict):
                continue
            meets_patch = relationship.get("meets_first_patched_version") is True
            if relationship.get("manifest_path_matches") and meets_patch:
                relationship["coverage_evidence"] = "direct"
                relationship["manifest_relationship_path"] = [str(alert_manifest or "")]
                continue
            relationship_number = relationship.get("number")
            pull_request = (
                pull_requests_by_number.get(relationship_number)
                if isinstance(relationship_number, int)
                else None
            )
            path_assessment = (
                pull_request.get("path_assessment") if pull_request is not None else {}
            ) or {}
            changed_paths = [
                item.get("path")
                for item in path_assessment.get("files") or []
                if isinstance(item, dict) and item.get("path")
            ]
            inclusion_path: list[str] = []
            for changed_path in changed_paths:
                inclusion_path = _manifest_relationship_path(
                    manifest_evidence,
                    alert_manifest,
                    changed_path,
                )
                if inclusion_path:
                    break
            if inclusion_path and meets_patch:
                relationship["coverage_evidence"] = "likely"
                relationship["manifest_relationship_path"] = inclusion_path
            elif meets_patch:
                relationship["coverage_evidence"] = "indirect"
                relationship["manifest_relationship_path"] = []
            else:
                relationship["coverage_evidence"] = "unverified"
                relationship["manifest_relationship_path"] = inclusion_path


def _inspect_repository(
    config: WatcherConfig,
    repository: RepositoryConfig,
    client: GitHubClient,
    *,
    observed_at: str,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    truncations: list[dict[str, Any]] = []
    open_prs: tuple[dict[str, Any], ...] = ()
    alerts: tuple[dict[str, Any], ...] = ()
    recent_merged: tuple[dict[str, Any], ...] = ()
    base_sha: str | None = None

    try:
        base_sha = client.fetch_branch_head(repository.name, repository.default_branch)
    except GitHubError as exc:
        errors.append(_collection_error(repository.name, "default_branch_head", exc))

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
            assessment = assess_pull_request(
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
            assessment["evidence_observed_at"] = observed_at
            pull_request_inspections.append(assessment)
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
                    "reasons": [public_github_error_message("pull-request collection")],
                    "mutation_authorized": False,
                }
            )

    correlated_alerts = correlate_alerts(
        repository.name,
        alerts,
        correlation_pull_requests,
        recent_merged,
    )
    manifest_seed_paths = [
        str(alert.get("manifest_path") or "")
        for alert in correlated_alerts
        if str(alert.get("ecosystem") or "").casefold() == "pip"
    ]
    manifest_evidence: dict[str, Any]
    if base_sha is None:
        manifest_evidence = {
            "status": "unavailable",
            "base_sha": None,
            "files": [],
            "relationships": [],
            "errors": [
                {
                    "path": None,
                    "message": "Manifest evidence requires a collected default-branch head SHA.",
                }
            ],
            "truncations": [],
        }
    else:
        manifest_evidence = collect_manifest_evidence(
            repository=repository.name,
            base_sha=base_sha,
            seed_paths=manifest_seed_paths,
            client=client,
            max_files=config.collection.max_manifest_files,
            max_file_bytes=config.collection.max_manifest_file_bytes,
            max_depth=config.collection.max_manifest_depth,
        )
    for error in manifest_evidence.get("errors") or []:
        errors.append(
            {
                "repository": repository.name,
                "surface": f"manifest_file:{error.get('path') or 'unknown'}",
                "message": str(error.get("message") or "Manifest evidence collection failed."),
            }
        )
    truncations.extend(manifest_evidence.get("truncations") or [])
    _classify_relationship_evidence(
        correlated_alerts,
        manifest_evidence,
        pull_request_inspections,
    )
    _attach_relationship_evidence(pull_request_inspections, correlated_alerts)
    return {
        "repository": repository.name,
        "expected_base": repository.default_branch,
        "evidence_snapshot": {
            "base_sha": base_sha,
            "observed_at": observed_at,
        },
        "workflow_profile": asdict(repository.workflow),
        "manifest_evidence": manifest_evidence,
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

    try:
        client.validate_auth()
    except GitHubError as exc:
        raise GitHubError(public_github_error_message("authentication")) from exc
    generated_at = _timestamp(clock())
    repositories = [
        _inspect_repository(config, repository, client, observed_at=generated_at)
        for repository in config.repositories
    ]
    error_count = sum(item["summary"]["error_count"] for item in repositories)
    truncation_count = sum(item["summary"]["truncation_count"] for item in repositories)
    return {
        "schema_version": 1,
        "mode": "inspect",
        "generated_at": generated_at,
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
