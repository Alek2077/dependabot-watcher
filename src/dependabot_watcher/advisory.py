from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol, Sequence

from .config import AdvisoryConfig, WatcherConfig
from .github import GitHubClient
from .inspector import Clock, _utc_now, inspect


ADVISORY_SCHEMA_VERSION = 1
RECOMMENDED_ACTIONS = frozenset(
    {
        "merge_candidate",
        "hold",
        "review_required",
        "investigate",
        "human_attention",
        "no_action",
    }
)
RISK_LEVELS = frozenset({"low", "medium", "high"})
CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})


class AdvisoryError(RuntimeError):
    """Raised when an advisory provider cannot produce a trustworthy result."""


class AdvisoryContractError(AdvisoryError):
    """Raised when provider output does not satisfy the advisory contract."""


def advisory_action_policy(config: WatcherConfig) -> dict[str, Any]:
    """Return the stable non-secret advisory policy bound into executable plans."""

    policy_input = {
        "schema_version": 1,
        "advice_required": config.operation.require_advice_before_execution,
        "mode": config.advisory.mode,
        "provider": config.advisory.provider,
        "budget": asdict(config.advisory.budget),
        "codex_cli": (
            asdict(config.advisory.codex_cli)
            if config.advisory.codex_cli is not None
            else None
        ),
    }
    payload = json.dumps(
        policy_input,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "advice_required": config.operation.require_advice_before_execution,
        "advisory_mode": config.advisory.mode,
        "provider": config.advisory.provider,
        "policy_id": f"advisory_policy_{hashlib.sha256(payload).hexdigest()[:24]}",
    }


@dataclass(frozen=True)
class AdvisoryRequest:
    role: str
    candidates: tuple[dict[str, Any], ...]
    queue_synopsis: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": ADVISORY_SCHEMA_VERSION,
            "role": self.role,
            "candidates": list(self.candidates),
        }
        if self.queue_synopsis is not None:
            payload["queue_synopsis"] = copy.deepcopy(dict(self.queue_synopsis))
        return payload


@dataclass(frozen=True)
class ProviderResponse:
    payload: Mapping[str, Any]
    provider: str
    model: str | None
    reasoning_effort: str | None
    duration_seconds: float
    usage: Mapping[str, int]


class AdvisoryProvider(Protocol):
    def analyze(self, request: AdvisoryRequest, *, stage: str) -> ProviderResponse:
        """Return schema-shaped advice without mutating the target repositories."""

        ...


@dataclass(frozen=True)
class _Candidate:
    candidate_id: str
    kind: str
    identity: dict[str, Any]
    provider_input: dict[str, Any]
    priority: tuple[int, str, int]


def _candidate_id(kind: str, identity: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"kind": kind, "identity": identity},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"cand_{hashlib.sha256(encoded).hexdigest()[:20]}"


