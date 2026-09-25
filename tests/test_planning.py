from __future__ import annotations

import copy
import json

import pytest

from dependabot_watcher.output import render_plan
from dependabot_watcher.planning import PlanningError, plan_from_advisory
from dependabot_watcher.output import render_plan


def _pull_request(
    number: int,
    *,
    classification: str = "baseline_candidate",
    related_alerts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "candidate_identity": {
            "repository": "example-org/example-app",
            "number": number,
            "title": f"Bump dependency in PR {number}",
            "url": f"https://github.com/example-org/example-app/pull/{number}",
            "expected_base": "main",
            "observed_base": "main",
            "head_ref": f"dependabot/pip/dependency-{number}",
            "head_sha": f"head-{number}",
        },
        "updated_at": "2026-09-21T08:00:00Z",
        "evidence_observed_at": "2026-09-21T09:00:00Z",
        "author": "app/dependabot",
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "is_draft": False,
        "checks": "passing",
        "status_checks": [
            {
                "name": "CI",
                "state": "passing",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ],
        "classification": classification,
        "reasons": [] if classification == "baseline_candidate" else ["Checks are pending."],
        "path_assessment": {
            "classification": "dependency_only",
            "files": [
                {
                    "path": "requirements.txt",
                    "categories": ["dependency_manifest"],
                    "dependency_related": True,
                    "sensitive": False,
                    "classified": True,
                },
                {
                    "path": "requirements.lock" if number == 42 else f"lock-{number}.txt",
                    "categories": ["dependency_lockfile"],
                    "dependency_related": True,
                    "sensitive": False,
                    "classified": True,
                },
            ],
            "unclassified_paths": [],
            "sensitive_paths": [],
        },
        "recent_sensitive_overlap": [],
        "related_alerts": related_alerts or [],
        "mutation_authorized": False,
    }


def _advisory_report() -> dict[str, object]:
    likely_alert = {
        "number": 7,
        "dependency_name": "example-lib",
        "ecosystem": "pip",
        "severity": "high",
        "manifest_path": "requirements-dev.txt",
        "first_patched_version": "2.0.0",
        "classification": "unresolved_without_pr",
        "target_version": "2.0.0",
        "coverage_evidence": "likely",
        "manifest_path_matches": False,
        "patched_version_relation": "exact",
        "manifest_relationship_path": ["requirements-dev.txt", "requirements.txt"],
        "meets_first_patched_version": True,
    }
    return {
        "schema_version": 1,
        "mode": "advise",
        "generated_at": "2026-09-21T09:00:00Z",
        "status": "complete",
        "capabilities": {
            "model_calls": False,
            "persistent_local_writes": False,
            "github_mutations": False,
            "notifications": False,
        },
        "inspection": {
            "schema_version": 1,
            "mode": "inspect",
            "generated_at": "2026-09-21T09:00:00Z",
            "status": "complete",
            "repositories": [
                {
                    "repository": "example-org/example-app",
                    "expected_base": "main",
                    "evidence_snapshot": {
                        "base_sha": "base123",
                        "observed_at": "2026-09-21T09:00:00Z",
                    },
                    "workflow_profile": {
                        "merge_method": "squash",
                        "promotion_branch": "production",
                        "required_checks": ["CI"],
                        "deployment_environments": ["staging"],
                        "validation_notes": ["Verify staging before continuing."],
                    },
                    "pull_requests": [
                        _pull_request(42, related_alerts=[likely_alert]),
                        _pull_request(43),
                        _pull_request(44, classification="attention_required"),
                    ],
                    "alerts": [
                        {
                            "number": 7,
                            "state": "open",
                            "dependency_name": "example-lib",
                            "ecosystem": "pip",
                            "manifest_path": "requirements-dev.txt",
                            "severity": "high",
                            "first_patched_version": "2.0.0",
                            "classification": "unresolved_without_pr",
                            "package_related_open_pull_requests": [
                                {
                                    "number": 42,
                                    "target_version": "2.0.0",
                                    "coverage_evidence": "likely",
                                    "manifest_path_matches": False,
                                    "patched_version_relation": "exact",
                                    "meets_first_patched_version": True,
                                    "manifest_relationship_path": [
                                        "requirements-dev.txt",
                                        "requirements.txt",
                                    ],
                                }
                            ],
                        },
                        {
                            "number": 8,
                            "state": "open",
                            "dependency_name": "uncovered-lib",
                            "ecosystem": "pip",
                            "manifest_path": "requirements.txt",
                            "severity": "critical",
                            "first_patched_version": "2.0.0",
                            "classification": "unresolved_without_pr",
                            "package_related_open_pull_requests": [],
                        },
                    ],
                    "manifest_evidence": {
                        "status": "complete",
                        "base_sha": "base123",
                        "files": [
                            {
                                "path": "requirements-dev.txt",
                                "depth": 0,
                                "includes": [
                                    {"path": "requirements.txt", "relation": "requirement"}
                                ],
                            },
                            {
                                "path": "requirements.txt",
                                "depth": 1,
                                "includes": [],
                            },
                        ],
                        "relationships": [
                            {
                                "source_manifest": "requirements-dev.txt",
                                "included_manifest": "requirements.txt",
                                "relation": "requirement",
                                "depth": 1,
                            }
                        ],
                    },
                    "errors": [],
                    "truncations": [],
                    "summary": {
                        "open_pull_request_count": 3,
                        "open_alert_count": 2,
                        "unresolved_alert_count": 2,
                    },
                }
            ],
        },
        "advisory": {
            "mode": "off",
            "status": "disabled",
            "decisions": [],
            "deferred": [],
            "errors": [],
            "calls": [],
        },
    }


def test_plan_builds_deterministic_recommended_order_and_suppresses_likely_alert_step() -> None:
    report = plan_from_advisory(_advisory_report())  # type: ignore[arg-type]

    steps = report["repositories"][0]["recommended_order"]
    assert [(item["kind"], item["identity"]["number"]) for item in steps] == [
        ("pull_request", 42),
        ("alert", 8),
        ("pull_request", 43),
        ("pull_request", 44),
    ]
    assert steps[0]["expected_alert_impact"][0]["coverage_evidence"] == "likely"
    assert all(item["prerequisites"] == [] for item in steps)
    assert steps[0]["conflicts"][0]["step_id"] == steps[2]["step_id"]
    assert steps[0]["conflicts"][0]["shared_paths"] == ["requirements.txt"]
    assert any(
        gate["type"] == "promotion_branch" and gate["name"] == "production"
        for gate in steps[0]["validation_gates"]
    )
    assert steps[0]["freshness"]["base_sha"] == "base123"
    assert steps[0]["freshness"]["head_sha"] == "head-42"
    assert all(item["freshness"]["reinspection_required"] is True for item in steps)
    assert all(item["mutation_authorized"] is False for item in steps)
    assert report["mutation_authorized"] is False


def test_plan_verbosity_changes_human_detail_only() -> None:
    report = plan_from_advisory(_advisory_report())  # type: ignore[arg-type]

    concise = render_plan(report, "text", verbosity="concise")
    detailed = render_plan(report, "text", verbosity="detailed")

    assert "Validation:" not in concise
    assert "Validation:" in detailed
    assert "Freshness:" in detailed
    assert "Decision source: deterministic policy (no model used)" in concise
    assert "without direct open-PR coverage" in concise
    assert len(concise) < len(detailed)
    assert json.loads(render_plan(report, "json", verbosity="concise")) == json.loads(
        render_plan(report, "json", verbosity="detailed")
    )


def test_detailed_plan_sanitizes_freshness_for_humans_but_preserves_json() -> None:
    report = plan_from_advisory(_advisory_report())  # type: ignore[arg-type]
    hostile_sha = "head\x1b]0;owned\x07\r\nforged"
    report["repositories"][0]["recommended_order"][0]["freshness"][
        "head_sha"
    ] = hostile_sha

    detailed = render_plan(report, "text", verbosity="detailed")
    payload = json.loads(render_plan(report, "json", verbosity="detailed"))

    assert "head=head forged" in detailed
    assert "\x1b" not in detailed
    assert "owned" not in detailed
    assert (
        payload["repositories"][0]["recommended_order"][0]["freshness"]["head_sha"]
        == hostile_sha
    )


def test_model_advice_can_make_action_more_conservative_without_changing_order() -> None:
    source = _advisory_report()
    source["advisory"]["mode"] = "reviewed"  # type: ignore[index]
    source["advisory"]["provider"] = "codex_cli"  # type: ignore[index]
    source["capabilities"]["model_calls"] = True  # type: ignore[index]
    source["advisory"]["decisions"] = [  # type: ignore[index]
        {
            "kind": "pull_request",
            "identity": {"repository": "example-org/example-app", "number": 42},
            "effective_advice": {
                "recommended_action": "hold",
                "summary": "Wait for more evidence.",
            },
        }
    ]

    report = plan_from_advisory(source)  # type: ignore[arg-type]
    rendered = render_plan(report, "text")
    steps = report["repositories"][0]["recommended_order"]

    assert steps[0]["identity"]["number"] == 42
    assert steps[0]["action"] == "hold"
    assert "accepted optional Codex CLI advice (reviewed mode)" in rendered


def test_plan_provenance_does_not_treat_failed_provider_call_as_advice() -> None:
    source = _advisory_report()
    source["advisory"]["mode"] = "economy"  # type: ignore[index]
    source["advisory"]["provider"] = "codex_cli"  # type: ignore[index]
    source["advisory"]["errors"] = [  # type: ignore[index]
        {"stage": "triage", "batch": 1, "message": "provider failed"}
    ]
    source["capabilities"]["model_calls"] = True  # type: ignore[index]

    rendered = render_plan(plan_from_advisory(source), "text")  # type: ignore[arg-type]

    assert "analysis failed and no model advice was accepted" in rendered
    assert "informed by" not in rendered


def test_plan_outputs_are_concise_for_humans_and_complete_for_agents() -> None:
    report = plan_from_advisory(_advisory_report())  # type: ignore[arg-type]

    text = render_plan(report, "text")
    markdown = render_plan(report, "markdown")
    payload = json.loads(render_plan(report, "json"))

    assert "Recommended Order" in text
    assert "PR #42" in text
    assert "Then: re-inspect repository state" in text
    assert "### Recommended Order" in markdown
    assert payload["source"]["inspection"]["repositories"][0]["manifest_evidence"][
        "status"
    ] == "complete"


def test_partial_repository_never_recommends_merge_validation() -> None:
    source = _advisory_report()
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    repository["errors"] = [  # type: ignore[index]
        {
            "repository": "example-org/example-app",
            "surface": "open_alerts",
            "message": "unavailable",
        }
    ]

    report = plan_from_advisory(source)  # type: ignore[arg-type]
    steps = report["repositories"][0]["recommended_order"]

    assert report["status"] == "partial"
    assert all(step["action"] != "validate_then_merge" for step in steps)
    assert "inspection evidence is incomplete" in " ".join(steps[0]["blockers"])


def test_plan_identity_is_stable_for_one_snapshot_and_changes_with_base_sha() -> None:
    source = _advisory_report()

    first = plan_from_advisory(source)  # type: ignore[arg-type]
    second = plan_from_advisory(source)  # type: ignore[arg-type]
    source["inspection"]["repositories"][0]["evidence_snapshot"]["base_sha"] = (  # type: ignore[index]
        "new-base"
    )
    source["inspection"]["repositories"][0]["manifest_evidence"]["base_sha"] = (  # type: ignore[index]
        "new-base"
    )
    changed = plan_from_advisory(source)  # type: ignore[arg-type]

    assert first["plan_id"] == second["plan_id"]
    assert first["repositories"][0]["plan_id"] != changed["repositories"][0]["plan_id"]


def test_mixed_alert_records_do_not_combine_severity_and_coverage() -> None:
    source = _advisory_report()
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    repository["pull_requests"] = [  # type: ignore[index]
        _pull_request(
            42,
            related_alerts=[
                {
                    "number": 7,
                    "dependency_name": "low-covered",
                    "ecosystem": "pip",
                    "severity": "low",
                    "manifest_path": "requirements.txt",
                    "first_patched_version": "2.0.0",
                    "classification": "likely_covered_by_open_pr",
                    "target_version": "2.0.0",
                    "coverage_evidence": "direct",
                    "manifest_path_matches": True,
                    "patched_version_relation": "exact",
                    "manifest_relationship_path": ["requirements.txt"],
                    "meets_first_patched_version": True,
                },
                {
                    "number": 8,
                    "dependency_name": "critical-unverified",
                    "ecosystem": "pip",
                    "severity": "critical",
                    "manifest_path": "requirements.txt",
                    "first_patched_version": "2.0.0",
                    "classification": "related_open_pr_unverified",
                    "target_version": "1.0.0",
                    "coverage_evidence": "unverified",
                    "manifest_path_matches": True,
                    "patched_version_relation": "older_release",
                    "manifest_relationship_path": ["requirements.txt"],
                    "meets_first_patched_version": False,
                },
            ],
        )
    ]
    repository["alerts"] = [  # type: ignore[index]
        {
            "number": 7,
            "state": "open",
            "dependency_name": "low-covered",
            "ecosystem": "pip",
            "manifest_path": "requirements.txt",
            "severity": "low",
            "first_patched_version": "2.0.0",
            "classification": "likely_covered_by_open_pr",
            "package_related_open_pull_requests": [
                {
                    "number": 42,
                    "target_version": "2.0.0",
                    "coverage_evidence": "direct",
                    "manifest_path_matches": True,
                    "patched_version_relation": "exact",
                    "manifest_relationship_path": ["requirements.txt"],
                    "meets_first_patched_version": True,
                }
            ],
        },
        {
            "number": 8,
            "state": "open",
            "dependency_name": "critical-unverified",
            "ecosystem": "pip",
            "manifest_path": "requirements.txt",
            "severity": "critical",
            "first_patched_version": "2.0.0",
            "classification": "related_open_pr_unverified",
            "package_related_open_pull_requests": [
                {
                    "number": 42,
                    "target_version": "1.0.0",
                    "coverage_evidence": "unverified",
                    "manifest_path_matches": True,
                    "patched_version_relation": "older_release",
                    "manifest_relationship_path": ["requirements.txt"],
                    "meets_first_patched_version": False,
                }
            ],
        },
    ]

    report = plan_from_advisory(source)  # type: ignore[arg-type]
    steps = report["repositories"][0]["recommended_order"]
    pull_request_step = next(step for step in steps if step["kind"] == "pull_request")

    assert "critical/high" not in pull_request_step["priority_reason"]
    assert [item["alert_number"] for item in pull_request_step["expected_alert_impact"]] == [7]
    assert [item["alert_number"] for item in pull_request_step["related_alerts"]] == [8, 7]
    assert steps[0]["kind"] == "alert"
    assert steps[0]["identity"]["number"] == 8


def test_plan_identity_changes_with_material_pr_and_alert_state() -> None:
    source = _advisory_report()
    original = plan_from_advisory(source)  # type: ignore[arg-type]

    source["inspection"]["repositories"][0]["pull_requests"][0][  # type: ignore[index]
        "classification"
    ] = "attention_required"
    source["inspection"]["repositories"][0]["pull_requests"][0][  # type: ignore[index]
        "reasons"
    ] = ["Checks are pending."]
    changed_pr = plan_from_advisory(source)  # type: ignore[arg-type]

    assert original["plan_id"] != changed_pr["plan_id"]
    assert (
        original["repositories"][0]["recommended_order"][0]["step_id"]
        != changed_pr["repositories"][0]["recommended_order"][0]["step_id"]
    )

    source = _advisory_report()
    original = plan_from_advisory(source)  # type: ignore[arg-type]
    source["inspection"]["repositories"][0]["alerts"][0][  # type: ignore[index]
        "advisory_summary"
    ] = "Updated advisory evidence."
    changed_alert = plan_from_advisory(source)  # type: ignore[arg-type]

    assert original["plan_id"] != changed_alert["plan_id"]
    assert (
        original["repositories"][0]["alert_snapshot"]["digest"]
        != changed_alert["repositories"][0]["alert_snapshot"]["digest"]
    )


def test_plan_identity_ignores_generation_time_when_evidence_is_unchanged() -> None:
    source = _advisory_report()
    first = plan_from_advisory(source)  # type: ignore[arg-type]
    source["generated_at"] = "2026-09-21T10:00:00Z"
    source["inspection"]["generated_at"] = "2026-09-21T10:00:00Z"  # type: ignore[index]
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    repository["evidence_snapshot"]["observed_at"] = "2026-09-21T10:00:00Z"
    for pull_request in repository["pull_requests"]:
        pull_request["evidence_observed_at"] = "2026-09-21T10:00:00Z"
    second = plan_from_advisory(source)  # type: ignore[arg-type]

    assert first["plan_id"] == second["plan_id"]
    assert first["repositories"][0]["plan_id"] == second["repositories"][0]["plan_id"]
    assert [
        item["step_id"] for item in first["repositories"][0]["recommended_order"]
    ] == [item["step_id"] for item in second["repositories"][0]["recommended_order"]]


@pytest.mark.parametrize(
    "field",
    ["evidence_snapshot", "expected_base", "errors", "truncations"],
)
def test_planning_rejects_missing_repository_freshness_contract(field: str) -> None:
    source = _advisory_report()
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    repository.pop(field)

    with pytest.raises(PlanningError):
        plan_from_advisory(source)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    ["candidate_identity", "updated_at", "evidence_observed_at", "classification"],
)
def test_planning_rejects_missing_pr_freshness_contract(field: str) -> None:
    source = _advisory_report()
    pull_request = source["inspection"]["repositories"][0]["pull_requests"][0]  # type: ignore[index]
    pull_request.pop(field)

    with pytest.raises(PlanningError):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_planning_rejects_empty_repository_collection() -> None:
    source = _advisory_report()
    source["inspection"]["repositories"] = []  # type: ignore[index]

    with pytest.raises(PlanningError, match="repository collection"):
        plan_from_advisory(source)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["alerts", "errors", "truncations"])
