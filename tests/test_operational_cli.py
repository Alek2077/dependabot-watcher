from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from dependabot_watcher import cli
from dependabot_watcher.config import ConfigError, SupervisorConfig, load_config
from dependabot_watcher.operational_output import render_operational


class _TtyBuffer(StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("notify", ["off", "desktop"])
def test_cli_run_submits_configured_supervisor_without_premature_desktop_notice(
    tmp_path: Path, monkeypatch, capsys, notify: str,
) -> None:
    policy = SupervisorConfig(mode="approval_first", model="gpt-5.6-luna", reasoning_effort="xhigh")
    config = SimpleNamespace(supervisor=policy, collection=SimpleNamespace(command_timeout_seconds=120),
        repositories=(SimpleNamespace(name="example-org/example-app"),))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())
    observed = {}
    run_id = "run_" + "a" * 20

    def reconcile(*args, **kwargs):
        observed["collection_mode"] = kwargs["notification_mode"]
        return {"status": "complete", "run_id": run_id, "notification": {"needed": True},
            "supervisor": {"mode": policy.mode, "model": policy.model, "reasoning_effort": policy.reasoning_effort}}

    def submit(config_path, state_path, actual_run, **kwargs):
        observed["run_id"] = actual_run
        observed["notification_mode"] = kwargs["notification_mode"]
        return {"status": "submitted", "assurance": "process_submission_only", "model_calls": "not_observed"}

    monkeypatch.setattr(cli, "reconcile", reconcile)
    monkeypatch.setattr(cli, "submit_scheduled_supervisor", submit)
    monkeypatch.setattr(cli, "deliver_desktop_notification", lambda *args, **kwargs: pytest.fail("Premature notice before agent reasoning"))
    assert cli.main(["run", "--config", str(tmp_path / "config.toml"), "--state-dir", str(tmp_path / "state"),
        "--notify", notify, "--format", "json"]) == cli.ExitCode.SUCCESS
    assert observed == {"collection_mode": "off", "run_id": run_id, "notification_mode": notify}
    assert json.loads(capsys.readouterr().out)["supervisor_dispatch"]["model_calls"] == "not_observed"


def test_cli_help_explains_doctor_profile_scope() -> None:
    help_text = " ".join(cli.build_parser().format_help().split())
    assert (
        "Run a read-only preflight for the environment and every configured repository."
        in help_text
    )
    assert "Run one durable non-mutating reconciliation for scheduling." in help_text


