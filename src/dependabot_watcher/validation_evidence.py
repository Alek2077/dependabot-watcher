"""Bounded historical execution evidence, never a fresh-result substitute.

The index is controller-owned and not mounted in validation sandboxes. It stores
no commands or output. Matching source/policy narrows relevance; it does not prove
that host tools, external services or time-dependent inputs remain unchanged.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .runtime_state import RuntimeStateError, ensure_private_directory, load_private_json, save_private_json


class ValidationEvidence:
    """Caller holds the repository validation lease for reads and updates."""

    def __init__(self, directory: Path, policy: Mapping[str, Any]) -> None:
        self.directory = ensure_private_directory(directory)
        self.policy_digest = hashlib.sha256(json.dumps(dict(policy), sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()

    def _read(self, tree: str) -> list[dict[str, Any]]:
        if re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', tree) is None:
            raise RuntimeStateError('Historical validation tree identity is invalid.')
        try:
            state = load_private_json(self.directory, Path(tree + '.json'), maximum_bytes=65536)
        except (OSError, RuntimeStateError):
            return []  # Optional evidence cannot turn a fresh check into a failure.
        results = state.get('results')
        if (set(state) != {'schema_version', 'results'}
                or type(state.get('schema_version')) is not int or state['schema_version'] != 1
                or not isinstance(results, list) or len(results) > 32):
            return []
        for result in results:
            if (not isinstance(result, dict) or set(result) != {
                    'job_id', 'operation_id', 'head_sha', 'source_tree_sha', 'command_sha256',
                    'output_sha256', 'policy_digest', 'observed_at', 'exit_code', 'tracked_source_changed'}
                    or result.get('source_tree_sha') != tree
                    or type(result.get('exit_code')) is not int or result['exit_code'] != 0
                    or result.get('tracked_source_changed') is not False):
                return []
            for key, pattern in (
                ('job_id', r'job_[a-f0-9]{32}'), ('operation_id', r'validation_[a-f0-9]{32}'),
                ('head_sha', r'[a-f0-9]{40}|[a-f0-9]{64}'),
                ('command_sha256', r'[a-f0-9]{64}'), ('output_sha256', r'[a-f0-9]{64}'),
                ('policy_digest', r'[a-f0-9]{64}'),
            ):
                value = result.get(key)
                if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
                    return []
            try:
                observed = datetime.fromisoformat(result['observed_at'])
                if observed.utcoffset() is None:
                    return []
            except (ValueError, TypeError):
                return []
        return results

    def historical(self, tree: str) -> dict[str, Any]:
        results = [result for result in self._read(tree)
                   if result['policy_digest'] == self.policy_digest]
        return {'status': 'historical_only', 'fresh_execution': False,
            'environment_equivalence': 'not_established',
            'results': results[-8:], 'results_total': len(results),
            'results_preview_truncated': len(results) > 8}

    def record(self, receipt: Mapping[str, Any]) -> None:
        if receipt.get('exit_code') != 0 or receipt.get('tracked_source_changed') is not False:
            return
        tree = receipt['source_tree_sha']
        result = {name: receipt[name] for name in ('job_id', 'operation_id', 'head_sha',
            'source_tree_sha', 'command_sha256', 'output_sha256', 'exit_code', 'tracked_source_changed')}
        result['head_sha'] = result['head_sha'].lower()
        result.update(policy_digest=self.policy_digest, observed_at=datetime.now(timezone.utc).isoformat())
        results = self._read(tree)
        results = [previous for previous in results
                   if (previous['command_sha256'], previous['policy_digest'])
                   != (result['command_sha256'], result['policy_digest'])]
        save_private_json(self.directory, Path(tree + '.json'),
                          {'schema_version': 1, 'results': (results + [result])[-32:]})
