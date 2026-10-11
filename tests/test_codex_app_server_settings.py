from __future__ import annotations

from pathlib import Path
import json
from typing import Any

import pytest

from dependabot_watcher.providers import codex_app_server_settings as settings_module
from dependabot_watcher.providers.codex_app_server_settings import CodexAppServerSettings
from dependabot_watcher.review_queue import ReviewQueueError


def make_settings(workspace: Path, **kwargs: Any) -> CodexAppServerSettings:
    return CodexAppServerSettings(**{"workspace": workspace, "model": "gpt-5.6-luna",
                                     "reasoning_effort": "xhigh", "expected_instruction_sources": (workspace / "AGENTS.md",), **kwargs})


def response(workspace: Path) -> dict[str, Any]:
    return {"thread": {"id": "session-1"}, "cwd": str(workspace), "model": "gpt-5.6-luna",
            "modelProvider": "openai", "reasoningEffort": "xhigh", "approvalsReviewer": "user",
            "approvalPolicy": "on-request", "sandbox": {"type": "workspaceWrite", "networkAccess": False},
            "instructionSources": [str(workspace / "AGENTS.md")], "activePermissionProfile": {"id": "watcher-job"}}


def test_inherited_mode_does_not_override_sandbox_rules_or_approval_policy(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path)
    params = settings.thread_params()
    assert params == {"cwd": str(tmp_path), "model": "gpt-5.6-luna", "config": {"model_reasoning_effort": "xhigh"}}
    monkeypatch.setattr(settings_module.shutil, "which", lambda _: "/trusted/codex")
    assert settings.command() == ["/trusted/codex", "app-server", "--listen", "stdio://"]
    assert "threadId" not in params
    verified = settings.verify_thread(response(tmp_path))
    assert verified["instruction_sources"] == [str(tmp_path / "AGENTS.md")]
    assert verified["remote_mutation_isolation"] == "not_established"
    assert verified["action_authority"] == "not_granted_by_access_mode"


def test_restricted_mode_selects_explicit_profile_not_legacy_default(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, access="restricted", permission_profile="watcher-job")
    params = settings.thread_params("session-1")
    assert params["permissions"] == "watcher-job" and "sandbox" not in params
    assert params["approvalPolicy"] == "on-request" and params["approvalsReviewer"] == "user"
    assert params["threadId"] == "session-1"
    original = response(tmp_path)
    verified = settings.verify_thread(original, "session-1")
    original["sandbox"]["networkAccess"] = True
    assert verified["sandbox"]["networkAccess"] is False
    with pytest.raises(ReviewQueueError, match="restricted permission"):
        settings.verify_thread({**original, "activePermissionProfile": {"id": "other"}})


@pytest.mark.parametrize("enabled", [True, False])
def test_restricted_granular_policy_keeps_explicit_prompt_or_denial_categories(tmp_path: Path, enabled: bool) -> None:
    original = response(tmp_path)
    policy = {"sandbox_approval": enabled, "rules": enabled, "mcp_elicitations": enabled}
    original["approvalPolicy"] = {"granular": policy}
    verified = make_settings(tmp_path, access="restricted", permission_profile="watcher-job").verify_thread(original)
    assert verified["approval_policy"] == {"granular": policy}


@pytest.mark.parametrize("policy", [
    {}, {"sandbox_approval": True, "rules": True},
    {"sandbox_approval": 1, "rules": True, "mcp_elicitations": True},
    {"sandbox_approval": True, "rules": True, "mcp_elicitations": True, "request_permissions": "yes"},
    {"sandbox_approval": True, "rules": True, "mcp_elicitations": True, "unknown": True},
])
def test_restricted_granular_policy_rejects_unverified_fields(tmp_path: Path, policy: dict[str, Any]) -> None:
    original = response(tmp_path)
    original["approvalPolicy"] = {"granular": policy}
    with pytest.raises(ReviewQueueError):
        make_settings(tmp_path, access="restricted", permission_profile="watcher-job").verify_thread(original)


def test_full_mode_requires_acknowledgement_and_observed_full_access(tmp_path: Path) -> None:
    with pytest.raises(ReviewQueueError, match="acknowledgement"):
        make_settings(tmp_path, access="full")
    settings = make_settings(tmp_path, access="full", full_access_acknowledged=True)
    assert settings.thread_params()["sandbox"] == "danger-full-access"
    assert settings.thread_params()["approvalPolicy"] == "never"
    with pytest.raises(ReviewQueueError, match="full-access"):
        settings.verify_thread(response(tmp_path))
    effective = {**response(tmp_path), "approvalPolicy": "never", "sandbox": {"type": "dangerFullAccess"}}
    assert settings.verify_thread(effective)["filesystem_unrestricted"] is True
    assert settings.verify_thread(effective)["action_authority"] == "not_granted_by_access_mode"