def test_cli_run_quiet_suppresses_all_private_report_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config = SimpleNamespace(
        supervisor=SupervisorConfig(),
        collection=SimpleNamespace(command_timeout_seconds=120),
        repositories=(SimpleNamespace(name="example-org/example-app"),),
    )
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())
    observed: dict[str, object] = {}

    def reconcile(*args, **kwargs):
        observed.update(kwargs)
        return {
            "status": "complete",
            "outcome": "action_required",
            "notification": {"needed": True, "reason": "approval_ready"},
        }

    monkeypatch.setattr(cli, "reconcile", reconcile)

    exit_code = cli.main(
        [
            "run",
            "--config",
            str(tmp_path / "config.toml"),
            "--state-dir",
            str(tmp_path / "state"),
            "--expected-interval-minutes",
            "60",
            "--quiet",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert captured.out == ""
    assert captured.err == ""
    assert observed["expected_interval_minutes"] == 60


def test_cli_run_delivers_and_records_desktop_notification(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config = SimpleNamespace(
        supervisor=SupervisorConfig(),
        collection=SimpleNamespace(command_timeout_seconds=120),
        repositories=(SimpleNamespace(name="example-org/example-app"),),
    )
    report = {
        "status": "complete",
        "outcome": "action_required",
        "run_id": "run_example",
        "notification": {"needed": True, "reason": "approval_ready"},
    }
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())
    monkeypatch.setattr(cli, "reconcile", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        cli,
        "deliver_desktop_notification",
        lambda value, **kwargs: {"mode": "desktop", "status": "delivered"},
    )

    def record(state_directory, run_id, delivery, **kwargs):
        return {
            **report,
            "notification": {
                **report["notification"],
                "delivery": delivery,
            },
        }

    monkeypatch.setattr(cli, "record_notification_delivery", record)

    exit_code = cli.main(
        [
            "run",
            "--config",
            str(tmp_path / "config.toml"),
            "--state-dir",
            str(tmp_path / "state"),
            "--notify",
            "desktop",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert json.loads(captured.out)["notification"]["delivery"]["status"] == "delivered"
    assert captured.err == ""


def test_cli_run_notification_failure_is_actionable_and_nonzero(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config = SimpleNamespace(
        supervisor=SupervisorConfig(),
        collection=SimpleNamespace(command_timeout_seconds=120),
        repositories=(SimpleNamespace(name="example-org/example-app"),),
    )
    report = {
        "status": "complete",
        "outcome": "action_required",
        "run_id": "run_example",
        "notification": {"needed": True, "reason": "approval_ready"},
    }
    failed = {"mode": "desktop", "status": "failed"}
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())
    monkeypatch.setattr(cli, "reconcile", lambda *args, **kwargs: report)
    monkeypatch.setattr(cli, "deliver_desktop_notification", lambda value, **kwargs: failed)
    monkeypatch.setattr(
        cli,
        "record_notification_delivery",
        lambda *args, **kwargs: {
            **report,
            "notification": {**report["notification"], "delivery": failed},
        },
    )

    exit_code = cli.main(
        [
            "run",
            "--config",
            str(tmp_path / "config.toml"),
            "--state-dir",
            str(tmp_path / "state"),
            "--notify",
            "desktop",
            "--quiet",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.NOTIFICATION_ERROR
    assert captured.out == ""
    assert "durable run report" in captured.err


def test_cli_schedule_preview_routes_without_local_scheduler_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda path: object())
    monkeypatch.setattr(
        cli,
        "recurring_profile_summary",
        lambda config: {
            "repository_count": 1,
            "advisory": {"mode": "off"},
            "operation": {"mutation_enabled": False},
        },
    )

    def preview(action, **kwargs):
        observed.update(action=action, **kwargs)
        return {
            "schema_version": 1,
            "mode": "schedule",
            "action": "preview",
            "status": "preview",
            "changed": False,
            "schedule": {
                "platform": "wsl",
                "job_id": "dependabot-watcher-test",
                "interval_minutes": 1440,
                "run_argv": ["dependabot-watcher", "run"],
                "config_path": str(tmp_path / "config.toml"),
                "state_directory": str(tmp_path / "state"),
            },
            "native_status": {},
            "missed_run_policy": "one current-state catch-up",
            "overlap_policy": "runtime lock",
            "next": None,
        }

    monkeypatch.setattr(cli, "manage_schedule", preview)

    exit_code = cli.main(
        [
            "schedule",
            "preview",
            "--config",
            str(tmp_path / "config.toml"),
            "--state-dir",
            str(tmp_path / "state"),
            "--platform",
            "wsl",
            "--distro",
            "Ubuntu",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert json.loads(captured.out)["status"] == "preview"
    assert json.loads(captured.out)["profile"]["advisory"]["mode"] == "off"
    assert observed["action"] == "preview"
    assert observed["requested_platform"] == "wsl"
    assert observed["distro"] == "Ubuntu"
    assert Path(str(observed["executable"])).name == "dependabot-watcher"
    assert Path(str(observed["executable"])).is_absolute()


def test_cli_schedule_inventory_bypasses_profile_and_executable_resolution(
    monkeypatch,
    capsys,
) -> None:
    observed: dict[str, object] = {}

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Inventory must not load a profile or resolve its executable")

    def inventory(**kwargs):
        observed.update(kwargs)
        return {
            "schema_version": 1,
            "mode": "schedule",
            "action": "inventory",
            "status": "complete",
            "changed": False,
            "mutation_authorized": False,
            "capabilities": {
                "model_calls": False,
                "persistent_local_writes": False,
                "github_mutations": False,
                "notifications": False,
            },
            "platform": "systemd",
            "registration_count": 0,
            "registrations": [],
            "untrusted_candidates": [],
            "next": "No recovery action is needed.",
        }

    monkeypatch.setattr(cli, "load_config", unexpected)
    monkeypatch.setattr(cli, "_current_console_executable", unexpected)
    monkeypatch.setattr(cli, "inventory_schedules", inventory)

    exit_code = cli.main(
        ["schedule", "inventory", "--platform", "systemd", "--format", "json"]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert json.loads(captured.out)["action"] == "inventory"
    assert observed == {"requested_platform": "systemd"}
    assert captured.err == ""


def test_cli_schedule_binds_the_invoked_console_script(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = tmp_path / "dependabot-watcher"
    executable.write_text("placeholder", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(cli.sys, "argv", [str(executable)])

    assert cli._current_console_executable() == str(executable.resolve())


def test_cli_schedule_normalizes_wsl_style_native_windows_invocation() -> None:
    assert cli._normalized_console_invocation(
        "/mnt/c/Users/Alek/AppData/Local/Programs/dependabot-watcher.exe",
        windows=True,
    ) == r"C:\Users\Alek\AppData\Local\Programs\dependabot-watcher.exe"


def test_cli_schedule_uses_console_sibling_when_launcher_argv_is_opaque(
    tmp_path: Path,
    monkeypatch,
) -> None:
    python = tmp_path / "bin/python"
    console = tmp_path / "bin/dependabot-watcher"
    python.parent.mkdir(parents=True)
    python.write_text("placeholder", encoding="utf-8")
    console.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(cli.sys, "argv", ["opaque-launcher"])
    monkeypatch.setattr(cli.sys, "executable", str(python))

    assert cli._current_console_executable() == str(console.resolve())


def test_cli_offline_setup_creates_safe_profile_without_external_calls(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "config.toml"

    def unexpected_doctor(*args, **kwargs):
        raise AssertionError("Setup must not route through doctor")

    monkeypatch.setattr(cli, "doctor", unexpected_doctor)

    exit_code = cli.main(
        [
            "setup",
            "--config",
            str(path),
            "--repo",
            "example-org/example-app",
            "--branch",
            "example-org/example-app=develop",
            "--offline",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    config = load_config(path)
    assert exit_code == cli.ExitCode.SUCCESS
    assert captured.err == ""
    assert report["mode"] == "setup"
    assert report["capabilities"]["model_calls"] is False
    assert report["capabilities"]["github_mutations"] is False
    assert report["capabilities"]["persistent_local_writes"] is True
    assert config.repositories[0].default_branch == "develop"
    assert config.operation.mutation_enabled is False
    assert "Verification: offline; GitHub access was not verified" in render_operational(
        report, "text"
    )
    assert "Verification: offline; GitHub access was not verified" in render_operational(
        report, "markdown"
    )


@pytest.mark.parametrize("refresh", [False, True])
def test_cli_supervisor_setup_preserves_explicit_context_and_truthful_disclosure(tmp_path: Path, capsys, refresh: bool) -> None:
    path = tmp_path / "config.toml"
    workspace = str(tmp_path / "work=tree")
    instruction = str(tmp_path / "trusted instructions" / "AGENTS.md")
    code = cli.main(["setup", "--config", str(path), "--repo", "example-org/example-app",
        "--offline", "--format", "json", "--supervisor-mode", "approval_first",
        "--supervisor-model", "chosen-model", "--supervisor-reasoning", "low",
        "--supervisor-account", "chatgpt", "--supervisor-access", "inherit",
        "--acknowledge-supervisor-use", "--supervisor-workspace", f"example-org/example-app={workspace}",
        "--supervisor-instruction", f"EXAMPLE-ORG/EXAMPLE-APP={instruction}",
        *(["--supervisor-refresh-workspace", "EXAMPLE-ORG/EXAMPLE-APP"] if refresh else [])])
    captured = capsys.readouterr()
    assert code == cli.ExitCode.SUCCESS
    assert captured.err == ""
    report = json.loads(captured.out)
    saved = load_config(path)
    assert saved.supervisor.workspaces[0].directory == workspace
    assert saved.supervisor.workspaces[0].instruction_sources == (instruction,)
    assert saved.supervisor.workspaces[0].refresh_authorized is refresh
    assert saved.operation.mutation_enabled is False
    for output in ("text", "markdown"):
        rendered = render_operational(report, output)
        assert "chosen-model" in rendered
        assert "Agent access: inherit" in rendered
        assert "subscription/API usage" in rendered
        assert "No agent or merge was launched" in rendered
        assert "AI: off" not in rendered
    assert not Path(workspace).exists()


@pytest.mark.parametrize("extra", [
    ["--supervisor-model", "chosen-model"],
    ["--supervisor-timeout-seconds", "0"],
    ["--supervisor-mode", "approval_first", "--supervisor-workspace", "invalid"],
    ["--supervisor-mode", "approval_first", "--supervisor-access", "inherit",
     "--supervisor-instruction", "example-org/example-app=/instructions"],
    ["--supervisor-mode", "approval_first", "--supervisor-access", "inherit",
     "--supervisor-workspace", "example-org/example-app=/one",
     "--supervisor-workspace", "EXAMPLE-ORG/EXAMPLE-APP=/two"],
])
def test_cli_supervisor_setup_refuses_ambiguous_or_inactive_choices(tmp_path: Path, capsys, extra: list[str]) -> None:
    path = tmp_path / "config.toml"
    code = cli.main(["setup", "--config", str(path), "--repo", "example-org/example-app",
                     "--offline", *extra])
    assert code == cli.ExitCode.CONFIGURATION_ERROR
    assert not path.exists()
    assert capsys.readouterr().err


def test_cli_ordinary_setup_accepts_case_alias_branch_and_preserves_repository(tmp_path: Path, capsys) -> None:
    path = tmp_path / "config.toml"
    code = cli.main(["setup", "--config", str(path), "--repo", "EXAMPLE-ORG/Example-App",
                    "--branch", "example-org/example-app=develop", "--offline", "--format", "json"])
    assert code == cli.ExitCode.SUCCESS
    report = json.loads(capsys.readouterr().out)
    assert report["repositories"][0] == {"name": "EXAMPLE-ORG/Example-App", "default_branch": "develop"}


@pytest.mark.parametrize("extra", [
    ["--triage-model", "ignored"], ["--acknowledge-model-use"],
    ["--advisory-mode", "economy", "--triage-model", "triage", "--triage-reasoning", "low",
     "--acknowledge-model-use", "--review-model", "ignored"],
])
def test_cli_refuses_advice_flags_that_would_be_discarded(tmp_path: Path, capsys, extra: list[str]) -> None:
    path = tmp_path / "config.toml"
    code = cli.main(["setup", "--config", str(path), "--repo", "example-org/example-app", "--offline", *extra])
    assert code == cli.ExitCode.CONFIGURATION_ERROR
    assert not path.exists()
    assert capsys.readouterr().err


@pytest.mark.parametrize("extra", [
    ["--acknowledge-mutation"],
    ["--merge-method", "example-org/example-app=squash"],
    ["--required-check", "example-org/example-app=CI"],
])
def test_cli_read_only_setup_refuses_discarded_workflow_choices(tmp_path: Path, capsys, extra: list[str]) -> None:
    path = tmp_path / "config.toml"
    code = cli.main(["setup", "--config", str(path), "--repo", "example-org/example-app", "--offline", *extra])
    assert code == cli.ExitCode.CONFIGURATION_ERROR
    assert not path.exists()
    assert "require autonomy approval_required" in capsys.readouterr().err


def test_cli_execute_without_token_runs_read_only_preflight(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    plan_path = tmp_path / "plan.json"
    config_path.write_text("placeholder", encoding="utf-8")
    plan_path.write_text("{}", encoding="utf-8")
    config = SimpleNamespace(collection=SimpleNamespace(command_timeout_seconds=120))
    saved_plan = {"saved": True}
    observed: dict[str, object] = {}

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_plan", lambda path: saved_plan)
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())

    def preflight(
        config_arg,
        client_arg,
        plan_arg,
        step_id,
        *,
        state_directory,
    ):
        observed.update(
            config=config_arg,
            plan=plan_arg,
            step_id=step_id,
            state_directory=state_directory,
        )
        return {
            "mode": "execute_preflight",
            "status": "ready",
            "binding": {
                "repository": "example-org/example-app",
                "pull_request": 42,
                "head_sha": "a" * 40,
                "merge_method": "squash",
            },
            "action_id": "action_test",
            "mutation_performed": False,
            "approval_token": "approve_test",
            "next": "Approve exactly this action.",
        }

    monkeypatch.setattr(cli, "preflight_execution", preflight)

    exit_code = cli.main(
        [
            "execute",
            "--config",
            str(config_path),
            "--plan-file",
            str(plan_path),
            "--step-id",
            "step_exact",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert observed == {
        "config": config,
        "plan": saved_plan,
        "step_id": "step_exact",
        "state_directory": cli.default_state_directory(),
    }
    assert "Mutation performed: no" in captured.out
    assert "approve_test" in captured.out


def test_cli_execute_already_applied_shows_next_step(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    plan_path = tmp_path / "plan.json"
    config_path.write_text("placeholder", encoding="utf-8")
    plan_path.write_text("{}", encoding="utf-8")
    config = SimpleNamespace(collection=SimpleNamespace(command_timeout_seconds=120))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_plan", lambda path: {"saved": True})
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())
    monkeypatch.setattr(
        cli,
        "preflight_execution",
        lambda *args, **kwargs: {
            "mode": "execute_preflight",
            "status": "already_applied",
            "binding": {
                "repository": "example-org/example-app",
                "pull_request": 42,
                "head_sha": "a" * 40,
                "merge_method": "squash",
            },
            "action_id": "action_test",
            "mutation_performed": False,
            "approval_token": None,
            "next": "No action is needed; GitHub reports this exact head as already merged.",
        },
    )

    exit_code = cli.main(
        [
            "execute",
            "--config",
            str(config_path),
            "--plan-file",
            str(plan_path),
            "--step-id",
            "step_exact",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert "Status: already_applied" in captured.out
    assert "Next: No action is needed" in captured.out


def test_cli_execute_with_token_routes_approved_execution(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    plan_path = tmp_path / "plan.json"
    config_path.write_text("placeholder", encoding="utf-8")
    plan_path.write_text("{}", encoding="utf-8")
    config = SimpleNamespace(collection=SimpleNamespace(command_timeout_seconds=120))
    observed: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_plan", lambda path: {"saved": True})
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())

    def execute(
        config_arg,
        client_arg,
        plan_arg,
        step_id,
        token,
        *,
        state_directory,
        config_path,
    ):
        observed.update(
            step_id=step_id,
            token=token,
            state_directory=state_directory,
            config_path=config_path,
        )
        return {
            "schema_version": 1,
            "mode": "execute",
            "status": "merged",
            "mutation_performed": True,
            "mutation_authorized": True,
            "binding": {
                "repository": "example-org/example-app",
                "pull_request": 42,
                "head_sha": "a" * 40,
                "merge_method": "squash",
            },
            "action_id": "action_test",
        }

    monkeypatch.setattr(cli, "execute_approved", execute)

    exit_code = cli.main(
        [
            "execute",
            "--config",
            str(config_path),
            "--plan-file",
            str(plan_path),
            "--step-id",
            "step_exact",
            "--approval-token",
            "approve_exact",
            "--state-dir",
            str(tmp_path / "custom-state"),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert observed == {
        "step_id": "step_exact",
        "token": "approve_exact",
        "state_directory": tmp_path / "custom-state",
        "config_path": config_path,
    }
    assert json.loads(captured.out)["status"] == "merged"


def test_cli_execute_reports_execution_error_with_specific_exit_code(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    plan_path = tmp_path / "plan.json"
    config_path.write_text("placeholder", encoding="utf-8")
    plan_path.write_text("{}", encoding="utf-8")
    config = SimpleNamespace(collection=SimpleNamespace(command_timeout_seconds=120))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_plan", lambda path: {"saved": True})
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())
    monkeypatch.setattr(
        cli,
        "preflight_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            cli.ExecutionError("stale exact action")
        ),
    )

    exit_code = cli.main(
        [
            "execute",
            "--config",
            str(config_path),
            "--plan-file",
            str(plan_path),
            "--step-id",
            "step_exact",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.EXECUTION_ERROR
    assert captured.out == ""
    assert "Execution error: stale exact action" in captured.err


@pytest.mark.parametrize(
    ("command", "exception", "expected_code", "expected_error"),
    [
        (
            ["run", "--format", "json"],
            cli.ReconciliationError("state is unavailable"),
            cli.ExitCode.RECONCILIATION_ERROR,
            "Reconciliation error: state is unavailable",
        ),
        (
            ["schedule", "status", "--format", "json"],
            cli.ScheduleError("native definition is invalid"),
            cli.ExitCode.SCHEDULE_ERROR,
            "Schedule error: native definition is invalid",
        ),
    ],
)
def test_cli_reports_new_operational_exit_codes(
    command,
    exception,
    expected_code,
    expected_error,
    monkeypatch,
    capsys,
) -> None:
    if command[0] == "run":
        config = SimpleNamespace(
            supervisor=SupervisorConfig(),
            collection=SimpleNamespace(command_timeout_seconds=120),
            repositories=(SimpleNamespace(name="example-org/example-app"),),
        )
        monkeypatch.setattr(cli, "load_config", lambda path: config)
        monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())
        monkeypatch.setattr(cli, "reconcile", lambda *args, **kwargs: (_ for _ in ()).throw(exception))
    else:
        monkeypatch.setattr(
            cli,
            "manage_schedule",
            lambda *args, **kwargs: (_ for _ in ()).throw(exception),
        )

    exit_code = cli.main(command)

    captured = capsys.readouterr()
    assert exit_code == expected_code
    assert captured.out == ""
    assert expected_error in captured.err


def test_cli_setup_can_create_explicit_approval_gated_profile(
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "config.toml"

    exit_code = cli.main(
        [
            "setup",
            "--config",
            str(path),
            "--repo",
            "example-org/example-app",
            "--branch",
            "example-org/example-app=main",
            "--autonomy",
            "approval_required",
            "--merge-method",
            "example-org/example-app=squash",
            "--required-check",
            "example-org/example-app=CI",
            "--acknowledge-mutation",
            "--offline",
            "--format",
            "json",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    config = load_config(path)
    assert exit_code == cli.ExitCode.SUCCESS
    assert report["operation"]["autonomy"] == "approval_required"
    assert config.operation.mutation_enabled is True
    assert config.repositories[0].workflow.merge_method == "squash"
    assert config.repositories[0].workflow.required_checks == ("CI",)


def test_cli_rejects_required_check_boundary_whitespace() -> None:
    with pytest.raises(ConfigError, match="OWNER/REPO=CHECK"):
        cli._required_check_overrides(["example-org/example-app= CI"])


def test_cli_doctor_uses_partial_exit_for_attention_report(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "missing.toml"
    monkeypatch.setattr(
        cli,
        "doctor",
        lambda config_path, offline: {
            "schema_version": 1,
            "mode": "doctor",
            "generated_at": "2026-09-22T12:00:00Z",
            "status": "attention",
            "offline": offline,
            "capabilities": {
                "model_calls": False,
                "persistent_local_writes": False,
                "github_mutations": False,
                "notifications": False,
                "scheduling": False,
            },
            "runtime": {
                "platform": "linux",
                "platform_release": "test",
                "python": "3.13.0",
                "python_supported": True,
            },
            "configuration": {
                "path": str(config_path),
                "status": "missing",
                "schema_version": None,
            },
            "state": {"persistent_runtime_state": "not_used", "path": None},
            "tools": {
                "github_cli": {
                    "availability": "available",
                    "authentication": "unknown",
                    "version": "test",
                },
                "codex_cli": {
                    "availability": "available",
                    "authentication": "unknown",
                    "version": "test",
                },
            },
            "deployment_adapters": {"schedulers": [], "notifications": []},
            "repositories": [],
            "findings": [
                {
                    "severity": "warning",
                    "code": "configuration_missing",
                    "message": "Configuration is missing.",
                }
            ],
        },
    )

    exit_code = cli.main(
        ["doctor", "--config", str(path), "--offline", "--format", "json"]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == cli.ExitCode.PARTIAL_INSPECTION
    assert report["status"] == "attention"


def test_doctor_human_outputs_include_actionable_error_detail() -> None:
    report = {
        "mode": "doctor",
        "status": "blocked",
        "configuration": {
            "status": "invalid",
            "path": "/tmp/config.toml",
            "error": "Unknown operation key: unsafe.",
        },
        "runtime": {"platform": "linux", "python": "3.13.0"},
        "tools": {
            "github_cli": {
                "availability": "available",
                "authentication": "available",
                "version": "test",
            },
            "codex_cli": {
                "availability": "available",
                "authentication": "not_required",
                "version": "test",
            },
        },
        "repositories": [],
        "findings": [
            {
                "severity": "error",
                "code": "configuration_invalid",
                "message": "The resolved configuration is invalid.",
                "next_step": "Correct the configuration.",
            }
        ],
    }

    text = render_operational(report, "text")
    markdown = render_operational(report, "markdown")

    assert "Unknown operation key: unsafe." in text
    assert "Next: Correct the configuration." in text
    assert "Unknown operation key: unsafe." in markdown
    assert "Next: Correct the configuration." in markdown
    assert "not checked (advice is off)" in text


def test_cli_setup_defaults_to_human_text_and_existing_path_fails_cleanly(
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "config.toml"
    arguments = [
        "setup",
        "--config",
        str(path),
        "--repo",
        "example-org/example-app",
        "--offline",
    ]

    assert cli.main(arguments) == cli.ExitCode.SUCCESS
    first = capsys.readouterr()
    assert "Dependabot Watcher setup" in first.out
    assert "Verification: offline" in first.out
    assert "example-org/example-app (main)" in first.out
    assert "AI: off (no model calls and no provider quota or cost)" in first.out
    assert "GitHub changes: disabled" in first.out
    assert "dependabot-watcher run" in first.out
    assert "Choose a schedule cadence" in first.out

    assert cli.main(arguments) == cli.ExitCode.CONFIGURATION_ERROR
    second = capsys.readouterr()
    assert second.out == ""
    assert "will not overwrite" in second.err


def test_setup_and_doctor_show_interactive_progress_without_affecting_reports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stdout = _TtyBuffer()
    stderr = _TtyBuffer()
    monkeypatch.setattr(cli.sys, "stdout", stdout)
    monkeypatch.setattr(cli.sys, "stderr", stderr)
    config_path = tmp_path / "config.toml"

    assert (
        cli.main(
            [
                "setup",
                "--config",
                str(config_path),
                "--repo",
                "example-org/example-app",
                "--offline",
            ]
        )
        == cli.ExitCode.SUCCESS
    )
    assert "Creating the local operational profile" in stderr.getvalue()
    assert "Dependabot Watcher setup" in stdout.getvalue()

    stdout.seek(0)
    stdout.truncate(0)
    stderr.seek(0)
    stderr.truncate(0)
    assert (
        cli.main(
            [
                "doctor",
                "--config",
                str(config_path),
                "--offline",
            ]
        )
        == cli.ExitCode.PARTIAL_INSPECTION
    )
    assert "Checking the local environment" in stderr.getvalue()
    assert "Dependabot Watcher doctor" in stdout.getvalue()
