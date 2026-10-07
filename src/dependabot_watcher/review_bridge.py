from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, BinaryIO, cast

from .config import ConfigError, load_config
from .execution import ExecutionError
from .github import GhCliClient, GitHubClient, GitHubError, public_github_error_message
from .reconciliation import ReconciliationError
from .notifications import _content
from .review_actions import ActionReviews
from .review_queue import ReviewQueue, ReviewQueueError
from .runtime_state import RuntimeStateError, load_private_json, load_run_state, run_lock
from .supervisor_status import supervision_status
from .supervision import SupervisorJobs
from .supervisor_permissions import SupervisorPermissions
from .supervisor_bridge import launch_exchange
from .scheduled_supervisor import scheduled_supervisor_plan
from .scheduled_triggers import claim_trigger


_MAX_REQUEST_BYTES = 65_536
_MAX_RESPONSE_BYTES = 10_000_000


def notification_attention(queue: ReviewQueue, request: dict[str, Any]) -> dict[str, Any]:
    """Read one bound run's sparse attention without collecting or deciding.

    The sender cannot choose a report path, title, body, command or merge. The
    latest report and durable attempt must still agree before publication.
    """
    request_id, run_id = request.get("request_id"), request.get("run_id")
    if (set(request) != {"schema_version", "request_id", "operation", "run_id"}
            or request.get("operation") != "notification_attention"
            or type(request.get("schema_version")) is not int or request["schema_version"] != 1
            or not isinstance(request_id, str) or not re.fullmatch(r"read_[A-Za-z0-9_]{1,59}", request_id)
            or not isinstance(run_id, str) or not re.fullmatch(r"run_[a-f0-9]{20}", run_id)):
        raise ReviewQueueError("Notification requires a bounded current run identity.")
    with run_lock(queue.directory, wait_seconds=2):
        queue.require_current_configuration()
        state = load_run_state(queue.directory)
        report = load_private_json(queue.directory, Path("latest-run.json"))
        attempt = state.get("last_attempt")
        if (not isinstance(attempt, dict) or attempt.get("run_id") != run_id
                or report.get("run_id") != run_id or type(report.get("schema_version")) is not int or report["schema_version"] != 1
                or report.get("mode") != "run" or report.get("mutation_authorized") is not False
                or report.get("summary") != attempt.get("queue")
                or report.get("advisory_coverage") != attempt.get("advisory_coverage")
                or report.get("provider_usage") != attempt.get("provider_usage")
                or not isinstance(report.get("notification"), dict)
                or {key: value for key, value in report["notification"].items() if key != "delivery"}
                != attempt.get("notification")):
            raise ReviewQueueError("Notification run changed or does not match its saved evidence.")
        needed = report["notification"].get("needed")
        if type(needed) is not bool:
            raise ReviewQueueError("Saved notification decision is malformed.")
        title, body = _content(report, review_available=True) if needed else (None, None)
        return {"schema_version": 1, "request_id": request_id, "status": "ok",
                "run_id": run_id, "needed": needed, "title": title, "body": body,
                "capabilities": {"github_mutations": False, "model_calls": False}}


