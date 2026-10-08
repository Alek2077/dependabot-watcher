from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

import pytest

from dependabot_watcher.config import ConfigError, default_config_path, load_config, parse_config


def valid_config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "repositories": [
            {
                "name": "example-org/example-app",
                "default_branch": "main",
                "path_policy": {
                    "additional_dependency_patterns": ["deps/*.lock"],
                    "additional_sensitive_patterns": ["ops/**"],
                },
            }
        ],
    }


def configured_supervisor(**changes: Any) -> dict[str, Any]:
    return {"mode": "approval_first", "provider": "codex_app_server", "model": "fixture-model",
            "reasoning_effort": "low", "expected_account_type": "chatgpt", "access": "inherit",
            "model_use_acknowledged": True, "workspaces": [{"repository": "example-org/example-app",
                "directory": str(Path(tempfile.gettempdir()) / "watcher-fixture-workspace"),
                "instruction_sources": [str(Path(tempfile.gettempdir()) / "trusted-fixture" / "AGENTS.md")]}], **changes}


def test_supervisor_defaults_off_without_changing_advice_or_merge_policy() -> None:
    config = parse_config(valid_config())
    assert config.supervisor.mode == "off" and config.supervisor.workspaces == ()
    assert config.supervisor.model is None


def test_validation_preparation_limits_are_explicit_and_not_merge_authority() -> None:
    data = valid_config()
    data['supervisor'] = configured_supervisor(validation_authorized=True, validation={
        'preparation_authorized': True, 'network_authorized': True, 'maximum_bytes': 123456})
    config = parse_config(data)
    assert config.supervisor.validation.preparation_authorized
    assert config.supervisor.validation.network_authorized
    assert config.supervisor.validation.maximum_bytes == 123456
    assert not config.operation.mutation_enabled


@pytest.mark.parametrize('validation', [
    {'preparation_authorized': True}, {'network_authorized': True},
    {'maximum_bytes': 0}, {'maximum_bytes': True}, {'minimum_free_bytes': -1},
    {'command_timeout_seconds': 3601}, {'network_authorized': 'yes'}, {'unknown': True},
])
def test_invalid_preparation_authority_and_limits_are_refused(validation: dict[str, object]) -> None:
    data = valid_config()
    data['supervisor'] = configured_supervisor(validation=validation)
    with pytest.raises(ConfigError):
        parse_config(data)


@pytest.mark.parametrize("access", ["inherit", "restricted", "full"])
def test_explicit_supervisor_access_and_privacy_are_saved_separately_from_mutation(access: str) -> None:
    data = valid_config()
    options: dict[str, Any] = {"access": access}
    if access == "restricted":
        options["permission_profile"] = "fixture-restrictions"
    if access == "full":
        options["full_access_acknowledged"] = True
    data["supervisor"] = configured_supervisor(**options)
    config = parse_config(data)
    assert config.supervisor.access == access
    assert config.supervisor.model_use_acknowledged is True
    assert config.supervisor.max_jobs_per_run == 1 and config.supervisor.timeout_seconds == 900
    assert config.supervisor.workspaces[0].instruction_sources == (
        str(Path(tempfile.gettempdir()) / "trusted-fixture" / "AGENTS.md"),)
    assert config.advisory.mode == "off" and config.operation.mutation_enabled is False


@pytest.mark.parametrize("changes", [
    {"mode": "autonomous"}, {"provider": "other"}, {"model": None}, {"model": "bad\nmodel"},
    {"reasoning_effort": []}, {"reasoning_effort": "unknown"}, {"expected_account_type": {}},
    {"access": []}, {"access": "full"}, {"full_access_acknowledged": True},
    {"access": "restricted"}, {"permission_profile": "unexpected"}, {"model_use_acknowledged": False},
    {"model_use_acknowledged": 1}, {"validation_authorized": "yes"}, {"timeout_seconds": 3601},
    {"max_jobs_per_run": 0}, {"max_jobs_per_run": True}, {"max_jobs_per_run": 21},
    {"config_profile": "cannot-preserve"}, {"workspaces": []},
])
def test_supervisor_cannot_silently_select_missing_or_broader_authority(changes: dict[str, Any]) -> None:
    data = valid_config()
    data["supervisor"] = configured_supervisor(**changes)
    with pytest.raises(ConfigError):
        parse_config(data)