def _candidate_payload(candidate: _Candidate, prior_advice: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = copy.deepcopy(candidate.provider_input)
    payload["candidate_id"] = candidate.candidate_id
    if prior_advice is not None:
        payload["prior_advice"] = copy.deepcopy(dict(prior_advice))
    return payload


def _pull_request_candidate(repository: str, assessment: Mapping[str, Any]) -> _Candidate:
    raw_identity = assessment.get("candidate_identity")
    identity = dict(raw_identity) if isinstance(raw_identity, dict) else {}
    number = identity.get("number")
    numeric_number = number if isinstance(number, int) else 0
    trusted_identity = {
        "repository": identity.get("repository") or repository,
        "number": identity.get("number"),
        "expected_base": identity.get("expected_base"),
        "observed_base": identity.get("observed_base"),
        "head_ref": identity.get("head_ref"),
        "head_sha": identity.get("head_sha"),
    }
    display_evidence = {
        "title": identity.get("title"),
        "url": identity.get("url"),
    }
    deterministic_assessment = {
        key: value
        for key, value in assessment.items()
        if key not in {"candidate_identity", "mutation_authorized"}
    }
    classification = deterministic_assessment.get("classification")
    priority_rank = 2 if classification != "baseline_candidate" else 4
    candidate_identity = {**trusted_identity, **display_evidence}
    candidate = _Candidate(
        candidate_id=_candidate_id("pull_request", trusted_identity),
        kind="pull_request",
        identity=candidate_identity,
        provider_input={
            "kind": "pull_request",
            "trusted_identity": trusted_identity,
            "untrusted_display_evidence": display_evidence,
            "deterministic_assessment": deterministic_assessment,
        },
        priority=(priority_rank, repository.casefold(), numeric_number),
    )
    return candidate


def _alert_candidate(repository: str, alert: Mapping[str, Any]) -> _Candidate:
    number = alert.get("number")
    numeric_number = number if isinstance(number, int) else 0
    trusted_identity = {"repository": repository, "number": number}
    severity = str(alert.get("severity") or "unknown").casefold()
    severity_rank = {"critical": 0, "high": 1, "medium": 3, "moderate": 3, "low": 5}.get(
        severity,
        2,
    )
    display_evidence = {
        "dependency_name": alert.get("dependency_name"),
        "ecosystem": alert.get("ecosystem"),
        "manifest_path": alert.get("manifest_path"),
        "severity": alert.get("severity"),
        "first_patched_version": alert.get("first_patched_version"),
        "url": alert.get("url") or alert.get("html_url"),
    }
    return _Candidate(
        candidate_id=_candidate_id("alert", trusted_identity),
        kind="alert",
        identity={**trusted_identity, **display_evidence},
        provider_input={
            "kind": "alert",
            "trusted_identity": trusted_identity,
            "collected_alert_evidence": dict(alert),
        },
        priority=(severity_rank, repository.casefold(), numeric_number),
    )


def _build_candidates(
    inspection: Mapping[str, Any],
) -> tuple[list[_Candidate], list[dict[str, Any]]]:
    candidates: list[_Candidate] = []
    deferred: list[dict[str, Any]] = []
    repositories = inspection.get("repositories")
    if not isinstance(repositories, list):
        return candidates, deferred
    for repository_report in repositories:
        if not isinstance(repository_report, dict):
            continue
        repository = str(repository_report.get("repository") or "unknown")
        repository_complete = not repository_report.get("errors") and not repository_report.get(
            "truncations"
        )
        repository_candidates: list[_Candidate] = []
        for assessment in repository_report.get("pull_requests") or []:
            if isinstance(assessment, dict):
                repository_candidates.append(_pull_request_candidate(repository, assessment))
        for alert in repository_report.get("alerts") or []:
            if not isinstance(alert, dict):
                continue
            relationships = alert.get("package_related_open_pull_requests") or []
            has_planning_coverage = any(
                isinstance(relationship, dict)
                and relationship.get("coverage_evidence") in {"direct", "likely"}
                for relationship in relationships
            )
            if (
                alert.get("classification") == "likely_covered_by_open_pr"
                or has_planning_coverage
            ):
                continue
            repository_candidates.append(_alert_candidate(repository, alert))
        if repository_complete:
            candidates.extend(repository_candidates)
        else:
            deferred.extend(
                _deferred(candidate, "repository_inspection_incomplete")
                for candidate in repository_candidates
            )
    candidates.sort(key=lambda candidate: candidate.priority)
    return candidates, deferred


def queue_synopsis(inspection: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact complete-queue view for model ordering awareness."""

    repository_synopses: list[dict[str, Any]] = []
    total_items = 0
    for raw_repository in inspection.get("repositories") or []:
        if not isinstance(raw_repository, Mapping):
            continue
        pull_requests: list[dict[str, Any]] = []
        for assessment in raw_repository.get("pull_requests") or []:
            if not isinstance(assessment, Mapping):
                continue
            identity = assessment.get("candidate_identity")
            trusted_identity = identity if isinstance(identity, Mapping) else {}
            path_assessment = assessment.get("path_assessment")
            path_data = path_assessment if isinstance(path_assessment, Mapping) else {}
            paths = [
                str(item.get("path"))
                for item in path_data.get("files") or []
                if isinstance(item, Mapping) and item.get("path")
            ]
            related_alerts = [
                item.get("number")
                for item in assessment.get("related_alerts") or []
                if isinstance(item, Mapping) and isinstance(item.get("number"), int)
            ]
            pull_requests.append(
                {
                    "number": trusted_identity.get("number"),
                    "head_sha": trusted_identity.get("head_sha"),
                    "observed_base": trusted_identity.get("observed_base"),
                    "title": trusted_identity.get("title"),
                    "classification": assessment.get("classification"),
                    "mergeable": assessment.get("mergeable"),
                    "merge_state_status": assessment.get("merge_state_status"),
                    "checks": assessment.get("checks"),
                    "changed_paths": paths[:10],
                    "changed_paths_truncated": len(paths) > 10,
                    "related_alert_numbers": related_alerts,
                }
            )
        alerts: list[dict[str, Any]] = []
        for alert in raw_repository.get("alerts") or []:
            if not isinstance(alert, Mapping):
                continue
            relationships = [
                {
                    "pull_request_number": relationship.get("number"),
                    "coverage_evidence": relationship.get("coverage_evidence"),
                    "target_version": relationship.get("target_version"),
                }
                for relationship in alert.get("package_related_open_pull_requests") or []
                if isinstance(relationship, Mapping)
            ]
            alerts.append(
                {
                    "number": alert.get("number"),
                    "dependency_name": alert.get("dependency_name"),
                    "ecosystem": alert.get("ecosystem"),
                    "manifest_path": alert.get("manifest_path"),
                    "severity": alert.get("severity"),
                    "classification": alert.get("classification"),
                    "first_patched_version": alert.get("first_patched_version"),
                    "pull_request_relationships": relationships,
                }
            )
        total_items += len(pull_requests) + len(alerts)
        evidence_snapshot = raw_repository.get("evidence_snapshot")
        snapshot = evidence_snapshot if isinstance(evidence_snapshot, Mapping) else {}
        raw_summary = raw_repository.get("summary")
        repository_summary = raw_summary if isinstance(raw_summary, Mapping) else {}
        repository_synopses.append(
            {
                "repository": raw_repository.get("repository"),
                "expected_base": raw_repository.get("expected_base"),
                "base_sha": snapshot.get("base_sha"),
                "summary": copy.deepcopy(dict(repository_summary)),
                "evidence_complete": not bool(
                    raw_repository.get("errors") or raw_repository.get("truncations")
                ),
                "pull_requests": pull_requests,
                "alerts": alerts,
            }
        )
    return {
        "repository_count": len(repository_synopses),
        "item_count": total_items,
        "complete": inspection.get("status") == "complete",
        "truncated": any(
            bool(repository.get("truncations"))
            for repository in inspection.get("repositories") or []
            if isinstance(repository, Mapping)
        ),
        "semantic_relationship_status": "only_collected_relationships",
        "repositories": repository_synopses,
    }


def _string_list(value: object, location: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AdvisoryContractError(f"{location} must be an array of strings.")
    return list(value)


def validate_provider_payload(
    payload: Mapping[str, Any],
    expected_candidate_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise AdvisoryContractError("Provider response must be a JSON object.")
    if set(payload) != {"decisions"}:
        raise AdvisoryContractError("Provider response must contain only a decisions field.")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise AdvisoryContractError("Provider response decisions must be an array.")
    expected = set(expected_candidate_ids)
    validated: dict[str, dict[str, Any]] = {}
    required = {
        "candidate_id",
        "recommended_action",
        "risk_level",
        "confidence",
        "requires_review",
        "summary",
        "rationale",
        "evidence",
        "unresolved_questions",
    }
    for index, raw_decision in enumerate(decisions):
        location = f"decisions[{index}]"
        if not isinstance(raw_decision, dict) or set(raw_decision) != required:
            raise AdvisoryContractError(f"{location} has an invalid field set.")
        candidate_id = raw_decision.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in expected:
            raise AdvisoryContractError(f"{location}.candidate_id is unknown.")
        if candidate_id in validated:
            raise AdvisoryContractError(f"{location}.candidate_id is duplicated.")
        action = raw_decision.get("recommended_action")
        if not isinstance(action, str) or action not in RECOMMENDED_ACTIONS:
            raise AdvisoryContractError(f"{location}.recommended_action is invalid.")
        risk = raw_decision.get("risk_level")
        if not isinstance(risk, str) or risk not in RISK_LEVELS:
            raise AdvisoryContractError(f"{location}.risk_level is invalid.")
        confidence = raw_decision.get("confidence")
        if not isinstance(confidence, str) or confidence not in CONFIDENCE_LEVELS:
            raise AdvisoryContractError(f"{location}.confidence is invalid.")
        requires_review = raw_decision.get("requires_review")
        if not isinstance(requires_review, bool):
            raise AdvisoryContractError(f"{location}.requires_review must be a boolean.")
        summary = raw_decision.get("summary")
        rationale = raw_decision.get("rationale")
        if not isinstance(summary, str) or not summary.strip():
            raise AdvisoryContractError(f"{location}.summary must be a non-empty string.")
        if not isinstance(rationale, str) or not rationale.strip():
            raise AdvisoryContractError(f"{location}.rationale must be a non-empty string.")
        validated[candidate_id] = {
            "candidate_id": candidate_id,
            "recommended_action": action,
            "risk_level": risk,
            "confidence": confidence,
            "requires_review": requires_review,
            "summary": summary.strip(),
            "rationale": rationale.strip(),
            "evidence": _string_list(raw_decision.get("evidence"), f"{location}.evidence"),
            "unresolved_questions": _string_list(
                raw_decision.get("unresolved_questions"),
                f"{location}.unresolved_questions",
            ),
        }
    missing = expected - set(validated)
    if missing:
        raise AdvisoryContractError("Provider response omitted one or more requested candidates.")
    return validated


def _call_metadata(response: ProviderResponse, stage: str, candidate_count: int) -> dict[str, Any]:
    return {
        "stage": stage,
        "provider": response.provider,
        "model": response.model,
        "reasoning_effort": response.reasoning_effort,
        "candidate_count": candidate_count,
        "duration_seconds": response.duration_seconds,
        "usage": dict(response.usage),
    }


def _validate_candidate_actions(
    decisions: Mapping[str, Mapping[str, Any]],
    candidates: Sequence[_Candidate],
    *,
    stage: str,
) -> None:
    for candidate in candidates:
        decision = decisions[candidate.candidate_id]
        action = decision.get("recommended_action")
        requires_review = decision.get("requires_review")
        if action == "review_required" and not requires_review:
            raise AdvisoryContractError(
                "A review-required recommendation must set requires_review to true."
            )
        if stage == "triage" and action == "human_attention":
            raise AdvisoryContractError(
                "Triage must route unresolved judgment to review before requesting human attention."
            )
        if stage == "review" and action == "review_required":
            raise AdvisoryContractError(
                "The review stage must resolve the candidate or request human attention."
            )
        if stage == "review" and requires_review:
            raise AdvisoryContractError(
                "A completed review result must set requires_review to false."
            )
        if candidate.kind == "alert" and action == "merge_candidate":
            raise AdvisoryContractError("An alert cannot be returned as a merge candidate.")
        assessment = candidate.provider_input.get("deterministic_assessment") or {}
        if (
            candidate.kind == "pull_request"
            and assessment.get("classification") != "baseline_candidate"
            and action == "merge_candidate"
        ):
            raise AdvisoryContractError(
                "A pull request with deterministic blockers cannot be returned as a merge candidate."
            )


def _needs_review(candidate: _Candidate, decision: Mapping[str, Any]) -> bool:
    assessment = candidate.provider_input.get("deterministic_assessment") or {}
    deterministic_attention = (
        candidate.kind == "alert"
        or assessment.get("classification") != "baseline_candidate"
    )
    return bool(
        deterministic_attention
        or decision.get("requires_review")
        or decision.get("risk_level") != "low"
        or decision.get("confidence") != "high"
        or decision.get("recommended_action") not in {"merge_candidate", "no_action"}
    )


def _deferred(candidate: _Candidate, reason: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "kind": candidate.kind,
        "identity": candidate.identity,
        "reason": reason,
        "mutation_authorized": False,
    }


def _incomplete_review_advice(
    triage_decision: Mapping[str, Any],
) -> dict[str, Any]:
    unresolved_questions = list(triage_decision.get("unresolved_questions") or [])
    unresolved_questions.append("Required reviewer advice was not completed.")
    return {
        "candidate_id": triage_decision.get("candidate_id"),
        "recommended_action": "review_required",
        "risk_level": "high",
        "confidence": "low",
        "requires_review": True,
        "summary": "Required review did not complete; triage advice is not effective.",
        "rationale": (
            "Reviewed mode requires a successful second-stage result for this candidate. "
            "The triage result remains available as evidence only."
        ),
        "evidence": list(triage_decision.get("evidence") or []),
        "unresolved_questions": unresolved_questions,
    }


def _provider_for(config: AdvisoryConfig) -> AdvisoryProvider:
    if config.provider == "codex_cli" and config.codex_cli is not None:
        from .providers.codex_cli import CodexCliProvider

        return CodexCliProvider(config.codex_cli)
    raise AdvisoryError("No configured advisory provider is available.")


def advise_from_inspection(
    config: WatcherConfig,
    inspection: dict[str, Any],
    provider: AdvisoryProvider | None = None,
) -> dict[str, Any]:
    advisory_config = config.advisory
    candidates, deferred = _build_candidates(inspection)
    synopsis = queue_synopsis(inspection)
    action_policy = advisory_action_policy(config)
    if advisory_config.mode == "off":
        return {
            "schema_version": ADVISORY_SCHEMA_VERSION,
            "mode": "advise",
            "generated_at": inspection.get("generated_at"),
            "status": inspection.get("status"),
            "mutation_authorized": False,
            "capabilities": {
                "model_calls": False,
                "persistent_local_writes": False,
                "github_mutations": False,
                "notifications": False,
            },
            "inspection": inspection,
            "advisory": {
                "mode": "off",
                "provider": None,
                "required_for_execution": (
                    config.operation.require_advice_before_execution
                ),
                "action_policy_id": action_policy["policy_id"],
                "status": "disabled",
                "budget": asdict(advisory_config.budget),
                "summary": {
                    "eligible_candidate_count": len(candidates),
                    "advised_candidate_count": 0,
                    "reviewed_candidate_count": 0,
                    "deferred_candidate_count": len(deferred),
                    "provider_call_count": 0,
                },
                "decisions": [],
                "deferred": deferred,
                "errors": [],
                "calls": [],
            },
        }

    active_provider = provider or _provider_for(advisory_config)
    budget = advisory_config.budget
    selected = candidates[: budget.max_candidates]
    deferred.extend(_deferred(candidate, "advisory_candidate_budget") for candidate in candidates[len(selected) :])
    triage_decisions: dict[str, dict[str, Any]] = {}
    review_decisions: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    attempted_calls = 0

    triage_batches = [
        selected[index : index + budget.batch_size]
        for index in range(0, len(selected), budget.batch_size)
    ]
    triage_batches = triage_batches[: budget.max_triage_calls]
    triage_covered = sum(len(batch) for batch in triage_batches)
    deferred.extend(_deferred(candidate, "triage_call_budget") for candidate in selected[triage_covered:])

    for batch_index, batch in enumerate(triage_batches):
        request = AdvisoryRequest(
            role="triage",
            candidates=tuple(_candidate_payload(candidate) for candidate in batch),
            queue_synopsis=synopsis,
        )
        try:
            attempted_calls += 1
            response = active_provider.analyze(request, stage="triage")
        except Exception:
            errors.append(
                {
                    "stage": "triage",
                    "batch": batch_index + 1,
                    "category": "provider_failure",
                    "message": "Advisory provider failed during triage; details were withheld.",
                }
            )
            remaining = [candidate for later in triage_batches[batch_index:] for candidate in later]
            deferred.extend(_deferred(candidate, "triage_provider_failure") for candidate in remaining)
            break
        try:
            validated = validate_provider_payload(
                response.payload,
                [candidate.candidate_id for candidate in batch],
            )
            _validate_candidate_actions(validated, batch, stage="triage")
            triage_decisions.update(validated)
            calls.append(_call_metadata(response, "triage", len(batch)))
        except AdvisoryContractError as exc:
            errors.append(
                {
                    "stage": "triage",
                    "batch": batch_index + 1,
                    "category": "invalid_provider_response",
                    "message": str(exc),
                }
            )
            remaining = [candidate for later in triage_batches[batch_index:] for candidate in later]
            deferred.extend(_deferred(candidate, "triage_contract_failure") for candidate in remaining)
            break

    selected_by_id = {candidate.candidate_id: candidate for candidate in selected}
    if advisory_config.mode == "reviewed" and not errors:
        review_candidates = [
            candidate
            for candidate in selected
            if candidate.candidate_id in triage_decisions
            and _needs_review(candidate, triage_decisions[candidate.candidate_id])
        ]
        review_selected = review_candidates[: budget.max_review_candidates]
        deferred.extend(
            _deferred(candidate, "review_candidate_budget")
            for candidate in review_candidates[len(review_selected) :]
        )
        review_batches = [
            review_selected[index : index + budget.batch_size]
            for index in range(0, len(review_selected), budget.batch_size)
        ]
        review_batches = review_batches[: budget.max_review_calls]
        review_covered = sum(len(batch) for batch in review_batches)
        deferred.extend(
            _deferred(candidate, "review_call_budget")
            for candidate in review_selected[review_covered:]
        )
        for batch_index, batch in enumerate(review_batches):
            request = AdvisoryRequest(
                role="review",
                candidates=tuple(
                    _candidate_payload(candidate, triage_decisions[candidate.candidate_id])
                    for candidate in batch
                ),
                queue_synopsis=synopsis,
            )
            try:
                attempted_calls += 1
                response = active_provider.analyze(request, stage="review")
            except Exception:
                errors.append(
                    {
                        "stage": "review",
                        "batch": batch_index + 1,
                        "category": "provider_failure",
                        "message": "Advisory provider failed during review; details were withheld.",
                    }
                )
                remaining = [candidate for later in review_batches[batch_index:] for candidate in later]
                deferred.extend(_deferred(candidate, "review_provider_failure") for candidate in remaining)
                break
            try:
                validated = validate_provider_payload(
                    response.payload,
                    [candidate.candidate_id for candidate in batch],
                )
                _validate_candidate_actions(validated, batch, stage="review")
                review_decisions.update(validated)
                calls.append(_call_metadata(response, "review", len(batch)))
            except AdvisoryContractError as exc:
                errors.append(
                    {
                        "stage": "review",
                        "batch": batch_index + 1,
                        "category": "invalid_provider_response",
                        "message": str(exc),
                    }
                )
                remaining = [candidate for later in review_batches[batch_index:] for candidate in later]
                deferred.extend(_deferred(candidate, "review_contract_failure") for candidate in remaining)
                break

    decisions: list[dict[str, Any]] = []
    for candidate_id, triage_decision in triage_decisions.items():
        candidate = selected_by_id[candidate_id]
        review_decision = review_decisions.get(candidate_id)
        review_required = (
            advisory_config.mode == "reviewed"
            and _needs_review(candidate, triage_decision)
        )
        if review_required and review_decision is None:
            effective_advice = _incomplete_review_advice(triage_decision)
        else:
            effective_advice = review_decision or triage_decision
        decisions.append(
            {
                "candidate_id": candidate_id,
                "kind": candidate.kind,
                "identity": candidate.identity,
                "triage": triage_decision,
                "review": review_decision,
                "effective_advice": effective_advice,
                "mutation_authorized": False,
            }
        )
    decisions.sort(key=lambda item: selected_by_id[item["candidate_id"]].priority)
    status = "partial" if errors or inspection.get("status") == "partial" else "complete"
    return {
        "schema_version": ADVISORY_SCHEMA_VERSION,
        "mode": "advise",
        "generated_at": inspection.get("generated_at"),
        "status": status,
        "mutation_authorized": False,
        "capabilities": {
            "model_calls": attempted_calls > 0,
            "persistent_local_writes": False,
            "github_mutations": False,
            "notifications": False,
        },
        "inspection": inspection,
        "advisory": {
            "mode": advisory_config.mode,
            "provider": advisory_config.provider,
            "required_for_execution": config.operation.require_advice_before_execution,
            "action_policy_id": action_policy["policy_id"],
            "status": "partial" if errors else "complete",
            "budget": asdict(budget),
            "summary": {
                "eligible_candidate_count": len(candidates),
                "advised_candidate_count": len(decisions),
                "reviewed_candidate_count": len(review_decisions),
                "deferred_candidate_count": len(deferred),
                "provider_call_count": attempted_calls,
            },
            "decisions": decisions,
            "deferred": deferred,
            "errors": errors,
            "calls": calls,
        },
    }


def advise(
    config: WatcherConfig,
    client: GitHubClient,
    *,
    provider: AdvisoryProvider | None = None,
    clock: Clock = _utc_now,
) -> dict[str, Any]:
    """Inspect and optionally obtain bounded provider advice without mutation."""

    inspection = inspect(config, client, clock=clock)
    return advise_from_inspection(config, inspection, provider)