def test_planning_rejects_malformed_repository_records(field: str) -> None:
    source = _advisory_report()
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    repository[field] = ["malformed"]

    with pytest.raises(PlanningError):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_planning_rejects_branch_mismatched_baseline_candidate() -> None:
    source = _advisory_report()
    pull_request = source["inspection"]["repositories"][0]["pull_requests"][0]  # type: ignore[index]
    pull_request["candidate_identity"]["observed_base"] = "release"

    with pytest.raises(PlanningError, match="contradicts its evidence"):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_planning_rejects_nonreciprocal_alert_relationship() -> None:
    source = _advisory_report()
    alert = source["inspection"]["repositories"][0]["alerts"][1]  # type: ignore[index]
    alert["classification"] = "likely_covered_by_open_pr"
    alert["package_related_open_pull_requests"] = [
        {
            "number": 42,
            "target_version": "2.0.0",
            "coverage_evidence": "direct",
            "manifest_path_matches": True,
            "patched_version_relation": "exact",
            "manifest_relationship_path": ["requirements.txt"],
            "meets_first_patched_version": True,
        }
    ]

    with pytest.raises(PlanningError, match="reciprocal"):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_collection_error_pr_remains_visible_and_non_executable() -> None:
    source = _advisory_report()
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    repository["pull_requests"] = [
        {
            "candidate_identity": {
                "repository": "example-org/example-app",
                "number": 42,
                "expected_base": "main",
                "observed_base": "main",
                "head_ref": "dependabot/pip/example-lib-2.0",
                "head_sha": None,
            },
            "classification": "collection_error",
            "reasons": ["GitHub PR detail collection failed."],
            "related_alerts": [],
            "mutation_authorized": False,
        }
    ]
    repository["alerts"] = []
    repository["errors"] = [
        {
            "repository": "example-org/example-app",
            "surface": "pull_request_details:42",
            "message": "unavailable",
        }
    ]

    report = plan_from_advisory(source)  # type: ignore[arg-type]
    step = report["repositories"][0]["recommended_order"][0]

    assert report["status"] == "partial"
    assert step["action"] == "investigate"
    assert step["freshness"]["head_sha"] is None
    assert all(gate["type"] != "exact_pr_head" for gate in step["validation_gates"])