def exchange(queue: ReviewQueue, incoming: BinaryIO, outgoing: BinaryIO, *, actions: ActionReviews | None = None) -> int:
    """One bounded UTF-8 JSON request; EOF closes the input frame.

    The native caller supplies trusted launch configuration through argv, closes
    stdin after writing and checks child exit status/response version. Request
    bodies cannot supply filesystem paths, shell commands or approval authority.
    Ordinary operations return one response. Explicit supervisor_start instead
    returns bounded JSON-line lifecycle/preview frames for a streaming consumer;
    permission replies still use independent ordinary exchanges.
    """
    request_id: str | None = None
    try:
        payload = incoming.read(_MAX_REQUEST_BYTES + 1)
        if len(payload) > _MAX_REQUEST_BYTES:
            raise ReviewQueueError("Review request exceeds its size limit.")
        request = json.loads(payload.decode("utf-8"))
        if not isinstance(request, dict):
            raise ReviewQueueError("Review request must be a JSON object.")
        candidate = request.get("request_id")
        if isinstance(candidate, str) and len(candidate) <= 64:
            request_id = candidate
        if not isinstance(request.get("operation"), str):
            raise ReviewQueueError("Review operation must be a string.")
        if request.get("operation") == "supervisor_start":
            return launch_exchange(queue, cast(GitHubClient, actions.client) if actions is not None else None, request, outgoing)
        if request.get("operation") == "notification_attention":
            response = notification_attention(queue, request)
        elif request.get("operation") in {"scheduled_supervisor_plan", "scheduled_supervisor_claim"}:
            if (set(request) != {"schema_version", "request_id", "operation", "run_id"}
                    or type(request.get("schema_version")) is not int or request["schema_version"] != 1
                    or not isinstance(request_id, str) or not re.fullmatch(r"read_[A-Za-z0-9_]{1,59}", request_id)):
                raise ReviewQueueError("Scheduled selection requires a bounded read-only request.")
            response = {"schema_version": 1, "request_id": request_id, "status": "ok",
                        **({"plan": scheduled_supervisor_plan(queue, request["run_id"])}
                           if request["operation"] == "scheduled_supervisor_plan"
                           else {"trigger": claim_trigger(queue, request["run_id"])})}
        elif request.get("operation") in ("supervisor_permissions", "supervisor_permission_decide"):
            response = SupervisorPermissions(SupervisorJobs(queue)).handle(request)
        elif request.get("operation") == "supervisor_status":
            response = supervision_status(queue, request)
        elif request.get("operation") in ("prepare_action", "confirm_action", "check_action", "action_history", "collect_queue"):
            if actions is None:
                raise ReviewQueueError("Action review is not connected to this backend.")
            response = actions.handle(request)
        else:
            response = queue.handle(request)
            if actions is not None:
                response["reviews"] = actions.snapshot()
                response["execution_enabled"] = (
                    queue.config.operation.autonomy == "approval_required" and queue.config.operation.mutation_enabled)
        exit_code = 0
    except (ValueError, UnicodeError, RecursionError):
        response = {"schema_version": 1, "request_id": request_id, "status": "error",
                    "message": "Review request must contain valid UTF-8 JSON."}
        exit_code = 2
    except GitHubError:
        response = {"schema_version": 1, "request_id": request_id, "status": "error",
                    "message": public_github_error_message("action review")}
        exit_code = 2
    except (ReviewQueueError, RuntimeStateError, ExecutionError, ReconciliationError) as exc:
        response = {"schema_version": 1, "request_id": request_id, "status": "error",
                    "message": str(exc)}
        exit_code = 2
    encoded = (json.dumps(response, ensure_ascii=True) + "\n").encode("utf-8")
    if len(encoded) > _MAX_RESPONSE_BYTES:
        encoded = (json.dumps({
            "schema_version": 1, "request_id": request_id, "status": "error",
            "message": "Review response exceeds its size limit; refresh to inspect saved state.",
        }) + "\n").encode("utf-8")
        exit_code = 2
    outgoing.write(encoded)
    outgoing.flush()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native review's local structured backend bridge.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        queue = ReviewQueue(load_config(args.config), args.state_dir, config_path=args.config)
    except ConfigError:
        sys.stdout.buffer.write(b'{"schema_version":1,"request_id":null,"status":"error",'
                                b'"message":"Review configuration is unavailable or invalid."}\n')
        return 2
    actions = ActionReviews(
        queue, GhCliClient(timeout_seconds=queue.config.collection.command_timeout_seconds),
        config_path=args.config,
    )
    return exchange(queue, sys.stdin.buffer, sys.stdout.buffer, actions=actions)


if __name__ == "__main__":
    raise SystemExit(main())
