from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .advisory import AdvisoryProvider, advise
from .config import WatcherConfig
from .github import GitHubClient, repository_relative_path
from .policy import (
    DEPENDABOT_AUTHORS,
    DEPENDENCY_CATEGORIES,
    SENSITIVE_CATEGORIES,
    compare_patched_versions,
)


PLAN_SCHEMA_VERSION = 1
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "moderate": 2, "low": 3}
_COVERAGE_RANK = {"direct": 0, "likely": 1, "indirect": 2, "unverified": 3}
_PATCH_RELATIONS = {
    "exact",
    "equivalent_release",
    "newer_release",
    "older_release",
    "unverified",
}
_POSITIVE_PATCH_RELATIONS = {"exact", "equivalent_release", "newer_release"}
_RECIPROCAL_RELATIONSHIP_FIELDS = (
    "number",
    "dependency_name",
    "ecosystem",
    "severity",
    "manifest_path",
    "first_patched_version",
    "classification",
    "target_version",
    "manifest_path_matches",
    "patched_version_relation",
    "meets_first_patched_version",
    "coverage_evidence",
    "manifest_relationship_path",
)
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PlanningError(RuntimeError):
    """Raised when trusted planning inputs do not satisfy the plan contract."""


def _stable_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _identity_snapshot(value: object) -> object:
    """Remove observation-only timestamps while retaining material freshness state."""

    if isinstance(value, Mapping):
        return {
            str(key): _identity_snapshot(item)
            for key, item in value.items()
            if key not in {"generated_at", "observed_at", "evidence_observed_at"}
        }
    if isinstance(value, (list, tuple)):
        return [_identity_snapshot(item) for item in value]
    return value


def _required_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningError(f"Planning input is missing {location}.")
    return value


def _step_identifier(
    repository: str,
    repository_plan_core: Mapping[str, Any],
    step_core: Mapping[str, Any],
) -> str:
    evidence = repository_plan_core.get("evidence_snapshot")
    alert_snapshot = repository_plan_core.get("alert_snapshot")
    evidence_mapping = evidence if isinstance(evidence, Mapping) else {}
    alert_mapping = alert_snapshot if isinstance(alert_snapshot, Mapping) else {}
    identity = {
        "repository": repository,
        "base_sha": evidence_mapping.get("base_sha"),
        "alert_snapshot_digest": alert_mapping.get("digest"),
        "step": _identity_snapshot(step_core),
    }
    return f"step_{_stable_digest(identity)[:16]}"


def _repository_plan_identifier(repository_plan_core: Mapping[str, Any]) -> str:
    identity = {
        "repository": repository_plan_core.get("repository"),
        "plan": _identity_snapshot(repository_plan_core),
    }
    return f"plan_{_stable_digest(identity)[:16]}"


def _overall_plan_identifier(status: object, repository_plan_ids: list[str]) -> str:
    return f"plan_{_stable_digest({'status': status, 'repository_plan_ids': repository_plan_ids})[:16]}"


def validate_plan_identities(report: Mapping[str, Any]) -> None:
    """Verify that stable plan identities still match the material saved content."""

    repositories = report.get("repositories")
    if not isinstance(repositories, list):
        raise PlanningError("Plan identity validation requires repository records.")
    repository_plan_ids: list[str] = []
    for index, repository_plan in enumerate(repositories):
        if not isinstance(repository_plan, Mapping):
            raise PlanningError(f"Plan repository record {index} is malformed.")
        repository = _required_string(
            repository_plan.get("repository"), f"repository identity at index {index}"
        )
        observed_plan_id = _required_string(
            repository_plan.get("plan_id"), f"repository plan ID for {repository}"
        )
        repository_plan_core = dict(repository_plan)
        repository_plan_core.pop("plan_id", None)
        expected_plan_id = _repository_plan_identifier(repository_plan_core)
        if observed_plan_id != expected_plan_id:
            raise PlanningError(
                f"Repository plan identity does not match its saved content for {repository}."
            )
        evidence = repository_plan_core.get("evidence_snapshot")
        alert_snapshot = repository_plan_core.get("alert_snapshot")
        if not isinstance(evidence, Mapping) or not isinstance(alert_snapshot, Mapping):
            raise PlanningError(
                f"Repository plan identity lacks snapshot evidence for {repository}."
            )
        steps = repository_plan_core.get("recommended_order")
        if not isinstance(steps, list):
            raise PlanningError(f"Repository plan steps are malformed for {repository}.")
        for step in steps:
            if not isinstance(step, Mapping):
                raise PlanningError(f"Repository plan step is malformed for {repository}.")
            observed_step_id = _required_string(
                step.get("step_id"), f"step ID for {repository}"
            )
            step_core = dict(step)
            step_core.pop("step_id", None)
            step_core.pop("order", None)
            step_core["prerequisites"] = []
            step_core["conflicts"] = []
            if observed_step_id != _step_identifier(
                repository, repository_plan_core, step_core
            ):
                raise PlanningError(
                    f"Plan step identity does not match its saved content for {repository}."
                )
        repository_plan_ids.append(observed_plan_id)
    observed_plan_id = _required_string(report.get("plan_id"), "overall plan ID")
    if observed_plan_id != _overall_plan_identifier(
        report.get("status"), repository_plan_ids
    ):
        raise PlanningError("Overall plan identity does not match its saved content.")


