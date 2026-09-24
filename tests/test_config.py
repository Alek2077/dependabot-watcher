from __future__ import annotations

from pathlib import Path

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