@pytest.mark.parametrize("field,value", [
    ("model", "gpt-6.1-sol"), ("reasoningEffort", "high"), ("cwd", "/different"),
    ("approvalsReviewer", "auto_review"), ("modelProvider", ""), ("approvalPolicy", "unknown"),
    ("sandbox", {"type": "unknown"}), ("thread", {"id": "other-session"}),
    ("instructionSources", None), ("instructionSources", [42]),
])
def test_changed_or_unverified_effective_settings_stop_before_a_turn(tmp_path: Path, field: str, value: Any) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(ReviewQueueError, match="effective supervisor settings"):
        settings.verify_thread({**response(tmp_path), field: value}, session_id="session-1")


def test_named_cli_preset_is_refused_instead_of_using_another_account(tmp_path: Path) -> None:
    with pytest.raises(ReviewQueueError, match="named CLI config preset"):
        make_settings(tmp_path, config_profile="chatgpt")


@pytest.mark.parametrize("kwargs", [
    {"access": "restricted"}, {"access": "restricted", "permission_profile": "bad/name"},
    {"access": "inherit", "permission_profile": "watcher-job"},
    {"access": "inherit", "full_access_acknowledged": True}, {"timeout_seconds": 0},
    {"reasoning_effort": "unknown"}, {"access": "unknown"},
])
def test_conflicting_or_invalid_access_settings_are_not_silently_adjusted(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ReviewQueueError):
        make_settings(tmp_path, **kwargs)


@pytest.mark.parametrize("sources", [[], ["/foreign/AGENTS.md"]])
def test_missing_or_unexpected_instruction_chain_is_not_verified(tmp_path: Path, sources: list[str]) -> None:
    with pytest.raises(ReviewQueueError, match="expected trusted instruction"):
        make_settings(tmp_path).verify_thread({**response(tmp_path), "instructionSources": sources})


def test_explicitly_empty_expected_chain_is_valid_only_with_empty_discovery(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, expected_instruction_sources=())
    receipt = settings.verify_thread({**response(tmp_path), "instructionSources": []})
    assert receipt["instruction_sources"] == []
    assert receipt["command_rules_verification"] == "not_established"


def test_settings_receipt_does_not_copy_unknown_nested_configuration(tmp_path: Path) -> None:
    original = response(tmp_path)
    original["approvalPolicy"] = {"granular": {"sandbox_approval": True, "rules": True,
        "mcp_elicitations": False, "request_permissions": False, "skill_approval": True,
        "unknown": {"credential": "PRIVATE_CONFIG_MARKER"}}}
    original["sandbox"]["environment"] = {"token": "PRIVATE_CONFIG_MARKER"}
    original["activePermissionProfile"]["configuration"] = {"token": "PRIVATE_CONFIG_MARKER"}
    receipt = make_settings(tmp_path).verify_thread(original)
    assert receipt["approval_policy"] == {"granular": {"sandbox_approval": True, "rules": True,
        "mcp_elicitations": False, "request_permissions": False, "skill_approval": True}}
    assert receipt["sandbox"] == {"type": "workspaceWrite", "networkAccess": False}
    assert receipt["active_permission_profile"] == {"id": "watcher-job"}
    assert "PRIVATE_CONFIG_MARKER" not in json.dumps(receipt)


def test_explicit_local_execution_environment_is_checked_not_copied(tmp_path: Path) -> None:
    original = response(tmp_path)
    original["thread"]["environments"] = [{"environmentId": "local", "cwd": str(tmp_path),
                                         "runtimeWorkspaceRoots": [str(tmp_path), "/permitted"]}]
    receipt = make_settings(tmp_path).verify_thread(original)
    assert receipt["workspace"] == str(tmp_path)
    assert "environments" not in receipt and "/permitted" not in json.dumps(receipt)


@pytest.mark.parametrize("environments", [[], {}, [None],
    [{"environmentId": "remote1", "cwd": "/worktree", "runtimeWorkspaceRoots": []}],
    [{"environmentId": "local", "cwd": "/different", "runtimeWorkspaceRoots": []}]])
def test_unverified_execution_environment_stops_before_a_turn(tmp_path: Path, environments: Any) -> None:
    original = response(tmp_path)
    original["thread"]["environments"] = environments
    with pytest.raises(ReviewQueueError, match="execution environment"):
        make_settings(tmp_path).verify_thread(original)
