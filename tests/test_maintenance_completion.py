from __future__ import annotations

import copy
from typing import Any

import pytest

from dependabot_watcher.config import ConfigError, parse_config
from dependabot_watcher.maintenance_completion import new_obligation, obligation_scope, pending_stage, validate_obligation
from dependabot_watcher.review_queue import ReviewQueueError


def case(policy: str = "remote_only", targets: bool = True) -> tuple[Any, dict[str, Any]]:
    repository: dict[str, Any] = {"name": "example-org/example-app", "default_branch": "integration",
                                  "completion_policy": policy}
    if targets:
        repository["local_sync"] = [{"directory": "/selected/project", "branch": "integration"}]
    config = parse_config({"schema_version": 1, "repositories": [repository]})
    preparation = {"action_id": "action_" + "a" * 24, "binding": {
        "repository": "example-org/example-app", "expected_base": "integration"}}
    preparation["decision"] = {"completion_scope": copy.deepcopy(obligation_scope(new_obligation(config, preparation)))}
    return config, preparation


def test_registered_checkout_does_not_select_required_completion_or_grant_mutation() -> None:
    config, preparation = case()
    obligation = new_obligation(config, preparation)
    assert obligation["policy"] == "remote_only" and obligation["targets"] == []
    assert not config.operation.mutation_enabled
    assert not obligation["assessment_required"]
    validate_obligation(obligation, preparation)


def test_uninstalled_unbound_development_schema_is_not_silently_reconstructed() -> None:
    config, preparation = case()
    obligation = new_obligation(config, preparation)
    obligation["schema_version"] = 1
    obligation.pop("finalization")
    with pytest.raises(ReviewQueueError, match="unsupported development schema"):
        validate_obligation(obligation, preparation)


def test_finalization_requires_all_selected_evidence_and_is_not_a_display_label() -> None:
    config, preparation = case("selected_local_sync")
    obligation = new_obligation(config, preparation)
    obligation["merge_commit_sha"] = "c" * 40
    obligation["evidence"] = {"context_digest": "a" * 64, "base_sha": "c" * 40}
    obligation["finalization"] = {"context_digest": "a" * 64, "base_sha": "c" * 40,
                                  "verified_at": "2026-10-10T12:00:00Z"}
    with pytest.raises(ReviewQueueError, match="verification"):
        validate_obligation(obligation, preparation)
    obligation["local_results"][obligation["targets"][0]["target_id"]] = {
        "head_sha": "c" * 40, "remote_sha": "c" * 40}
    validate_obligation(obligation, preparation)
    assert pending_stage(obligation) == "completed"


def test_selected_policy_freezes_targets_and_follows_selected_base_not_main() -> None:
    config, preparation = case("selected_local_sync")
    obligation = new_obligation(config, preparation)
    assert obligation["targets"][0]["branch"] == "integration"
    assert obligation["targets"][0]["directory"] == "/selected/project"
    assert len(obligation["targets"][0]["target_id"]) == 32
    validate_obligation(obligation, preparation)
    changed, _ = case(targets=False)
    assert new_obligation(changed, preparation)["targets"] == []
    assert obligation["targets"]  # Current configuration cannot erase saved scope.


@pytest.mark.parametrize("policy", [None, True, [], {}, "automatic", ""])
def test_unknown_completion_policy_is_refused(policy: Any) -> None:
    with pytest.raises(ConfigError, match="completion_policy"):
        parse_config({"schema_version": 1, "repositories": [{"name": "org/repo", "completion_policy": policy}]})


def test_required_completion_needs_targets_of_the_selected_merge_branch() -> None:
    with pytest.raises(ConfigError, match="requires selected"):
        case("selected_local_sync", targets=False)
    with pytest.raises(ConfigError, match="default_branch"):
        parse_config({"schema_version": 1, "repositories": [{"name": "org/repo",
            "completion_policy": "selected_local_sync",
            "local_sync": [{"directory": "/selected", "branch": "another-branch"}]}]})


@pytest.mark.parametrize("change", ["action", "policy", "coordinated_scope", "target", "duplicate", "receipt", "premature"])
def test_ambiguous_or_unbound_saved_completion_is_refused(change: str) -> None:
    config, preparation = case("selected_local_sync")
    obligation = new_obligation(config, preparation)
    if change == "action":
        obligation["action_id"] = "action_" + "b" * 24
    elif change == "policy":
        obligation["policy"] = "remote_only"
    elif change == "coordinated_scope":
        obligation.update(policy="remote_only", targets=[])
    elif change == "target":
        obligation["targets"][0]["directory"] = "/different"
    elif change == "duplicate":
        obligation["targets"].append(copy.deepcopy(obligation["targets"][0]))
    elif change == "receipt":
        obligation["local_results"]["unknown"] = {"head_sha": "f" * 40, "remote_sha": "f" * 40}
    else:
        obligation["evidence"] = {"context_digest": "a" * 64, "base_sha": "b" * 40}
    with pytest.raises(ReviewQueueError, match="completion"):
        validate_obligation(obligation, preparation)


def test_recorded_progress_never_itself_claims_live_local_completion() -> None:
    config, preparation = case("selected_local_sync")
    obligation = new_obligation(config, preparation)
    assert pending_stage(obligation) == "merge_outcome"
    obligation["merge_commit_sha"] = "c" * 40
    assert pending_stage(obligation) == "refresh"
    obligation["evidence"] = {"context_digest": "a" * 64, "base_sha": "c" * 40}
    assert pending_stage(obligation) == "local_sync"
    obligation["local_results"][obligation["targets"][0]["target_id"]] = {
        "head_sha": "c" * 40, "remote_sha": "c" * 40}
    assert pending_stage(obligation) == "verification"
    validate_obligation(obligation, preparation)
