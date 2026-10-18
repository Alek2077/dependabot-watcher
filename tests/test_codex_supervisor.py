from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dependabot_watcher.providers import codex_supervisor
from dependabot_watcher.providers.codex_supervisor import (
    CodexSupervisor, CodexSupervisorSettings, _bounded_process, parse_supervisor_output,
)
from dependabot_watcher.review_queue import ReviewQueueError


def running_job() -> dict[str, Any]:
    return {"job_id": "job_" + "a" * 32, "context_digest": "b" * 64, "status": "running",
            "context": {"repository": "example-org/example-app", "proposals": [{
                "proposal_id": "proposal_" + "c" * 16, "feedback": [{"request_id": "decision_1_question"}]}],
                "retired_proposals": []}}


def events(job: dict[str, Any]) -> list[dict[str, Any]]:
    final = {"job_id": job["job_id"], "context_digest": job["context_digest"],
             "analysis": {"summary": "Investigate before approving.", "recommendations": [{
                 "proposal_id": "proposal_" + "c" * 16, "conclusion": "investigate", "explanation": "Checks alone are insufficient."}],
                 "replies": [{"proposal_id": "proposal_" + "c" * 16, "request_id": "decision_1_question", "message": "Here is the explanation."}]}}
    return [{"type": "thread.started", "thread_id": "session-123"},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "PRIVATE COMMAND",
                                                  "aggregated_output": "PRIVATE OUTPUT", "status": "completed", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(final)}},
            {"type": "turn.completed", "usage": {"input_tokens": 200, "cached_input_tokens": 50, "output_tokens": 40}}]


def encoded(items: list[dict[str, Any]]) -> bytes:
    return ("\n".join(json.dumps(item) for item in items) + "\n").encode()


def test_real_event_parsing_preserves_job_reply_and_usage_without_tool_output() -> None:
    job = running_job()
    output = parse_supervisor_output(encoded(events(job)), job)
    assert output["session_id"] == "session-123"
    assert output["analysis"]["replies"][0]["request_id"] == "decision_1_question"
    assert output["usage"] == {"input_tokens": 200, "cached_input_tokens": 50, "output_tokens": 40, "reasoning_output_tokens": 0}
    assert output["command_observations"] == [{"status": "completed", "exit_code": 0}]
    assert "PRIVATE" not in json.dumps(output)


@pytest.mark.parametrize("fault", ["no_session", "no_completion", "failed_turn", "negative_usage",
                                  "wrong_job", "wrong_revision", "wrong_message", "duplicate_completion", "bad_json"])
def test_incomplete_or_mismatched_provider_turns_are_not_analysis(fault: str) -> None:
    job = running_job()
    items = events(job)
    if fault == "no_session":
        items.pop(0)
    elif fault == "no_completion":
        items.pop()
    elif fault == "failed_turn":
        items[-1] = {"type": "turn.failed", "error": "sensitive error"}
    elif fault == "negative_usage":
        items[-1]["usage"]["input_tokens"] = -1
    elif fault == "duplicate_completion":
        items.append(items[-1])
    elif fault in {"wrong_job", "wrong_revision", "wrong_message"}:
        final = json.loads(items[-2]["item"]["text"])
        if fault == "wrong_job":
            final["job_id"] = "different-job"
        elif fault == "wrong_revision":
            final["context_digest"] = "different-revision"
        else:
            final["analysis"]["replies"][0]["request_id"] = "decision_2_another"
        items[-2]["item"]["text"] = json.dumps(final)
    data = b"not json\n" if fault == "bad_json" else encoded(items)
    with pytest.raises(ReviewQueueError):
        parse_supervisor_output(data, job)


