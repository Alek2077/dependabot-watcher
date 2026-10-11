"""Backend-selected Codex access modes and checked thread provenance."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from ..review_queue import ReviewQueueError


@dataclass(frozen=True)
class CodexAppServerSettings:
    """Access is distinct from job/action authority and evidence privacy.

    Inherited means the installed runtime's base user/project/managed settings,
    not a silently substituted CLI named config preset. Restricted profile
    definitions are supplied by trusted backend configuration, never UI text.
    """

    workspace: Path
    model: str
    reasoning_effort: str
    expected_instruction_sources: tuple[Path, ...]
    access: Literal["inherit", "restricted", "full"] = "inherit"
    permission_profile: str | None = None
    full_access_acknowledged: bool = False
    config_profile: str | None = None
    timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if (not self.workspace.is_absolute() or not self.workspace.is_dir()
                or not isinstance(self.model, str) or not self.model.strip()
                or self.reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}
                or self.access not in {"inherit", "restricted", "full"}
                or not isinstance(self.expected_instruction_sources, tuple)
                or any(not isinstance(p, Path) or not p.is_absolute() for p in self.expected_instruction_sources)
                or len(set(self.expected_instruction_sources)) != len(self.expected_instruction_sources)
                or type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 3600
                or type(self.full_access_acknowledged) is not bool):
            raise ReviewQueueError("Codex app-server requires explicit valid model, workspace and access settings.")
        if self.config_profile is not None:
            raise ReviewQueueError("This Codex app-server route cannot preserve a named CLI config preset; select base settings explicitly.")
        if self.access == "restricted":
            if (not isinstance(self.permission_profile, str)
                    or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", self.permission_profile)):
                raise ReviewQueueError("Restricted Codex access requires a backend-selected named permission profile.")
        elif self.permission_profile is not None:
            raise ReviewQueueError("A named restriction cannot be combined with inherited or full access.")
        if (self.access == "full") != self.full_access_acknowledged:
            raise ReviewQueueError("Full access requires deliberate acknowledgement and cannot be applied implicitly.")

    def command(self, config_overrides: tuple[str, ...] = ()) -> list[str]:
        """No global settings edits; overrides are trusted backend definitions."""
        executable = shutil.which("codex")
        if executable is None:
            raise ReviewQueueError("Codex CLI is unavailable; no supervisor was launched.")
        command = [executable]
        for override in config_overrides:
            if not isinstance(override, str) or "=" not in override or "\0" in override:
                raise ReviewQueueError("Codex runtime overrides must be explicit backend settings.")
            command.extend(["-c", override])
        command.extend(["app-server", "--listen", "stdio://"])
        return command

    def thread_params(self, session_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": str(self.workspace), "model": self.model,
                                 "config": {"model_reasoning_effort": self.reasoning_effort}}
        if session_id is not None:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", session_id):
                raise ReviewQueueError("Resume must identify one backend-owned Codex session.")
            params["threadId"] = session_id
            # Do not transport an ever-growing transcript back to the client.
            # Codex still restores the conversation for inference on resume.
            params["excludeTurns"] = True
            params["initialTurnsPage"] = {"limit": 64, "sortDirection": "desc", "itemsView": "notLoaded"}
        if self.access == "restricted":
            params.update(permissions=self.permission_profile, approvalPolicy="on-request", approvalsReviewer="user")
        elif self.access == "full":
            params.update(sandbox="danger-full-access", approvalPolicy="never", approvalsReviewer="user")
        return params

    def verify_thread(self, response: Mapping[str, Any], session_id: str | None = None) -> dict[str, Any]:
        """Check returned settings before any turn; this is not isolation proof.

        Enforced filesystem/network/tool behavior still needs actual canaries.
        No email, credential, raw configuration or tool transcript is retained.
        """
        thread = response.get("thread")
        sandbox = response.get("sandbox")
        approval = response.get("approvalPolicy")
        approval_fields = {"sandbox_approval", "rules", "mcp_elicitations", "request_permissions", "skill_approval"}
        raw_granular = approval.get("granular") if isinstance(approval, dict) else None
        granular: dict[str, Any] = raw_granular if isinstance(raw_granular, dict) else {}
        granular_valid = (isinstance(approval, dict) and isinstance(raw_granular, dict) and set(approval) == {"granular"}
                          and {"sandbox_approval", "rules", "mcp_elicitations"} <= set(granular)
                          and all(type(granular[key]) is bool for key in approval_fields & set(granular)))
        instructions = response.get("instructionSources")
        if (not isinstance(thread, dict) or not isinstance(thread.get("id"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", thread["id"])
                or (session_id is not None and thread["id"] != session_id)
                or response.get("model") != self.model
                or response.get("reasoningEffort") != self.reasoning_effort
                or response.get("cwd") != str(self.workspace)
                or not isinstance(response.get("modelProvider"), str) or not response["modelProvider"]
                or response.get("approvalsReviewer") != "user"
                or not isinstance(sandbox, dict)
                or sandbox.get("type") not in {"readOnly", "workspaceWrite", "dangerFullAccess", "externalSandbox"}
                or not isinstance(instructions, list) or any(not isinstance(p, str) for p in instructions)
                or not (approval in ("never", "on-request", "untrusted") if isinstance(approval, str)
                        else granular_valid)):
            raise ReviewQueueError("Codex returned incomplete or different effective supervisor settings.")
        active = response.get("activePermissionProfile")
        environments = thread.get("environments")
        # Newer local runtimes identify ordinary command approvals as "local".
        # An identifier is not proof of a remote host: check the selected thread
        # environment before any model turn, and still refuse remote selections.
        # Older runtimes omit this experimental metadata or return null.
        if environments is not None:
            if (not isinstance(environments, list) or len(environments) != 1
                    or not isinstance(environments[0], dict)
                    or environments[0].get("environmentId") != "local"
                    or environments[0].get("cwd") != str(self.workspace)
                    or not isinstance(environments[0].get("runtimeWorkspaceRoots"), list)
                    or any(not isinstance(root, str) or not Path(root).is_absolute() or "\0" in root
                           for root in environments[0]["runtimeWorkspaceRoots"])):
                raise ReviewQueueError("Codex selected an unsupported or different execution environment.")
        if instructions != [str(p) for p in self.expected_instruction_sources]:
            raise ReviewQueueError("Codex did not load the expected trusted instruction sources.")
        if self.access == "restricted":
            if (not isinstance(active, dict) or active.get("id") != self.permission_profile
                    or not (approval == "on-request" or granular_valid and set(granular) <= approval_fields)
                    or sandbox["type"] in {"dangerFullAccess", "externalSandbox"}):
                raise ReviewQueueError("Codex did not select the requested restricted permission mode.")
        elif self.access == "full" and (sandbox["type"] != "dangerFullAccess" or approval != "never"):
            raise ReviewQueueError("Codex did not allow the deliberately selected full-access mode.")
        # Persist only the effective fields this client actually checks. A
        # provider extension must not smuggle raw config/credentials into the
        # private job journal through an otherwise valid settings response.
        approval_receipt = approval if isinstance(approval, str) else {
            "granular": {key: value for key, value in granular.items()
                         if key in {"sandbox_approval", "rules", "skill_approval", "request_permissions", "mcp_elicitations"}
                         and type(value) is bool}}
        sandbox_receipt: dict[str, Any] = {"type": sandbox["type"]}
        if type(sandbox.get("networkAccess")) is bool:
            sandbox_receipt["networkAccess"] = sandbox["networkAccess"]
        profile_id = active.get("id") if isinstance(active, dict) else None
        if profile_id is not None and (not isinstance(profile_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", profile_id)):
            raise ReviewQueueError("Codex returned an invalid effective permission profile identity.")
        return {"session_id": thread["id"], "access": self.access, "model": self.model,
                "model_provider": response["modelProvider"], "reasoning_effort": self.reasoning_effort,
                "workspace": str(self.workspace), "approval_policy": approval_receipt,
                "sandbox": sandbox_receipt, "active_permission_profile": {"id": profile_id} if profile_id else None,
                "instruction_sources": list(instructions),
                "command_rules_verification": "not_established",
                "filesystem_unrestricted": sandbox["type"] in {"dangerFullAccess", "externalSandbox"},
                "remote_mutation_isolation": "not_established",
                "action_authority": "not_granted_by_access_mode"}