def _validate_collection_records(
    repository_report: Mapping[str, Any], repository: str
) -> None:
    for error in repository_report.get("errors") or []:
        if not isinstance(error, Mapping):
            raise PlanningError(f"Planning input has malformed error records for {repository}.")
        if error.get("repository") != repository:
            raise PlanningError(f"Planning input has mismatched error evidence for {repository}.")
        _required_string(error.get("surface"), f"error surface for {repository}")
        _required_string(error.get("message"), f"error message for {repository}")
    for truncation in repository_report.get("truncations") or []:
        if not isinstance(truncation, Mapping):
            raise PlanningError(
                f"Planning input has malformed truncation records for {repository}."
            )
        _required_string(truncation.get("surface"), f"truncation surface for {repository}")
        _required_string(truncation.get("message"), f"truncation message for {repository}")
        limit = truncation.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise PlanningError(f"Planning input has malformed truncation limit for {repository}.")
        path = truncation.get("path")
        if path is not None and repository_relative_path(path) != path:
            raise PlanningError(f"Planning input has unsafe truncation path for {repository}.")


def _validated_path_assessment(
    pull_request: Mapping[str, Any], number: int
) -> set[str]:
    assessment = pull_request.get("path_assessment")
    if not isinstance(assessment, Mapping):
        raise PlanningError(f"Planning input has malformed path evidence for PR #{number}.")
    files = assessment.get("files")
    if not isinstance(files, list):
        raise PlanningError(f"Planning input has malformed changed files for PR #{number}.")
    paths: list[str] = []
    derived_unclassified: list[str] = []
    derived_sensitive: list[str] = []
    file_states: list[tuple[bool, bool, bool]] = []
    for item in files:
        if not isinstance(item, Mapping):
            raise PlanningError(f"Planning input has malformed changed file for PR #{number}.")
        path = item.get("path")
        if not isinstance(path, str) or repository_relative_path(path) != path or path in paths:
            raise PlanningError(f"Planning input has unsafe or repeated changed path for PR #{number}.")
        categories = item.get("categories")
        if not isinstance(categories, list) or any(
            not isinstance(category, str) or not category for category in categories
        ):
            raise PlanningError(f"Planning input has malformed path categories for PR #{number}.")
        category_set = set(categories)
        if len(category_set) != len(categories) or not category_set.issubset(
            DEPENDENCY_CATEGORIES | SENSITIVE_CATEGORIES
        ):
            raise PlanningError(f"Planning input has unknown path categories for PR #{number}.")
        dependency_related = item.get("dependency_related")
        sensitive = item.get("sensitive")
        classified = item.get("classified")
        if not all(
            isinstance(state, bool)
            for state in (dependency_related, sensitive, classified)
        ):
            raise PlanningError(f"Planning input has malformed path flags for PR #{number}.")
        assert isinstance(dependency_related, bool)
        assert isinstance(sensitive, bool)
        assert isinstance(classified, bool)
        if dependency_related != bool(category_set & DEPENDENCY_CATEGORIES):
            raise PlanningError(f"Planning input has contradictory dependency flags for PR #{number}.")
        if sensitive != bool(category_set & SENSITIVE_CATEGORIES):
            raise PlanningError(f"Planning input has contradictory sensitive flags for PR #{number}.")
        if classified != bool(category_set):
            raise PlanningError(f"Planning input has contradictory classification flags for PR #{number}.")
        paths.append(path)
        file_states.append((dependency_related, sensitive, classified))
        if not classified:
            derived_unclassified.append(path)
        if sensitive:
            derived_sensitive.append(path)
    if len(set(paths)) != len(paths):
        raise PlanningError(f"Planning input repeats changed paths for PR #{number}.")
    if not paths or any(not classified for _, _, classified in file_states):
        derived_classification = "unclassified"
    elif any(sensitive for _, sensitive, _ in file_states):
        derived_classification = "sensitive"
    elif all(dependency_related for dependency_related, _, _ in file_states):
        derived_classification = "dependency_only"
    else:
        derived_classification = "unclassified"
    if assessment.get("classification") != derived_classification:
        raise PlanningError(f"Planning input has contradictory path evidence for PR #{number}.")
    if assessment.get("unclassified_paths") != derived_unclassified:
        raise PlanningError(f"Planning input has contradictory unclassified paths for PR #{number}.")
    if assessment.get("sensitive_paths") != derived_sensitive:
        raise PlanningError(f"Planning input has contradictory sensitive paths for PR #{number}.")
    return set(paths)


def _validated_sensitive_overlap(
    pull_request: Mapping[str, Any], number: int
) -> list[str]:
    overlap = pull_request.get("recent_sensitive_overlap")
    if not isinstance(overlap, list) or any(
        repository_relative_path(path) != path for path in overlap
    ):
        raise PlanningError(f"Planning input has malformed sensitive overlap for PR #{number}.")
    if len(set(overlap)) != len(overlap):
        raise PlanningError(f"Planning input repeats sensitive overlap for PR #{number}.")
    return overlap