@pytest.mark.parametrize("workspace", [
    {"repository": "other/repository", "directory": "/owned", "instruction_sources": []},
    {"repository": "example-org/example-app", "directory": "relative", "instruction_sources": []},
    {"repository": "example-org/example-app", "directory": "/owned/../outside", "instruction_sources": []},
    {"repository": "example-org/example-app", "directory": "/owned\n", "instruction_sources": []},
    {"repository": "example-org/example-app", "directory": "/owned"},
    {"repository": "example-org/example-app", "directory": "/owned", "instruction_sources": "wrong"},
    {"repository": "example-org/example-app", "directory": "/owned", "instruction_sources": ["relative"]},
    {"repository": "example-org/example-app", "directory": "/owned", "instruction_sources": ["/same", "/same"]},
])
def test_supervisor_workspace_scope_and_instruction_chain_must_be_explicit(workspace: dict[str, Any]) -> None:
    data = valid_config()
    data["supervisor"] = configured_supervisor(workspaces=[workspace])
    with pytest.raises(ConfigError):
        parse_config(data)


def test_supervisor_does_not_read_or_modify_registered_workspace_when_parsing(tmp_path: Path) -> None:
    data = valid_config()
    absent = tmp_path / "not-created"
    data["supervisor"] = configured_supervisor(workspaces=[{"repository": "example-org/example-app",
        "directory": str(absent), "instruction_sources": []}])
    parsed = parse_config(data)
    assert parsed.supervisor.workspaces[0].directory == str(absent)
    assert not absent.exists()  # Actual launch must check identity/ownership/harness.


@pytest.mark.parametrize("value", [True, False, "true", 1, None])
def test_workspace_refresh_requires_explicit_boolean_permission(value: object) -> None:
    data = valid_config()
    policy = configured_supervisor()
    policy["workspaces"][0]["refresh_authorized"] = value
    data["supervisor"] = policy
    if type(value) is bool:
        assert parse_config(data).supervisor.workspaces[0].refresh_authorized is value
    else:
        with pytest.raises(ConfigError, match="must be a boolean"):
            parse_config(data)


def test_supervisor_repository_binding_is_canonical_and_workspaces_do_not_alias() -> None:
    data = valid_config()
    data["repositories"] = [{"name": "example-org/example-app"}, {"name": "example-org/other-app"}]
    first = str(Path(tempfile.gettempdir()) / "watcher-fixture-first")
    second = str(Path(tempfile.gettempdir()) / "watcher-fixture-second")
    entries = [{"repository": "EXAMPLE-ORG/EXAMPLE-APP", "directory": first, "instruction_sources": []},
               {"repository": "example-org/other-app", "directory": second, "instruction_sources": []}]
    data["supervisor"] = configured_supervisor(workspaces=entries)
    assert parse_config(data).supervisor.workspaces[0].repository == "example-org/example-app"
    entries[1]["directory"] = first
    with pytest.raises(ConfigError, match="own registered workspace"):
        parse_config(data)


def test_disabled_supervisor_does_not_ignore_active_settings() -> None:
    data = valid_config()
    data["supervisor"] = configured_supervisor(mode="off")
    with pytest.raises(ConfigError, match="disabled supervisor"):
        parse_config(data)


def test_parse_config_accepts_generic_repository() -> None:
    config = parse_config(valid_config())

    assert config.schema_version == 1
    assert config.repositories[0].name == "example-org/example-app"
    assert config.repositories[0].path_policy.sensitive_patterns == ("ops/**",)
    assert config.collection.max_open_prs == 100
    assert config.collection.max_open_alerts == 500
    assert config.collection.max_history_items == 200
    assert config.collection.max_files_per_pull_request == 500
    assert config.advisory.mode == "off"
    assert config.advisory.provider is None
    assert config.advisory.budget.max_candidates == 0
    assert config.operation.require_advice_before_execution is False


def test_unknown_key_is_rejected() -> None:
    data = valid_config()
    data["notification_email"] = "person@example.test"

    with pytest.raises(ConfigError, match="Unknown top-level key"):
        parse_config(data)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("collection", [], "collection must be a TOML table"),
        ("path_policy", False, "path_policy must be a TOML table"),
    ],
)
def test_falsey_non_table_config_is_rejected(key: str, value: object, message: str) -> None:
    data = valid_config()
    data[key] = value

    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_duplicate_repository_is_rejected_case_insensitively() -> None:
    data = valid_config()
    data["repositories"] = [
        {"name": "Example-Org/Example-App"},
        {"name": "example-org/example-app"},
    ]

    with pytest.raises(ConfigError, match="duplicate"):
        parse_config(data)


