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