def _validated_manifest_evidence(
    repository_report: Mapping[str, Any], repository: str, expected_base_sha: str
) -> tuple[set[tuple[str, str]], str]:
    evidence = repository_report.get("manifest_evidence")
    if not isinstance(evidence, Mapping):
        raise PlanningError("Planning input has malformed manifest evidence.")
    status = _required_string(evidence.get("status"), f"manifest status for {repository}")
    if status not in {"complete", "partial", "not_applicable", "unavailable"}:
        raise PlanningError(f"Planning input has unknown manifest status for {repository}.")
    if evidence.get("base_sha") != expected_base_sha:
        raise PlanningError(f"Planning input has mismatched manifest base SHA for {repository}.")
    files = evidence.get("files")
    if not isinstance(files, list):
        raise PlanningError(f"Planning input has malformed manifest files for {repository}.")
    derived_relationships: set[tuple[str, str, str, int]] = set()
    included_paths: set[str] = set()
    seen_files: set[str] = set()
    for file_evidence in files:
        if not isinstance(file_evidence, Mapping):
            raise PlanningError(f"Planning input has malformed manifest file for {repository}.")
        path = file_evidence.get("path")
        depth = file_evidence.get("depth")
        includes = file_evidence.get("includes")
        if (
            not isinstance(path, str)
            or repository_relative_path(path) != path
            or path in seen_files
            or isinstance(depth, bool)
            or not isinstance(depth, int)
            or depth < 0
            or not isinstance(includes, list)
        ):
            raise PlanningError(f"Planning input has malformed manifest file for {repository}.")
        seen_files.add(path)
        seen_includes: set[tuple[str, str]] = set()
        for include in includes:
            if not isinstance(include, Mapping):
                raise PlanningError(f"Planning input has malformed manifest include for {repository}.")
            included = include.get("path")
            relation = include.get("relation")
            if (
                not isinstance(included, str)
                or repository_relative_path(included) != included
                or not isinstance(relation, str)
                or relation not in {"requirement", "constraint"}
                or (included, relation) in seen_includes
            ):
                raise PlanningError(f"Planning input has malformed manifest include for {repository}.")
            seen_includes.add((included, relation))
            included_paths.add(included)
            derived_relationships.add((path, included, relation, depth + 1))
    errors = evidence.get("errors", [])
    truncations = evidence.get("truncations", [])
    if not isinstance(errors, list) or not isinstance(truncations, list):
        raise PlanningError(f"Planning input has malformed manifest diagnostics for {repository}.")
    for error in errors:
        if not isinstance(error, Mapping):
            raise PlanningError(f"Planning input has malformed manifest errors for {repository}.")
        _required_string(error.get("message"), f"manifest error message for {repository}")
        path = error.get("path")
        if path is not None and repository_relative_path(path) != path:
            raise PlanningError(f"Planning input has unsafe manifest error path for {repository}.")
    for truncation in truncations:
        if not isinstance(truncation, Mapping):
            raise PlanningError(f"Planning input has malformed manifest truncations for {repository}.")
        _required_string(
            truncation.get("surface"), f"manifest truncation surface for {repository}"
        )
        _required_string(
            truncation.get("message"), f"manifest truncation message for {repository}"
        )
        limit = truncation.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise PlanningError(f"Planning input has malformed manifest limit for {repository}.")
        path = truncation.get("path")
        if path is not None and repository_relative_path(path) != path:
            raise PlanningError(f"Planning input has unsafe manifest truncation path for {repository}.")
    if status in {"complete", "not_applicable"} and (errors or truncations):
        raise PlanningError(f"Planning input has contradictory manifest status for {repository}.")
    if status in {"partial", "unavailable"} and not (errors or truncations):
        raise PlanningError(f"Planning input has unexplained incomplete manifests for {repository}.")
    relationships = evidence.get("relationships")
    if not isinstance(relationships, list):
        raise PlanningError("Planning input has malformed manifest relationships.")
    edges: set[tuple[str, str]] = set()
    observed_relationships: set[tuple[str, str, str, int]] = set()
    for relationship in relationships:
        if not isinstance(relationship, Mapping):
            raise PlanningError("Planning input has malformed manifest relationship evidence.")
        source = relationship.get("source_manifest")
        included = relationship.get("included_manifest")
        relation = relationship.get("relation")
        depth = relationship.get("depth")
        if (
            not isinstance(source, str)
            or not isinstance(included, str)
            or repository_relative_path(source) != source
            or repository_relative_path(included) != included
            or not isinstance(relation, str)
            or relation not in {"requirement", "constraint"}
            or isinstance(depth, bool)
            or not isinstance(depth, int)
            or depth <= 0
        ):
            raise PlanningError("Planning input has unsafe manifest relationship evidence.")
        relationship_record = (source, included, relation, depth)
        if relationship_record in observed_relationships:
            raise PlanningError(f"Planning input repeats manifest relationships for {repository}.")
        edges.add((source, included))
        observed_relationships.add(relationship_record)
    if observed_relationships != derived_relationships:
        raise PlanningError(f"Planning input has contradictory manifest relationships for {repository}.")
    if status == "complete" and not files:
        raise PlanningError(f"Planning input has empty complete manifest evidence for {repository}.")
    if status == "complete" and not included_paths.issubset(seen_files):
        raise PlanningError(f"Planning input has uncollected included manifests for {repository}.")
    if status == "not_applicable" and (files or relationships):
        raise PlanningError(f"Planning input has contradictory not-applicable manifests for {repository}.")
    return edges, status