@pytest.mark.parametrize("pattern", ["", "/absolute/**", "../private/**", "safe/../../private"])
def test_unsafe_or_empty_path_pattern_is_rejected(pattern: str) -> None:
    data = valid_config()
    data["path_policy"] = {"additional_sensitive_patterns": [pattern]}

    with pytest.raises(ConfigError, match="invalid repository-relative pattern"):
        parse_config(data)


def test_load_config_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "schema_version = 1\n"
        "[[repositories]]\n"
        'name = "example-org/example-app"\n'
        'default_branch = "develop"\n',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.repositories[0].default_branch == "develop"


def test_load_config_rejects_replaceable_parent_and_symlink(
    tmp_path: Path,
) -> None:
    unsafe_parent = tmp_path / "shared"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    unsafe = unsafe_parent / "config.toml"
    unsafe.write_text(
        'schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="another principal"):
        load_config(unsafe)

    safe = tmp_path / "safe.toml"
    safe.write_text(unsafe.read_text(encoding="utf-8"), encoding="utf-8")
    linked = tmp_path / "linked.toml"
    linked.symlink_to(safe)

    with pytest.raises(ConfigError, match="trusted regular file"):
        load_config(linked)


def test_default_config_path_honors_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPENDABOT_WATCHER_CONFIG", "/tmp/example-config.toml")

    assert default_config_path() == Path("/tmp/example-config.toml")


def test_economy_advisory_uses_bounded_preset() -> None:
    data = valid_config()
    data["advisory"] = {
        "mode": "economy",
        "provider": "codex_cli",
        "codex_cli": {"triage": {"model": "example-model", "reasoning_effort": "high"}},
    }

    config = parse_config(data)

    assert config.advisory.mode == "economy"
    assert config.advisory.budget.max_candidates == 8
    assert config.advisory.budget.batch_size == 4
    assert config.advisory.budget.max_triage_calls == 2
    assert config.advisory.codex_cli is not None
    assert config.advisory.codex_cli.triage is not None
    assert config.advisory.codex_cli.triage.model == "example-model"


def test_reviewed_advisory_requires_reviewer_configuration() -> None:
    data = valid_config()
    data["advisory"] = {
        "mode": "reviewed",
        "provider": "codex_cli",
        "codex_cli": {
            "triage": {"model": "triage-model", "reasoning_effort": "high"}
        },
    }

    with pytest.raises(ConfigError, match="requires advisory.codex_cli.review"):
        parse_config(data)


def test_advisory_rejects_unsupported_provider() -> None:
    data = valid_config()
    data["advisory"] = {"mode": "off", "provider": "imaginary"}

    with pytest.raises(ConfigError, match="supports only 'codex_cli'"):
        parse_config(data)


def test_advisory_budget_rejects_unusable_enabled_values() -> None:
    data = valid_config()
    data["advisory"] = {
        "mode": "economy",
        "provider": "codex_cli",
        "budget": {"max_candidates": 0},
        "codex_cli": {
            "triage": {"model": "triage-model", "reasoning_effort": "high"}
        },
    }

    with pytest.raises(ConfigError, match="positive candidate"):
        parse_config(data)


def test_enabled_codex_advice_requires_explicit_model_and_reasoning() -> None:
    data = valid_config()
    data["advisory"] = {
        "mode": "economy",
        "provider": "codex_cli",
        "codex_cli": {"triage": {"model": "example-model"}},
    }

    with pytest.raises(ConfigError, match="explicit model and reasoning_effort"):
        parse_config(data)


def test_codex_executable_cannot_be_redirected_by_configuration() -> None:
    data = valid_config()
    data["advisory"] = {
        "mode": "off",
        "codex_cli": {"executable": "touch"},
    }

    with pytest.raises(ConfigError, match="fixed to 'codex'"):
        parse_config(data)


def test_repository_workflow_profile_and_manifest_limits_are_typed() -> None:
    config = parse_config(
        {
            "schema_version": 1,
            "collection": {
                "max_manifest_files": 20,
                "max_manifest_file_bytes": 65536,
                "max_manifest_depth": 4,
            },
            "repositories": [
                {
                    "name": "example-org/example-app",
                    "workflow": {
                        "merge_method": "squash",
                        "promotion_branch": "production",
                        "required_checks": ["CI"],
                        "deployment_environments": ["staging"],
                        "validation_notes": ["Verify staging deployment."],
                    },
                }
            ],
        }
    )

    assert config.collection.max_manifest_files == 20
    assert config.collection.max_manifest_file_bytes == 65536
    assert config.collection.max_manifest_depth == 4
    assert config.repositories[0].workflow.merge_method == "squash"
    assert config.repositories[0].workflow.promotion_branch == "production"
    assert config.repositories[0].workflow.required_checks == ("CI",)


def test_repository_workflow_rejects_required_check_boundary_whitespace() -> None:
    data = valid_config()
    repositories = data["repositories"]
    assert isinstance(repositories, list)
    assert isinstance(repositories[0], dict)
    repositories[0]["workflow"] = {"required_checks": [" CI"]}

    with pytest.raises(ConfigError, match="single-line strings"):
        parse_config(data)


@pytest.mark.parametrize("merge_method", ["octopus", "fast-forward", ""])
def test_repository_workflow_profile_rejects_unknown_merge_method(merge_method: str) -> None:
    with pytest.raises(ConfigError, match="merge_method"):
        parse_config(
            {
                "schema_version": 1,
                "repositories": [
                    {
                        "name": "example-org/example-app",
                        "workflow": {"merge_method": merge_method},
                    }
                ],
            }
        )


def test_repository_workflow_profile_rejects_multiline_or_unsafe_values() -> None:
    with pytest.raises(ConfigError, match="single-line"):
        parse_config(
            {
                "schema_version": 1,
                "repositories": [
                    {
                        "name": "example-org/example-app",
                        "workflow": {"validation_notes": ["first line\nsecond line"]},
                    }
                ],
            }
        )

    with pytest.raises(ConfigError, match="must not contain whitespace"):
        parse_config(
            {
                "schema_version": 1,
                "repositories": [
                    {
                        "name": "example-org/example-app",
                        "workflow": {"promotion_branch": "release branch"},
                    }
                ],
            }
        )


def test_operational_profile_is_typed_and_non_mutating() -> None:
    data = valid_config()
    data["operation"] = {
        "autonomy": "read_only",
        "mutation_enabled": False,
        "report_verbosity": "detailed",
        "machine_output": "json",
    }
    data["profile"] = {
        "source": "setup_advanced",
        "created_at": "2026-09-22T12:00:00Z",
        "confirmed_at": "2026-09-22T12:00:00+00:00",
    }

    config = parse_config(data)

    assert config.operation.report_verbosity == "detailed"
    assert config.operation.mutation_enabled is False
    assert config.profile.source == "setup_advanced"


def test_operational_profile_accepts_only_approval_gated_mutation_pair() -> None:
    data = valid_config()
    data["operation"] = {
        "autonomy": "approval_required",
        "mutation_enabled": True,
    }

    config = parse_config(data)

    assert config.operation.autonomy == "approval_required"
    assert config.operation.mutation_enabled is True


def test_execution_advice_requirement_requires_enabled_advisory_mode() -> None:
    data = valid_config()
    data["operation"] = {"require_advice_before_execution": True}

    with pytest.raises(ConfigError, match="requires an enabled advisory mode"):
        parse_config(data)

    data["advisory"] = {
        "mode": "economy",
        "provider": "codex_cli",
        "codex_cli": {
            "triage": {"model": "triage-model", "reasoning_effort": "high"}
        },
    }

    config = parse_config(data)

    assert config.operation.require_advice_before_execution is True


@pytest.mark.parametrize(
    "operation",
    [
        {"autonomy": "automatic"},
        {"mutation_enabled": True},
        {"autonomy": "approval_required", "mutation_enabled": False},
        {"report_verbosity": "noisy"},
        {"machine_output": "pickle"},
    ],
)
def test_operational_profile_rejects_unsupported_authority(operation: object) -> None:
    data = valid_config()
    data["operation"] = operation

    with pytest.raises(ConfigError, match="operation"):
        parse_config(data)


def test_profile_timestamps_require_timezone() -> None:
    data = valid_config()
    data["profile"] = {"created_at": "2026-09-22T12:00:00"}

    with pytest.raises(ConfigError, match="timezone"):
        parse_config(data)
