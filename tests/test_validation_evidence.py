from __future__ import annotations

import json
from pathlib import Path

import pytest

from dependabot_watcher.runtime_state import RuntimeStateError, save_private_json
from dependabot_watcher.validation_evidence import ValidationEvidence
from dependabot_watcher.validation_mcp import _wire_receipt


def receipt(**changes):
    return {'job_id': 'job_' + 'a' * 32, 'operation_id': 'validation_' + 'b' * 32,
        'head_sha': 'c' * 40, 'source_tree_sha': 'd' * 40,
        'command_sha256': 'e' * 64, 'output_sha256': 'f' * 64,
        'exit_code': 0, 'tracked_source_changed': False, **changes}


def test_history_retains_provenance_without_claiming_fresh_or_equivalent_environment(tmp_path: Path) -> None:
    evidence = ValidationEvidence(tmp_path / 'evidence', {'network_authorized': False})
    evidence.record(receipt())
    history = evidence.historical('d' * 40)
    assert history['fresh_execution'] is False
    assert history['environment_equivalence'] == 'not_established'
    assert history['results'][0]['head_sha'] == 'c' * 40
    assert history['results'][0]['operation_id'] == 'validation_' + 'b' * 32
    assert 'command' not in history['results'][0] and 'output' not in history['results'][0]
    assert evidence.historical('0' * 40)['results'] == []
    changed_policy = ValidationEvidence(tmp_path / 'evidence', {'network_authorized': True})
    assert changed_policy.historical('d' * 40)['results'] == []
    other_repository = ValidationEvidence(tmp_path / 'other', {'network_authorized': False})
    assert other_repository.historical('d' * 40)['results'] == []


@pytest.mark.parametrize('change', [{'exit_code': 1}, {'tracked_source_changed': True}])
def test_failed_or_source_changed_execution_is_not_offered_for_reuse(tmp_path: Path, change: dict) -> None:
    evidence = ValidationEvidence(tmp_path / 'evidence', {})
    evidence.record(receipt(**change))
    assert evidence.historical('d' * 40)['results'] == []


def test_history_is_bounded_and_new_command_result_supersedes_its_prior_result(tmp_path: Path) -> None:
    evidence = ValidationEvidence(tmp_path / 'evidence', {})
    for index in range(40):
        evidence.record(receipt(command_sha256=f'{index:064x}'))
    history = evidence._read('d' * 40)
    assert len(history) == 32
    assert history[0]['command_sha256'] == f'{8:064x}'
    evidence.record(receipt(command_sha256=f'{39:064x}', head_sha='0' * 40))
    history = evidence._read('d' * 40)
    assert len(history) == 32 and history[-1]['head_sha'] == '0' * 40
    preview = evidence.historical('d' * 40)
    assert len(preview['results']) == 8 and preview['results_total'] == 32
    assert preview['results_preview_truncated'] is True
    wire = _wire_receipt({'validation_id': 'validation_' + 'a' * 32,
        'job_id': 'job_' + 'b' * 32, 'context_digest': 'c' * 64,
        'head_sha': 'd' * 40, 'historical_validation': preview,
        'omissions': [{'path': 'synthetic/' + str(index), 'reason': 'nonordinary'} for index in range(200)]},
        'mcp_' + 'f' * 32)
    assert wire.get('status') != 'blocked'
    assert wire['historical_validation']['results_total'] == 32
    assert len(json.dumps(wire).encode()) < 16384


@pytest.mark.parametrize('field,value', [
    ('source_tree_sha', '0' * 40), ('exit_code', True), ('command_sha256', '../outside'),
    ('tracked_source_changed', True), ('observed_at', 'not-a-time'),
    ('observed_at', '2026-10-11T00:00:00'),
])
def test_malformed_history_is_not_trusted(tmp_path: Path, field: str, value: object) -> None:
    evidence = ValidationEvidence(tmp_path / 'evidence', {})
    evidence.record(receipt())
    path = evidence.directory / ('d' * 40 + '.json')
    state = json.loads(path.read_text())
    state['results'][0][field] = value
    save_private_json(evidence.directory, Path(path.name), state)
    assert evidence.historical('d' * 40)['results'] == []


def test_invalid_tree_cannot_escape_evidence_directory(tmp_path: Path) -> None:
    evidence = ValidationEvidence(tmp_path / 'evidence', {})
    with pytest.raises(RuntimeStateError):
        evidence.historical('../outside')


def test_linked_history_is_not_read_or_overwritten(tmp_path: Path) -> None:
    evidence = ValidationEvidence(tmp_path / 'evidence', {})
    outside = tmp_path / 'unrelated.json'
    outside.write_text('unrelated data')
    (evidence.directory / ('d' * 40 + '.json')).symlink_to(outside)
    assert evidence.historical('d' * 40)['results'] == []
    evidence.record(receipt())  # Atomic replacement must not follow the link.
    assert outside.read_text() == 'unrelated data'
    assert not (evidence.directory / ('d' * 40 + '.json')).is_symlink()
    assert len(evidence.historical('d' * 40)['results']) == 1


def test_uppercase_head_is_canonicalized_without_discarding_history(tmp_path: Path) -> None:
    evidence = ValidationEvidence(tmp_path / 'evidence', {})
    evidence.record(receipt(head_sha='C' * 40))
    assert evidence.historical('d' * 40)['results'][0]['head_sha'] == 'c' * 40


def test_unknown_top_level_history_fields_are_not_trusted(tmp_path: Path) -> None:
    evidence = ValidationEvidence(tmp_path / 'evidence', {})
    evidence.record(receipt())
    path = Path('d' * 40 + '.json')
    state = json.loads((evidence.directory / path).read_text())
    save_private_json(evidence.directory, path, {**state, 'future_field': 'unknown'})
    assert evidence.historical('d' * 40)['results'] == []
