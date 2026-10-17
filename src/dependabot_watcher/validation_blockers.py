"""Bounded display-only validation blockers; never configuration or action authority."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .runtime_state import RuntimeStateError, load_private_json


def read_validation_blocker(state: Path, job: Mapping[str, Any], *, include_cleared: bool = False) -> dict[str, Any] | None:
    relative = Path('supervisor-validation') / job['job_id'] / 'resource-blocker.json'
    path = state / relative
    if not path.exists() and not path.is_symlink():
        return None
    record = load_private_json(state, relative, maximum_bytes=4096)
    if (set(record) != {'job_id', 'context_digest', 'active', 'maximum_bytes', 'minimum_free_bytes',
                        'allocated_bytes_after_cleanup'}
            or record['job_id'] != job['job_id'] or record['context_digest'] != job['context_digest']
            or type(record['active']) is not bool
            or any(type(record[name]) is not int or not 0 <= record[name] <= 2**63 - 1
                   for name in ('maximum_bytes', 'minimum_free_bytes', 'allocated_bytes_after_cleanup'))
            or not 1 <= record['maximum_bytes'] <= 16 * 1024**3
            or not 1 <= record['minimum_free_bytes'] <= 64 * 1024**3):
        raise RuntimeStateError('Validation resource blocker does not match its saved job.')
    return record if record['active'] or include_cleared else None