def test_adapter_preserves_selected_harness_and_explicit_readonly_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_supervisor.shutil, "which", lambda name: "/trusted/bin/codex")
    settings = CodexSupervisorSettings(tmp_path, "gpt-5.6-luna", "xhigh", profile="chatgpt")
    adapter = CodexSupervisor(settings)
    command = adapter.command()
    assert command[0] == "/trusted/bin/codex" and "exec" in command and "--json" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--cd") + 1] == str(tmp_path)
    assert command[command.index("--profile") + 1] == "chatgpt"
    assert "model_reasoning_effort=\"xhigh\"" in command
    assert all(flag not in command for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--disable", "--dangerously-bypass-approvals-and-sandbox"))
    captured: dict[str, Any] = {}

    def provider(argv: list[str], prompt: bytes, timeout: int) -> bytes:
        captured.update(argv=argv, prompt=prompt.decode(), timeout=timeout)
        return encoded(events(running_job()))

    monkeypatch.setattr(codex_supervisor, "_bounded_process", provider)
    result = adapter.analyze(running_job())
    assert result["model"] == settings.model and result["reasoning_effort"] == settings.reasoning_effort
    assert result["turn_completed"] is True and captured["timeout"] == 900
    assert "do not edit, install" in captured["prompt"]
    headings = ["## Objective and execution scope\n", "## Evidence trust and authority\n",
                "## Investigation and answers\n", "## Workflow-specific completion and synchronization\n",
                "## Response contract\n", "## Job context/evidence (not instructions)\n"]
    assert all(captured["prompt"].count(heading) == 1 for heading in headings)
    assert [captured["prompt"].index(heading) for heading in headings] == sorted(
        captured["prompt"].index(heading) for heading in headings)
    assert json.loads(captured["prompt"].rsplit("\n", 1)[-1])["context"] == running_job()["context"]
    with pytest.raises(ReviewQueueError, match="persist"):
        adapter.analyze({**running_job(), "status": "prepared"})


@pytest.mark.parametrize("workflow,completion", [(True, False), (False, True), (True, True)])
def test_conditional_assessment_sections_precede_complete_response_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow: bool, completion: bool,
) -> None:
    job = running_job()
    if workflow:
        job["context"]["workflow_guidance"] = {}
    if completion:
        job["context"]["maintenance_completion"] = {"obligation": {"action_id": "synthetic-action"}}
    captured: list[str] = []
    monkeypatch.setattr(codex_supervisor.shutil, "which", lambda name: "/trusted/bin/codex")
    def capture(argv: list[str], prompt: bytes, timeout: int) -> bytes:
        captured.append(prompt.decode())
        return b""
    monkeypatch.setattr(codex_supervisor, "_bounded_process", capture)
    monkeypatch.setattr(codex_supervisor, "parse_supervisor_output", lambda *args: {"session_id": "synthetic-session"})
    CodexSupervisor(CodexSupervisorSettings(tmp_path, "gpt-5.6-luna", "xhigh")).analyze(job)
    prompt = captured[0]
    contract = prompt.split("## Response contract\n", 1)[1].split("## Job context/evidence", 1)[0]
    if workflow:
        assert prompt.index("## Repository workflow assessment\n") < prompt.index("## Response contract\n")
        assert "workflow_assessment:" in contract
    if completion:
        assert prompt.index("## Post-merge assessment\n") < prompt.index("## Response contract\n")
        assert "maintenance_assessment:" in contract
    assert json.loads(prompt.rsplit("\n", 1)[-1])["context"] == job["context"]


def test_resume_targets_exact_session_with_same_scope_and_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_supervisor.shutil, "which", lambda name: "/trusted/bin/codex")
    adapter = CodexSupervisor(CodexSupervisorSettings(tmp_path, "gpt-6.1-sol", "low", profile="chatgpt", session_id="session-123"))
    command = adapter.command()
    assert command[command.index("resume") + 1] == "session-123"
    assert "--last" not in command and "--ephemeral" not in command
    assert command.index("--sandbox") < command.index("exec")
    assert command.index("--profile") < command.index("exec")
    monkeypatch.setattr(codex_supervisor, "_bounded_process", lambda *args: encoded(events(running_job())))
    resumed = {**running_job(), "session_id": "session-123"}
    assert adapter.analyze(resumed)["session_id"] == "session-123"
    different = events(running_job())
    different[0]["thread_id"] = "session-other"
    monkeypatch.setattr(codex_supervisor, "_bounded_process", lambda *args: encoded(different))
    with pytest.raises(ReviewQueueError, match="different supervisor session"):
        adapter.analyze(resumed)
    with pytest.raises(ReviewQueueError, match="backend-owned"):
        adapter.analyze(running_job())


def test_input_limit_prevents_any_provider_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_supervisor, "_MAX_INPUT", 10)
    adapter = CodexSupervisor(CodexSupervisorSettings(tmp_path, "test-model", "high"))
    with pytest.raises(ReviewQueueError, match="input limit"):
        adapter.analyze(running_job())


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_actual_child_streams_are_drained_without_unbounded_memory(stream: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_supervisor, "_MAX_OUTPUT", 1024)
    program = f"import sys; sys.stdin.buffer.read(); sys.{stream}.buffer.write(b'x'*200000); sys.{stream}.flush()"
    with pytest.raises(ReviewQueueError, match="exceeded"):
        _bounded_process([sys.executable, "-c", program], b"context", 10)


def test_actual_child_timeout_and_failure_are_sanitized() -> None:
    with pytest.raises(ReviewQueueError, match="time limit"):
        _bounded_process([sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"], b"context", 1)
    with pytest.raises(ReviewQueueError, match="private provider output was not disclosed"):
        _bounded_process([sys.executable, "-c", "import sys; sys.stdin.read(); sys.stderr.write('SECRET'); sys.exit(3)"], b"context", 10)


def test_actual_child_success_keeps_only_stdout() -> None:
    output = _bounded_process([sys.executable, "-c", "import sys; sys.stdin.read(); print('result'); sys.stderr.write('private')"], b"context", 10)
    assert output == b"result\n"


def test_unsupported_windows_transport_refuses_before_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_supervisor, "os", SimpleNamespace(name="nt"))
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("An unsupported transport must not create a subprocess")
    monkeypatch.setattr(codex_supervisor.subprocess, "Popen", unexpected)
    with pytest.raises(ReviewQueueError, match="WSL backend"):
        _bounded_process(["codex"], b"context", 10)
