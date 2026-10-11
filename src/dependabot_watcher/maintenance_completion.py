"""Bounded completion obligations attached to an exact approved remote action.

This module owns no Git or provider runtime. A saved obligation is workflow
scope, never permission to replay a merge, launch a model or update a checkout.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .config import WatcherConfig, repository_names_equal
from .review_queue import ReviewQueueError


_SHA = re.compile(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}")
_FIELDS = {"schema_version", "action_id", "repository", "base_branch", "policy",
           "targets", "assessment_required", "merge_commit_sha", "evidence", "assessment", "local_results", "finalization"}


def obligation_scope(value: Mapping[str, Any]) -> dict[str, Any]:
    """The immutable portion displayed and bound in action preparation."""
    return {key: value[key] for key in ("policy", "targets", "assessment_required")}


def _target_id(repository: str, directory: str, branch: str) -> str:
    return hashlib.sha256(json.dumps([repository.casefold(), directory, branch],
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def new_obligation(config: WatcherConfig, preparation: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the selected completion scope before consent can be consumed."""
    binding = preparation["binding"]
    repository = next(item for item in config.repositories
                      if repository_names_equal(item.name, binding["repository"]))
    targets = [{"repository": repository.name, "directory": target.directory,
                "branch": target.branch,
                "target_id": _target_id(repository.name, target.directory, target.branch)}
               for target in repository.local_sync] if repository.completion_policy == "selected_local_sync" else []
    return {"schema_version": 2, "action_id": preparation["action_id"],
            "repository": binding["repository"], "base_branch": binding["expected_base"],
            "policy": repository.completion_policy, "targets": targets,
            "assessment_required": config.supervisor.mode != "off", "merge_commit_sha": None,
            "evidence": None, "assessment": None, "local_results": {}, "finalization": None}


def validate_obligation(value: object, preparation: Mapping[str, Any]) -> None:
    """Reject ambiguous or broadened durable state before using it as a gate."""
    if isinstance(value, dict) and value.get("schema_version") != 2:
        # Version 1 existed only in this uninstalled development slice and did
        # not anchor scope in the reviewed preparation. Do not invent consent
        # from today's mutable configuration or silently discard an obligation.
        raise ReviewQueueError("Maintenance completion uses an unsupported development schema; preserve saved history for explicit recovery.")
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ReviewQueueError("Maintenance completion has an invalid schema.")
    binding = preparation["binding"]
    decision = preparation.get("decision")
    if not isinstance(decision, Mapping) or decision.get("completion_scope") != obligation_scope(value):
        raise ReviewQueueError("Maintenance completion differs from the reviewed workflow scope.")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 2
            or value["action_id"] != preparation["action_id"]
            or value["repository"] != binding["repository"]
            or value["base_branch"] != binding["expected_base"]
            or not isinstance(value["policy"], str)
            or value["policy"] not in {"remote_only", "selected_local_sync"}
            or type(value["assessment_required"]) is not bool
            or not isinstance(value["targets"], list) or len(value["targets"]) > 10
            or (value["policy"] == "selected_local_sync") != bool(value["targets"])
            or (value["merge_commit_sha"] is not None and (
                not isinstance(value["merge_commit_sha"], str) or not _SHA.fullmatch(value["merge_commit_sha"])))):
        raise ReviewQueueError("Maintenance completion has an invalid action or policy binding.")
    identifiers: set[str] = set()
    for target in value["targets"]:
        if (not isinstance(target, dict) or set(target) != {"repository", "directory", "branch", "target_id"}
                or any(not isinstance(target[key], str) or not target[key]
                       for key in ("repository", "directory", "branch", "target_id"))
                or not repository_names_equal(target["repository"], binding["repository"])
                or not Path(target["directory"]).is_absolute() or ".." in Path(target["directory"]).parts
                or any(ord(char) < 32 for char in target["directory"])
                or target["branch"] != binding["expected_base"]
                or target["target_id"] != _target_id(target["repository"], target["directory"], target["branch"])
                or target["target_id"] in identifiers):
            raise ReviewQueueError("Maintenance completion has an invalid selected checkout.")
        identifiers.add(target["target_id"])
    if not isinstance(value["local_results"], dict) or set(value["local_results"]) - identifiers:
        raise ReviewQueueError("Maintenance completion has an unregistered local result.")
    # Receipt schemas are intentionally strict: a display label is not proof.
    evidence = value["evidence"]
    if evidence is not None and (
            not isinstance(evidence, dict) or set(evidence) != {"context_digest", "base_sha"}
            or not isinstance(evidence["context_digest"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", evidence["context_digest"])
            or not isinstance(evidence["base_sha"], str) or not _SHA.fullmatch(evidence["base_sha"])):
        raise ReviewQueueError("Maintenance completion has invalid refreshed evidence.")
    assessment = value["assessment"]
    if assessment is not None and (
            not value["assessment_required"] or evidence is None
            or not isinstance(assessment, dict)
            or set(assessment) != {"job_id", "session_id", "context_digest"}
            or not isinstance(assessment["job_id"], str)
            or not re.fullmatch(r"job_[a-f0-9]{32}", assessment["job_id"])
            or not isinstance(assessment["session_id"], str) or not assessment["session_id"]
            or assessment["context_digest"] != evidence["context_digest"]):
        raise ReviewQueueError("Maintenance completion has invalid assessment provenance.")
    for result in value["local_results"].values():
        if (not isinstance(result, dict) or set(result) != {"head_sha", "remote_sha"}
                or any(not isinstance(result[key], str) or not _SHA.fullmatch(result[key])
                       for key in ("head_sha", "remote_sha"))
                or result["head_sha"] != result["remote_sha"]):
            raise ReviewQueueError("Maintenance completion has invalid local evidence.")
    if value["merge_commit_sha"] is None and (evidence is not None or assessment is not None or value["local_results"]):
        raise ReviewQueueError("Maintenance completion evidence lacks its confirmed merge revision.")
    finalization = value["finalization"]
    if finalization is not None:
        if (not isinstance(finalization, dict) or set(finalization) != {"verified_at", "base_sha", "context_digest"}
                or evidence is None or value["merge_commit_sha"] is None
                or finalization["base_sha"] != evidence["base_sha"]
                or finalization["context_digest"] != evidence["context_digest"]
                or (value["assessment_required"] and assessment is None)
                or set(value["local_results"]) != identifiers
                or any(result["head_sha"] != finalization["base_sha"] for result in value["local_results"].values())
                or not isinstance(finalization["verified_at"], str)):
            raise ReviewQueueError("Maintenance completion has invalid final verification evidence.")
        try:
            verified_at = datetime.fromisoformat(finalization["verified_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReviewQueueError("Maintenance completion verification time is invalid.") from exc
        if verified_at.utcoffset() is None:
            raise ReviewQueueError("Maintenance completion verification time requires a time zone.")


def pending_stage(value: Mapping[str, Any]) -> str:
    """Describe recorded progress; live local verification is still required."""
    if value["finalization"] is not None:
        return "completed"
    if value["merge_commit_sha"] is None:
        return "merge_outcome"
    if value["evidence"] is None:
        return "refresh"
    if value["assessment_required"] and value["assessment"] is None:
        return "assessment"
    if any(target["target_id"] not in value["local_results"] for target in value["targets"]):
        return "local_sync"
    return "verification"