def test_planning_rejects_pr_side_relationship_without_alert_evidence() -> None:
    source = _advisory_report()
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    repository["alerts"] = []

    with pytest.raises(PlanningError, match="unknown or repeated alert relationship"):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_planning_rejects_missing_pr_side_reciprocal_relationship() -> None:
    source = _advisory_report()
    pull_request = source["inspection"]["repositories"][0]["pull_requests"][0]  # type: ignore[index]
    pull_request["related_alerts"] = []

    with pytest.raises(PlanningError, match="lacks reciprocal"):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_planning_rejects_unanchored_likely_manifest_relationship() -> None:
    source = _advisory_report()
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    alert_relationship = repository["alerts"][0]["package_related_open_pull_requests"][0]
    pr_relationship = repository["pull_requests"][0]["related_alerts"][0]
    for relationship in (alert_relationship, pr_relationship):
        relationship["manifest_relationship_path"] = [
            "unrelated/source.txt",
            "requirements.txt",
        ]

    with pytest.raises(PlanningError, match="unanchored"):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_planning_rejects_direct_coverage_for_unchanged_manifest() -> None:
    source = _advisory_report()
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    alert = repository["alerts"][0]
    alert["classification"] = "likely_covered_by_open_pr"
    alert_relationship = alert["package_related_open_pull_requests"][0]
    pr_relationship = repository["pull_requests"][0]["related_alerts"][0]
    for relationship in (alert_relationship, pr_relationship):
        relationship["coverage_evidence"] = "direct"
        relationship["manifest_path_matches"] = True
        relationship["manifest_relationship_path"] = ["requirements-dev.txt"]
    pr_relationship["classification"] = "likely_covered_by_open_pr"

    with pytest.raises(PlanningError, match="manifest-match"):
        plan_from_advisory(source)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path_assessment", {"classification": "dependency_only"}),
        ("recent_sensitive_overlap", {}),
    ],
)
def test_planning_rejects_incomplete_baseline_evidence(field: str, value: object) -> None:
    source = _advisory_report()
    pull_request = source["inspection"]["repositories"][0]["pull_requests"][0]  # type: ignore[index]
    pull_request[field] = value

    with pytest.raises(PlanningError):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_planning_rejects_structurally_empty_error_record() -> None:
    source = _advisory_report()
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    repository["errors"] = [{}]

    with pytest.raises(PlanningError):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_planning_rejects_case_insensitive_duplicate_repository() -> None:
    source = _advisory_report()
    duplicate = copy.deepcopy(source["inspection"]["repositories"][0])  # type: ignore[index]
    duplicate["repository"] = "EXAMPLE-ORG/example-app"
    for pull_request in duplicate["pull_requests"]:
        pull_request["candidate_identity"]["repository"] = duplicate["repository"]
    source["inspection"]["repositories"].append(duplicate)  # type: ignore[index]

    with pytest.raises(PlanningError, match="repeats repository"):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_planning_rejects_fabricated_path_categories() -> None:
    source = _advisory_report()
    file_evidence = source["inspection"]["repositories"][0]["pull_requests"][0][  # type: ignore[index]
        "path_assessment"
    ]["files"][0]
    file_evidence["categories"] = ["fabricated_category"]

    with pytest.raises(PlanningError, match="unknown path categories"):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_planning_recomputes_patched_version_relationship() -> None:
    source = _advisory_report()
    relationship = source["inspection"]["repositories"][0]["alerts"][0][  # type: ignore[index]
        "package_related_open_pull_requests"
    ][0]
    relationship["target_version"] = "999.0.0"

    with pytest.raises(PlanningError, match="fabricated patched-version"):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_partial_manifest_evidence_makes_plan_partial_and_non_executable() -> None:
    source = _advisory_report()
    manifest_evidence = source["inspection"]["repositories"][0][  # type: ignore[index]
        "manifest_evidence"
    ]
    manifest_evidence["status"] = "partial"
    manifest_evidence["errors"] = [
        {"path": "requirements-dev.txt", "message": "Manifest read failed."}
    ]

    report = plan_from_advisory(source)  # type: ignore[arg-type]

    assert report["status"] == "partial"
    assert report["repositories"][0]["status"] == "partial"
    assert all(
        step["action"] != "validate_then_merge"
        for step in report["repositories"][0]["recommended_order"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("number", True), ("manifest_path_matches", 0)],
)
def test_planning_rejects_type_coerced_reciprocal_fields(
    field: str, value: object
) -> None:
    source = _advisory_report()
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    if field == "number":
        repository["alerts"][0]["number"] = 1
    relationship = repository["pull_requests"][0]["related_alerts"][0]
    relationship[field] = value

    with pytest.raises(PlanningError, match="malformed reciprocal"):
        plan_from_advisory(source)  # type: ignore[arg-type]