def _validated_repository_input(
    repository_report: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    repository = _required_string(repository_report.get("repository"), "repository identity")
    expected_base = _required_string(
        repository_report.get("expected_base"), f"expected base for {repository}"
    )
    snapshot_value = repository_report.get("evidence_snapshot")
    if not isinstance(snapshot_value, dict):
        raise PlanningError(f"Planning input is missing the evidence snapshot for {repository}.")
    snapshot = dict(snapshot_value)
    _required_string(snapshot.get("base_sha"), f"base SHA for {repository}")
    _required_string(snapshot.get("observed_at"), f"observation time for {repository}")
    for field in ("pull_requests", "alerts", "errors", "truncations"):
        if not isinstance(repository_report.get(field), list):
            raise PlanningError(f"Planning input has malformed {field} for {repository}.")
    _validate_collection_records(repository_report, repository)
    workflow = repository_report.get("workflow_profile")
    if workflow is not None and not isinstance(workflow, dict):
        raise PlanningError(f"Planning input has a malformed workflow profile for {repository}.")
    return repository, expected_base, snapshot


def _validate_pull_request_input(
    repository: str,
    expected_base: str,
    pull_request: Mapping[str, Any],
) -> int:
    identity = pull_request.get("candidate_identity")
    if not isinstance(identity, Mapping):
        raise PlanningError(f"Planning input has malformed PR identity for {repository}.")
    if identity.get("repository") != repository:
        raise PlanningError(f"Planning input has mismatched PR repository identity for {repository}.")
    number = identity.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise PlanningError(f"Planning input has malformed PR number for {repository}.")
    identity_expected_base = _required_string(
        identity.get("expected_base"), f"PR #{number} expected base"
    )
    if identity_expected_base != expected_base:
        raise PlanningError(f"Planning input has mismatched expected base for PR #{number}.")
    classification = _required_string(
        pull_request.get("classification"), f"PR #{number} classification"
    )
    if classification not in {"baseline_candidate", "attention_required", "collection_error"}:
        raise PlanningError(f"Planning input has unknown classification for PR #{number}.")
    reasons = pull_request.get("reasons")
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise PlanningError(f"Planning input has malformed blockers for PR #{number}.")
    if pull_request.get("mutation_authorized") is not False:
        raise PlanningError(f"Planning input has invalid mutation authority for PR #{number}.")
    related_alerts = pull_request.get("related_alerts")
    if not isinstance(related_alerts, list) or any(
        not isinstance(item, Mapping) for item in related_alerts
    ):
        raise PlanningError(f"Planning input has malformed alert relationships for PR #{number}.")
    if classification == "collection_error":
        if not reasons:
            raise PlanningError(f"Collection-error PR #{number} has no failure evidence.")
        return number
    status_checks = pull_request.get("status_checks")
    if not isinstance(status_checks, list):
        raise PlanningError(f"Planning input has malformed status-check evidence for PR #{number}.")
    seen_checks: set[str] = set()
    for check in status_checks:
        if not isinstance(check, Mapping):
            raise PlanningError(f"Planning input has malformed status check for PR #{number}.")
        name = _required_string(check.get("name"), f"PR #{number} status-check name")
        if name.casefold() in seen_checks:
            raise PlanningError(f"Planning input repeats status check {name!r} for PR #{number}.")
        seen_checks.add(name.casefold())
        if check.get("state") not in {"passing", "pending", "failing", "unknown", "missing"}:
            raise PlanningError(f"Planning input has unknown status-check state for PR #{number}.")
    observed_base = _required_string(identity.get("observed_base"), f"PR #{number} observed base")
    for field in ("head_ref", "head_sha"):
        _required_string(identity.get(field), f"PR #{number} {field}")
    _required_string(pull_request.get("updated_at"), f"PR #{number} update time")
    _required_string(
        pull_request.get("evidence_observed_at"),
        f"PR #{number} evidence observation time",
    )
    path_assessment = pull_request.get("path_assessment")
    _validated_path_assessment(pull_request, number)
    sensitive_overlap = _validated_sensitive_overlap(pull_request, number)
    if classification == "attention_required":
        if not reasons:
            raise PlanningError(f"Attention-required PR #{number} has no blocker evidence.")
        return number
    baseline_consistent = (
        observed_base == expected_base
        and str(pull_request.get("author") or "").casefold() in DEPENDABOT_AUTHORS
        and pull_request.get("is_draft") is False
        and str(pull_request.get("mergeable") or "").upper() == "MERGEABLE"
        and str(pull_request.get("merge_state_status") or "").upper() == "CLEAN"
        and pull_request.get("checks") == "passing"
        and isinstance(path_assessment, Mapping)
        and path_assessment.get("classification") == "dependency_only"
        and not sensitive_overlap
        and not reasons
    )
    if not baseline_consistent:
        raise PlanningError(f"Baseline-candidate PR #{number} contradicts its evidence.")
    return number


def _validate_alert_input(
    repository: str,
    alert: Mapping[str, Any],
    pull_requests_by_number: Mapping[int, Mapping[str, Any]],
    changed_paths_by_pull_request: Mapping[int, set[str]],
    manifest_edges: set[tuple[str, str]],
) -> int:
    number = alert.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise PlanningError(f"Planning input has malformed alert identity for {repository}.")
    for field in ("dependency_name", "ecosystem", "severity", "classification"):
        _required_string(alert.get(field), f"alert #{number} {field}")
    manifest_path = repository_relative_path(alert.get("manifest_path"))
    if manifest_path is None:
        raise PlanningError(f"Planning input has unsafe manifest path for alert #{number}.")
    classification = str(alert.get("classification"))
    if classification not in {
        "likely_covered_by_open_pr",
        "related_open_pr_unverified",
        "possibly_covered_by_recent_merge",
        "unresolved_without_pr",
    }:
        raise PlanningError(f"Planning input has unknown classification for alert #{number}.")
    relationships = alert.get("package_related_open_pull_requests")
    if not isinstance(relationships, list):
        raise PlanningError(f"Planning input has malformed relationships for alert #{number}.")
    direct_relationship = False
    related_pull_requests: set[int] = set()
    for relationship in relationships:
        if not isinstance(relationship, Mapping):
            raise PlanningError(f"Planning input has malformed relationship for alert #{number}.")
        related_number = relationship.get("number")
        if (
            isinstance(related_number, bool)
            or not isinstance(related_number, int)
            or related_number not in pull_requests_by_number
        ):
            raise PlanningError(f"Alert #{number} references an unknown pull request.")
        if related_number in related_pull_requests:
            raise PlanningError(f"Alert #{number} repeats a pull-request relationship.")
        related_pull_requests.add(related_number)
        coverage = relationship.get("coverage_evidence")
        if not isinstance(coverage, str) or coverage not in _COVERAGE_RANK:
            raise PlanningError(f"Alert #{number} has unknown coverage evidence.")
        meets_patch_value = relationship.get("meets_first_patched_version")
        path_matches_value = relationship.get("manifest_path_matches")
        if not isinstance(meets_patch_value, bool) or not isinstance(path_matches_value, bool):
            raise PlanningError(f"Alert #{number} has malformed relationship flags.")
        patch_relation = relationship.get("patched_version_relation")
        if not isinstance(patch_relation, str) or patch_relation not in _PATCH_RELATIONS:
            raise PlanningError(f"Alert #{number} has unknown patched-version evidence.")
        target_version = _required_string(
            relationship.get("target_version"), f"alert #{number} target version"
        )
        computed_patch_relation = compare_patched_versions(
            target_version, alert.get("first_patched_version")
        )
        if patch_relation != computed_patch_relation:
            raise PlanningError(f"Alert #{number} has fabricated patched-version evidence.")
        meets_patch = meets_patch_value
        path_matches = path_matches_value
        if meets_patch != (patch_relation in _POSITIVE_PATCH_RELATIONS):
            raise PlanningError(f"Alert #{number} has contradictory patched-version evidence.")
        if coverage != "unverified":
            _required_string(alert.get("first_patched_version"), f"alert #{number} patched version")
        relationship_path = relationship.get("manifest_relationship_path") or []
        if not isinstance(relationship_path, list) or any(
            repository_relative_path(item) != item for item in relationship_path
        ) or len(set(relationship_path)) != len(relationship_path):
            raise PlanningError(f"Alert #{number} has malformed manifest relationship evidence.")
        changed_paths = changed_paths_by_pull_request.get(related_number, set())
        if path_matches != (manifest_path in changed_paths):
            raise PlanningError(f"Alert #{number} has contradictory manifest-match evidence.")
        if relationship_path:
            if relationship_path[0] != manifest_path or relationship_path[-1] not in changed_paths:
                raise PlanningError(f"Alert #{number} has unanchored manifest relationship evidence.")
            if any(
                edge not in manifest_edges
                for edge in zip(relationship_path, relationship_path[1:])
            ):
                raise PlanningError(f"Alert #{number} has unsupported manifest graph evidence.")
        if coverage == "direct":
            if not meets_patch or not path_matches or relationship_path != [manifest_path]:
                raise PlanningError(f"Alert #{number} has inconsistent direct coverage evidence.")
            direct_relationship = True
        elif coverage == "likely":
            if not meets_patch or path_matches or len(relationship_path) < 2:
                raise PlanningError(f"Alert #{number} has inconsistent likely coverage evidence.")
        elif coverage == "indirect":
            if not meets_patch or path_matches or relationship_path:
                raise PlanningError(f"Alert #{number} has inconsistent indirect coverage evidence.")
        elif meets_patch:
            raise PlanningError(f"Alert #{number} has inconsistent unverified coverage evidence.")
    if (classification == "likely_covered_by_open_pr") != direct_relationship:
        raise PlanningError(f"Alert #{number} classification contradicts its relationship evidence.")
    return number


def _validate_reciprocal_relationships(
    pull_requests_by_number: Mapping[int, Mapping[str, Any]],
    alerts: list[Mapping[str, Any]],
) -> None:
    expected: dict[tuple[int, int], dict[str, Any]] = {}
    for alert in alerts:
        alert_number = alert["number"]
        for relationship in alert.get("package_related_open_pull_requests") or []:
            pull_request_number = relationship["number"]
            expected[(pull_request_number, alert_number)] = {
                "number": alert_number,
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
                "manifest_relationship_path": relationship.get(
                    "manifest_relationship_path"
                ),
            }
    observed: set[tuple[int, int]] = set()
    for pull_request_number, pull_request in pull_requests_by_number.items():
        for relationship in pull_request.get("related_alerts") or []:
            alert_number = relationship.get("number")
            if isinstance(alert_number, bool) or not isinstance(alert_number, int):
                raise PlanningError(
                    f"PR #{pull_request_number} has malformed reciprocal alert evidence."
                )
            key = (pull_request_number, alert_number)
            if key in observed or key not in expected:
                raise PlanningError(
                    f"PR #{pull_request_number} has an unknown or repeated alert relationship."
                )
            observed.add(key)
            for field in _RECIPROCAL_RELATIONSHIP_FIELDS:
                if field not in relationship or type(relationship[field]) is not type(
                    expected[key][field]
                ):
                    raise PlanningError(
                        f"PR #{pull_request_number} has malformed reciprocal alert evidence."
                    )
            relationship_path = relationship.get("manifest_relationship_path")
            if not isinstance(relationship_path, list) or any(
                not isinstance(path, str) for path in relationship_path
            ):
                raise PlanningError(
                    f"PR #{pull_request_number} has malformed reciprocal alert evidence."
                )
            if any(
                relationship.get(field) != expected[key].get(field)
                for field in _RECIPROCAL_RELATIONSHIP_FIELDS
            ):
                raise PlanningError(
                    f"PR #{pull_request_number} contradicts reciprocal alert evidence."
                )
    if observed != set(expected):
        raise PlanningError("Planning input lacks reciprocal PR relationship evidence.")


def _advice_by_identity(report: Mapping[str, Any]) -> dict[tuple[str, str, int], dict[str, Any]]:
    decisions: dict[tuple[str, str, int], dict[str, Any]] = {}
    advisory = report.get("advisory")
    if not isinstance(advisory, Mapping):
        return decisions
    for decision in advisory.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        identity = decision.get("identity")
        if not isinstance(identity, dict):
            continue
        repository = identity.get("repository")
        number = identity.get("number")
        kind = decision.get("kind")
        if isinstance(repository, str) and isinstance(number, int) and isinstance(kind, str):
            decisions[(kind, repository, number)] = decision
    return decisions


def _alert_snapshot(repository_report: Mapping[str, Any]) -> dict[str, Any]:
    alerts = [
        dict(alert)
        for alert in repository_report.get("alerts") or []
        if isinstance(alert, dict)
    ]
    alerts.sort(key=lambda item: (str(item.get("number")), str(item.get("dependency_name"))))
    return {"count": len(alerts), "digest": _stable_digest(_identity_snapshot(alerts))}


def _effective_advice(
    decisions: Mapping[tuple[str, str, int], dict[str, Any]],
    *,
    kind: str,
    repository: str,
    number: object,
) -> dict[str, Any] | None:
    if not isinstance(number, int):
        return None
    decision = decisions.get((kind, repository, number))
    if decision is None:
        return None
    effective = decision.get("effective_advice")
    return dict(effective) if isinstance(effective, dict) else None


def _conservative_action(action: str, advice: Mapping[str, Any] | None) -> str:
    if advice is None:
        return action
    recommended = advice.get("recommended_action")
    if recommended == "hold":
        return "hold"
    if recommended == "human_attention":
        return "human_attention"
    if recommended in {"investigate", "review_required"}:
        return "investigate"
    if recommended == "no_action" and action != "investigate":
        return "no_action"
    return action


def _relationship_summary(pull_request: Mapping[str, Any]) -> list[dict[str, Any]]:
    relationships: list[dict[str, Any]] = []
    for alert in pull_request.get("related_alerts") or []:
        if not isinstance(alert, dict):
            continue
        relationships.append(
            {
                "alert_number": alert.get("number"),
                "dependency_name": alert.get("dependency_name"),
                "severity": alert.get("severity"),
                "manifest_path": alert.get("manifest_path"),
                "coverage_evidence": alert.get("coverage_evidence") or "unverified",
                "manifest_relationship_path": list(
                    alert.get("manifest_relationship_path") or []
                ),
                "meets_first_patched_version": alert.get("meets_first_patched_version"),
            }
        )
    relationships.sort(
        key=lambda item: (
            _SEVERITY_RANK.get(str(item.get("severity") or "").casefold(), 4),
            _COVERAGE_RANK.get(str(item.get("coverage_evidence") or ""), 4),
            int(item.get("alert_number") or 0),
        )
    )
    return relationships


def _validation_gates(
    workflow: Mapping[str, Any],
    *,
    has_head: bool,
) -> list[dict[str, Any]]:
    gates = [
        {
            "type": "reinspect_repository",
            "description": "Reinspect repository state after this step before continuing.",
        },
        {
            "type": "alert_snapshot",
            "description": "Confirm the Dependabot alert snapshot still matches the plan.",
        },
    ]
    if has_head:
        gates.insert(
            0,
            {
                "type": "exact_pr_head",
                "description": "Confirm the pull request still has the planned head SHA.",
            },
        )
        gates.insert(
            1,
            {
                "type": "status_checks",
                "description": "Confirm required checks are complete and passing.",
            },
        )
    for check in workflow.get("required_checks") or []:
        gates.append({"type": "configured_check", "name": check})
    for environment in workflow.get("deployment_environments") or []:
        gates.append({"type": "deployment_environment", "name": environment})
    if workflow.get("promotion_branch"):
        gates.append(
            {"type": "promotion_branch", "name": workflow["promotion_branch"]}
        )
    for note in workflow.get("validation_notes") or []:
        gates.append({"type": "configured_validation", "description": note})
    return gates


def _pr_step(
    repository: str,
    pull_request: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    alert_snapshot: Mapping[str, Any],
    workflow: Mapping[str, Any],
    advice: Mapping[str, Any] | None,
    evidence_complete: bool,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    identity = dict(pull_request.get("candidate_identity") or {})
    relationships = _relationship_summary(pull_request)
    classification = pull_request.get("classification")
    base_action = (
        "validate_then_merge"
        if classification == "baseline_candidate" and evidence_complete
        else "investigate"
    )
    action = _conservative_action(base_action, advice)
    expected_alert_impact = [
        item
        for item in relationships
        if _COVERAGE_RANK.get(str(item.get("coverage_evidence") or ""), 4)
        <= _COVERAGE_RANK["likely"]
    ]
    ranked_impact = [
        (
            _SEVERITY_RANK.get(str(item.get("severity") or "").casefold(), 4),
            _COVERAGE_RANK.get(str(item.get("coverage_evidence") or ""), 4),
        )
        for item in expected_alert_impact
    ]
    severity_rank, coverage_rank = min(ranked_impact, default=(4, 4))
    high_security = bool(expected_alert_impact) and severity_rank <= 1
    if high_security:
        group = 0
        priority_reason = "Addresses a critical/high alert with direct or likely coverage evidence."
    elif expected_alert_impact:
        group = 2
        priority_reason = "Addresses an alert with direct or likely coverage evidence."
    elif classification == "baseline_candidate":
        group = 3
        priority_reason = "Deterministic PR checks currently make this a baseline candidate."
    else:
        group = 6
        priority_reason = "Deterministic blockers require investigation before any action."
    number = identity.get("number")
    head_sha = identity.get("head_sha")
    blockers = list(pull_request.get("reasons") or [])
    if not evidence_complete:
        blockers.append("Repository inspection evidence is incomplete.")
    step_core = {
        "kind": "pull_request",
        "action": action,
        "identity": identity,
        "summary": f"{action.replace('_', ' ').capitalize()} PR #{number}.",
        "priority_reason": priority_reason,
        "deterministic_classification": classification,
        "blockers": blockers,
        "expected_alert_impact": expected_alert_impact,
        "related_alerts": relationships,
        "advice": dict(advice) if advice is not None else None,
        "prerequisites": [],
        "conflicts": [],
        "changed_paths": [
            item.get("path")
            for item in (pull_request.get("path_assessment") or {}).get("files") or []
            if isinstance(item, dict) and item.get("path")
        ],
        "status_checks": [dict(item) for item in pull_request.get("status_checks") or []],
        "validation_gates": _validation_gates(workflow, has_head=bool(head_sha)),
        "freshness": {
            "base_sha": snapshot.get("base_sha"),
            "head_sha": head_sha,
            "pr_updated_at": pull_request.get("updated_at"),
            "evidence_observed_at": pull_request.get("evidence_observed_at")
            or snapshot.get("observed_at"),
            "alert_snapshot_digest": alert_snapshot.get("digest"),
            "reinspection_required": True,
        },
        "mutation_authorized": False,
    }
    step = {
        "step_id": _step_identifier(
            repository,
            {
                "evidence_snapshot": snapshot,
                "alert_snapshot": alert_snapshot,
            },
            step_core,
        ),
        **step_core,
    }
    return ((group, severity_rank, coverage_rank, int(number or 0)), step)


def _alert_step(
    repository: str,
    alert: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    alert_snapshot: Mapping[str, Any],
    workflow: Mapping[str, Any],
    advice: Mapping[str, Any] | None,
) -> tuple[tuple[Any, ...], dict[str, Any]] | None:
    relationships = [
        item
        for item in alert.get("package_related_open_pull_requests") or []
        if isinstance(item, dict)
    ]
    best_coverage = min(
        (
            _COVERAGE_RANK.get(str(item.get("coverage_evidence") or "unverified"), 3)
            for item in relationships
        ),
        default=4,
    )
    if best_coverage <= _COVERAGE_RANK["likely"]:
        return None
    action = _conservative_action("investigate", advice)
    severity = str(alert.get("severity") or "unknown").casefold()
    severity_rank = _SEVERITY_RANK.get(severity, 4)
    group = 1 if severity_rank <= 1 else 4
    number = alert.get("number")
    identity = {
        "repository": repository,
        "number": number,
        "url": alert.get("url") or alert.get("html_url"),
        "dependency_name": alert.get("dependency_name"),
        "ecosystem": alert.get("ecosystem"),
        "manifest_path": alert.get("manifest_path"),
        "severity": alert.get("severity"),
    }
    step_core = {
        "kind": "alert",
        "action": action,
        "identity": identity,
        "summary": (
            f"{action.replace('_', ' ').capitalize()} alert #{number} for "
            f"{alert.get('dependency_name') or 'dependency'}."
        ),
        "priority_reason": (
            "Critical/high alert lacks direct or likely remediation evidence."
            if severity_rank <= 1
            else "Alert lacks direct or likely remediation evidence."
        ),
        "deterministic_classification": alert.get("classification"),
        "blockers": [
            "No direct or likely open-PR coverage is proven by collected evidence."
        ],
        "expected_alert_impact": [],
        "related_pull_requests": [
            {
                "number": item.get("number"),
                "coverage_evidence": item.get("coverage_evidence") or "unverified",
                "manifest_relationship_path": list(
                    item.get("manifest_relationship_path") or []
                ),
            }
            for item in relationships
        ],
        "advice": dict(advice) if advice is not None else None,
        "prerequisites": [],
        "conflicts": [],
        "validation_gates": _validation_gates(workflow, has_head=False),
        "freshness": {
            "base_sha": snapshot.get("base_sha"),
            "head_sha": None,
            "evidence_observed_at": snapshot.get("observed_at"),
            "alert_snapshot_digest": alert_snapshot.get("digest"),
            "reinspection_required": True,
        },
        "mutation_authorized": False,
    }
    step = {
        "step_id": _step_identifier(
            repository,
            {
                "evidence_snapshot": snapshot,
                "alert_snapshot": alert_snapshot,
            },
            step_core,
        ),
        **step_core,
    }
    return ((group, severity_rank, best_coverage, int(number or 0)), step)


def _attach_conflicts(steps: list[dict[str, Any]]) -> None:
    pull_request_steps = [step for step in steps if step.get("kind") == "pull_request"]
    for index, left in enumerate(pull_request_steps):
        left_paths = set(left.get("changed_paths") or [])
        left_alerts: set[int] = set()
        for item in left.get("expected_alert_impact") or []:
            alert_number = item.get("alert_number") if isinstance(item, dict) else None
            if isinstance(alert_number, int):
                left_alerts.add(alert_number)
        for right in pull_request_steps[index + 1 :]:
            right_paths = set(right.get("changed_paths") or [])
            right_alerts: set[int] = set()
            for item in right.get("expected_alert_impact") or []:
                alert_number = item.get("alert_number") if isinstance(item, dict) else None
                if isinstance(alert_number, int):
                    right_alerts.add(alert_number)
            shared_paths = sorted(left_paths.intersection(right_paths))
            shared_alerts = sorted(left_alerts.intersection(right_alerts))
            if not shared_paths and not shared_alerts:
                continue
            for current, other in ((left, right), (right, left)):
                current["conflicts"].append(
                    {
                        "step_id": other["step_id"],
                        "type": "overlapping_dependency_change",
                        "shared_path_count": len(shared_paths),
                        "shared_paths": shared_paths[:20],
                        "shared_alerts": shared_alerts,
                        "effect": "Reinspect and refresh remaining pull requests after either step.",
                    }
                )


def plan_from_advisory(advisory_report: dict[str, Any]) -> dict[str, Any]:
    inspection = advisory_report.get("inspection")
    if not isinstance(inspection, dict):
        raise PlanningError("Planning requires an advisory report with inspection evidence.")
    decisions = _advice_by_identity(advisory_report)
    repository_plans: list[dict[str, Any]] = []
    total_steps = 0
    repositories = inspection.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise PlanningError("Planning input has a malformed repository collection.")
    seen_repositories: set[str] = set()
    for repository_report in repositories:
        if not isinstance(repository_report, dict):
            raise PlanningError("Planning input contains a malformed repository record.")
        repository, expected_base, snapshot = _validated_repository_input(repository_report)
        repository_key = repository.casefold()
        if repository_key in seen_repositories:
            raise PlanningError(f"Planning input repeats repository {repository}.")
        seen_repositories.add(repository_key)
        workflow = repository_report.get("workflow_profile") or {}
        alert_snapshot = _alert_snapshot(repository_report)
        manifest_edges, manifest_status = _validated_manifest_evidence(
            repository_report,
            repository,
            str(snapshot["base_sha"]),
        )
        evidence_complete = not repository_report.get("errors") and not repository_report.get(
            "truncations"
        ) and manifest_status in {"complete", "not_applicable"}
        ranked_steps: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        pull_requests_by_number: dict[int, Mapping[str, Any]] = {}
        for pull_request in repository_report.get("pull_requests") or []:
            if not isinstance(pull_request, dict):
                raise PlanningError(f"Planning input contains a malformed PR for {repository}.")
            pull_request_number = _validate_pull_request_input(
                repository, expected_base, pull_request
            )
            if pull_request_number in pull_requests_by_number:
                raise PlanningError(
                    f"Planning input repeats PR #{pull_request_number} for {repository}."
                )
            pull_requests_by_number[pull_request_number] = pull_request
        changed_paths_by_pull_request = {
            number: (
                set()
                if pull_request.get("classification") == "collection_error"
                else _validated_path_assessment(pull_request, number)
            )
            for number, pull_request in pull_requests_by_number.items()
        }
        alert_numbers: set[int] = set()
        validated_alerts: list[Mapping[str, Any]] = []
        for alert in repository_report.get("alerts") or []:
            if not isinstance(alert, dict):
                raise PlanningError(f"Planning input contains a malformed alert for {repository}.")
            alert_number = _validate_alert_input(
                repository,
                alert,
                pull_requests_by_number,
                changed_paths_by_pull_request,
                manifest_edges,
            )
            if alert_number in alert_numbers:
                raise PlanningError(
                    f"Planning input repeats alert #{alert_number} for {repository}."
                )
            alert_numbers.add(alert_number)
            validated_alerts.append(alert)
        _validate_reciprocal_relationships(pull_requests_by_number, validated_alerts)
        for pull_request in pull_requests_by_number.values():
            identity = pull_request.get("candidate_identity") or {}
            ranked_steps.append(
                _pr_step(
                    repository,
                    pull_request,
                    snapshot=snapshot,
                    alert_snapshot=alert_snapshot,
                    workflow=workflow,
                    advice=_effective_advice(
                        decisions,
                        kind="pull_request",
                        repository=repository,
                        number=identity.get("number"),
                    ),
                    evidence_complete=bool(evidence_complete),
                )
            )
        for alert in validated_alerts:
            ranked = _alert_step(
                repository,
                alert,
                snapshot=snapshot,
                alert_snapshot=alert_snapshot,
                workflow=workflow,
                advice=_effective_advice(
                    decisions,
                    kind="alert",
                    repository=repository,
                    number=alert.get("number"),
                ),
            )
            if ranked is not None:
                ranked_steps.append(ranked)
        ranked_steps.sort(key=lambda item: item[0])
        steps = [item[1] for item in ranked_steps]
        _attach_conflicts(steps)
        for index, step in enumerate(steps):
            step["order"] = index + 1
        total_steps += len(steps)
        unresolved_evidence = {
            "errors": list(repository_report.get("errors") or []),
            "truncations": list(repository_report.get("truncations") or []),
            "manifest_status": (
                (repository_report.get("manifest_evidence") or {}).get("status")
            ),
        }
        repository_plan_core = {
            "repository": repository,
            "expected_base": repository_report.get("expected_base"),
            "status": (
                "complete" if evidence_complete else "partial"
            ),
            "evidence_snapshot": dict(snapshot),
            "alert_snapshot": alert_snapshot,
            "workflow_profile": dict(workflow),
            "state": dict(repository_report.get("summary") or {}),
            "recommended_order": steps,
            "unresolved_evidence": unresolved_evidence,
            "mutation_authorized": False,
        }
        repository_plans.append(
            {
                "plan_id": _repository_plan_identifier(repository_plan_core),
                **repository_plan_core,
            }
        )
    status = "partial" if advisory_report.get("status") == "partial" or any(
        item["status"] == "partial" for item in repository_plans
    ) else "complete"
    repository_plan_ids = [item["plan_id"] for item in repository_plans]
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "mode": "plan",
        "generated_at": advisory_report.get("generated_at"),
        "plan_id": _overall_plan_identifier(status, repository_plan_ids),
        "status": status,
        "mutation_authorized": False,
        "capabilities": {
            "model_calls": bool(
                (advisory_report.get("capabilities") or {}).get("model_calls")
            ),
            "persistent_local_writes": False,
            "github_mutations": False,
            "notifications": False,
        },
        "summary": {
            "repository_count": len(repository_plans),
            "recommended_step_count": total_steps,
            "partial_repository_count": sum(
                item["status"] == "partial" for item in repository_plans
            ),
        },
        "repositories": repository_plans,
        "source": advisory_report,
    }


def plan(
    config: WatcherConfig,
    client: GitHubClient,
    *,
    provider: AdvisoryProvider | None = None,
    clock: Clock = _utc_now,
) -> dict[str, Any]:
    """Build a non-mutating ordered plan from fresh inspection and optional advice."""

    return plan_from_advisory(advise(config, client, provider=provider, clock=clock))
