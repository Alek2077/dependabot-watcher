from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import BinaryIO, cast

from .config import ConfigError, load_config
from .execution import ExecutionError
from .github import GhCliClient, GitHubClient, GitHubError, public_github_error_message
from .reconciliation import ReconciliationError
from .review_actions import ActionReviews
from .review_queue import ReviewQueue, ReviewQueueError
from .runtime_state import RuntimeStateError
from .supervisor_status import supervision_status
from .supervision import SupervisorJobs
from .supervisor_permissions import SupervisorPermissions
from .supervisor_bridge import launch_exchange


_MAX_REQUEST_BYTES = 65_536
_MAX_RESPONSE_BYTES = 10_000_000


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
        if request.get("operation") == "supervisor_start":
            return launch_exchange(queue, cast(GitHubClient, actions.client) if actions is not None else None, request, outgoing)
        if request.get("operation") in ("supervisor_permissions", "supervisor_permission_decide"):
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