@pytest.mark.parametrize("mutation", ["missing_files", "not_applicable_with_edges"])
def test_planning_rejects_incomplete_or_contradictory_manifest_schema(
    mutation: str,
) -> None:
    source = _advisory_report()
    evidence = source["inspection"]["repositories"][0]["manifest_evidence"]  # type: ignore[index]
    if mutation == "missing_files":
        evidence.pop("files")
    else:
        evidence["status"] = "not_applicable"

    with pytest.raises(PlanningError):
        plan_from_advisory(source)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("surface", "value"),
    [
        ("coverage_evidence", []),
        ("patched_version_relation", []),
        ("reciprocal_number", []),
    ],
)
def test_planning_normalizes_malformed_relationship_scalars(
    surface: str, value: object
) -> None:
    source = _advisory_report()
    repository = source["inspection"]["repositories"][0]  # type: ignore[index]
    if surface == "reciprocal_number":
        repository["pull_requests"][0]["related_alerts"][0]["number"] = value
    else:
        repository["alerts"][0]["package_related_open_pull_requests"][0][surface] = value

    with pytest.raises(PlanningError):
        plan_from_advisory(source)  # type: ignore[arg-type]


def test_complete_manifest_evidence_requires_collected_include_targets() -> None:
    source = _advisory_report()
    evidence = source["inspection"]["repositories"][0]["manifest_evidence"]  # type: ignore[index]
    evidence["files"] = [evidence["files"][0]]

    with pytest.raises(PlanningError, match="uncollected included manifests"):
        plan_from_advisory(source)  # type: ignore[arg-type]


@pytest.mark.parametrize("surface", ["include", "relationship"])
def test_planning_normalizes_unhashable_manifest_relation(surface: str) -> None:
    source = _advisory_report()
    evidence = source["inspection"]["repositories"][0]["manifest_evidence"]  # type: ignore[index]
    if surface == "include":
        evidence["files"][0]["includes"][0]["relation"] = []
    else:
        evidence["relationships"][0]["relation"] = []

    with pytest.raises(PlanningError):
        plan_from_advisory(source)  # type: ignore[arg-type]
