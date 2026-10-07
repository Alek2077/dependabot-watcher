"""Configured single-repository supervision using the existing owned lifecycle.

This is a backend entry point, not a scheduler, UI or remote mutation path.
The caller supplies the trusted saved profile and collection client; messages
cannot choose models, commands, workspaces or access. Unexpected permissions
still require a trusted presentation/consumer, not an invisible terminal wait.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Literal, cast

from .github import GitHubClient
from .config import SupervisorConfig, SupervisorWorkspaceConfig
from .providers.codex_app_server_settings import CodexAppServerSettings
from .providers.codex_app_server_supervisor import CodexAppServerSupervisor, PermissionNotice, PermissionPresentation
from .reconciliation import reconcile
from .review_queue import ReviewQueue, ReviewQueueError, supervisor_context_digest
from .runtime_state import ensure_private_directory, run_lock
from .supervision import SupervisorJobs
from .supervisor_runner import SupervisorRunner
from .supervisor_workspace_refresh import refresh_supervisor_workspace, supervisor_workspace_lease


def _configured_registration(queue: ReviewQueue, repository: str) -> tuple[SupervisorConfig, SupervisorWorkspaceConfig]:
    """Shared saved-policy checks; neither runtime attestation nor a launch."""
    if queue.config_path is None:
        raise ReviewQueueError("Configured supervision requires the trusted saved configuration path.")
    queue.require_current_configuration()
    policy = queue.config.supervisor
    if (policy.mode != "approval_first" or policy.provider != "codex_app_server"
            or not policy.model_use_acknowledged or policy.model is None
            or policy.reasoning_effort is None or policy.expected_account_type is None):
        raise ReviewQueueError("Choose and acknowledge a supervisor in the saved profile before starting investigation.")
    if not isinstance(repository, str) or not any(item.name.casefold() == repository.casefold() for item in queue.config.repositories):
        raise ReviewQueueError("Supervisor repository is outside the configured scope.")
    registrations = [item for item in policy.workspaces if item.repository.casefold() == repository.casefold()]
    if len(registrations) != 1:
        raise ReviewQueueError("Register one isolated workspace for this repository before starting investigation.")
    if queue.config.advisory.mode != "off":
        raise ReviewQueueError("This supervisor route requires metadata advice off; it will not spend an additional advice budget.")
    return policy, registrations[0]


def configured_launch_summary(queue: ReviewQueue, repository: str) -> dict[str, Any]:
    """Describe selected policy without collection, workspace inspection or inference.

    The status response binds this summary to its full context digest. Fresh
    worktree/harness/account/runtime checks remain mandatory during launch.
    """
    queue.require_current_configuration()
    policy = queue.config.supervisor
    registration: SupervisorWorkspaceConfig | None = None
    reason: str | None = None
    try:
        policy, registration = _configured_registration(queue, repository)
    except ReviewQueueError as exc:
        reason = str(exc)
    return {
        "status": "configured" if registration is not None else "blocked", "reason": reason,
        "mode": policy.mode, "provider": policy.provider, "model": policy.model,
        "reasoning_effort": policy.reasoning_effort, "expected_account_type": policy.expected_account_type,
        "access": policy.access, "permission_profile": policy.permission_profile,
        "validation_authorized": policy.validation_authorized,
        "timeout_seconds": policy.timeout_seconds,
        "model_use_acknowledged": policy.model_use_acknowledged,
        "full_access_acknowledged": policy.full_access_acknowledged,
        "workspace": registration.directory if registration else None,
        "instruction_sources": list(registration.instruction_sources) if registration else [],
        "requires_fresh_preflight": True, "live_runtime_verified": False,
        "github_mutations_authorized": False,
        "usage_effect": "subscription_or_api_usage_if_launched",
        "evidence_privacy": "repository_evidence_and_saved_questions_sent_to_selected_provider_if_launched",
    }


def run_configured_supervisor(
    queue: ReviewQueue, client: GitHubClient, repository: str,
    *, permission_presentation: PermissionPresentation | None = None,
    permission_notice: PermissionNotice | None = None,
    consumer_check: Callable[[], None] | None = None,
    retry_job_id: str | None = None,
) -> dict[str, Any]:
    """Refresh, verify and run one configured job; never enable a disabled policy.

    A completed/current job is reused; failed/interrupted attempts require
    deliberate recovery rather than automatic inference replay. The per-repo
    launch lease serializes refresh/reservation and reasoning while leaving the
    ordinary queue lock free for messages, permission decisions and status.
    This initial route requires metadata advice off so fresh collection cannot
    silently spend a second provider's budget. It does not provision worktrees.
    """
    policy, _registration = _configured_registration(queue, repository)
    assert queue.config_path is not None
    assert policy.model is not None and policy.reasoning_effort is not None and policy.expected_account_type is not None
    if policy.access != "full" and permission_presentation is None:
        raise ReviewQueueError("Inherited or restricted supervision requires a connected permission presentation and consumer.")
    launch_root = ensure_private_directory(queue.directory / "supervisor-launches")
    key = hashlib.sha256(repository.casefold().encode()).hexdigest()
    with run_lock(launch_root / key), supervisor_workspace_lease(queue, repository):
        if consumer_check is not None:
            consumer_check()
        queue.require_current_configuration()
        # Reconciliation collects outside the shared state lock and rejects
        # intervening action/state changes before commit. Advice is off here.
        # No notification or merge is introduced by investigation.
        reconcile(queue.config, client, config_path=queue.config_path, state_directory=queue.directory,
                  clock=queue.clock, notification_mode="off")
        if consumer_check is not None:
            consumer_check()
        queue.require_current_configuration()
        context = queue.supervision_context(repository)
        plan = context["repository_plan"]
        if not isinstance(plan, dict):
            raise ReviewQueueError("Fresh repository collection produced no usable investigation plan.")
        proof = refresh_supervisor_workspace(queue, repository, plan)
        queue.require_current_configuration()
        queue.record_workspace_evidence(asdict(proof))
        jobs = SupervisorJobs(queue)
        with run_lock(queue.directory, wait_seconds=2):
            current = queue._supervision_context_under_lock(repository)
            digest = supervisor_context_digest(current)
            state = jobs._load()
            jobs._recover_under_lock(state, repository)
            attempts = [item for item in state["jobs"].values()
                        if item["repository"].casefold() == repository.casefold()]
            # Continue only a completed, backend-owned session whose policy and
            # full workspace receipt still match. A changed harness/base starts
            # fresh; arbitrary thread IDs never come from UI or model output.
            owners = [item for item in attempts if item["status"] in {"completed", "superseded"}
                      and item["session_id"] is not None
                      and supervisor_context_digest(item["context"].get("supervisor")) == supervisor_context_digest(current["supervisor"])
                      and item["context"].get("workspace_evidence") == current["workspace_evidence"]]
            owner = max(owners, key=lambda item: item["finished_at"]) if owners else None
        job = jobs.prepare(repository, retry_job_id=retry_job_id)
        if job["context_digest"] != digest:
            raise ReviewQueueError("Investigation context changed during preparation; refresh before launching.")
        if job["status"] != "prepared":
            return {"job": job, "reused": True}
        settings = CodexAppServerSettings(Path(proof.directory), policy.model, policy.reasoning_effort,
            expected_instruction_sources=tuple(Path(source) for source, _ in proof.instruction_fingerprints),
            access=cast(Literal["inherit", "restricted", "full"], policy.access),
            permission_profile=policy.permission_profile, full_access_acknowledged=policy.full_access_acknowledged,
            timeout_seconds=policy.timeout_seconds)
        provider = CodexAppServerSupervisor(settings, expected_account_type=policy.expected_account_type,
            session_id=owner["session_id"] if owner and "retry_of" not in job else None,
            permission_presentation=permission_presentation,
            permission_notice=permission_notice,
            consumer_check=consumer_check,
            validation_authorized=policy.validation_authorized)
        outcome = SupervisorRunner(jobs, provider).run(job["job_id"])
        return {**outcome, "reused": False}
