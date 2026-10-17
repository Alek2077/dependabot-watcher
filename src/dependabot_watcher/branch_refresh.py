"""Exact, single-use approval for GitHub's PR-branch update, never a PR merge."""
from __future__ import annotations

import copy
import re
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol

from .github import GitHubError, GitHubRequestRejected
from .policy import DEPENDABOT_AUTHORS
from .review_queue import ReviewQueue, ReviewQueueError, _load_queue
from .runtime_state import RuntimeStateError, load_private_json, load_run_state, run_lock, save_private_json, save_run_state
from .review_actions import PendingMaintenanceError, _config_digest, _digest, _require_current_config, _time, _timestamp

_PATH = Path('branch-refresh-reviews.json')
_PENDING = {'executing', 'awaiting_update', 'recovery_required'}
_STATUSES = _PENDING | {'awaiting_confirmation', 'updated', 'invalidated', 'expired', 'closed', 'rejected', 'released'}


class _ClosedPullRequest(ReviewQueueError):
    pass


class BranchRefreshClient(Protocol):
    def fetch_branch_refresh_state(self, repository: str, number: int) -> dict[str, Any]: ...
    def fetch_branch_head(self, repository: str, branch: str) -> str: ...
    def compare_commits(self, repository: str, base: str, head: str) -> str: ...
    def update_pull_request_branch(self, repository: str, number: int, *, head_sha: str) -> None: ...


def _sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', value) is None:
        raise ReviewQueueError('Branch update requires a full commit identity.')
    return value.lower()


def _load(directory: Path) -> dict[str, Any]:
    if not (directory / _PATH).exists() and not (directory / _PATH).is_symlink():
        return {'schema_version': 1, 'reviews': {}}
    value = load_private_json(directory, _PATH, maximum_bytes=1_000_000)
    if (set(value) != {'schema_version', 'reviews'} or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['reviews'], dict) or len(value['reviews']) > 100):
        raise ReviewQueueError('Branch-update history has an invalid schema.')
    sequences: set[int] = set()
    for key, record in value['reviews'].items():
        if (not isinstance(record, dict) or set(record) != {'review_id', 'binding', 'binding_digest', 'selection_digest', 'config_digest',
                'proposal_id', 'version', 'sequence', 'created_at', 'expires_at', 'consented_at', 'status', 'observed_head', 'base_current', 'continuation_request', 'continuation_claimed', 'rejection_status', 'released_at'}
                or not isinstance(key, str) or re.fullmatch(r'refresh_[a-f0-9]{32}', key) is None
                or record['review_id'] != key or not isinstance(record['status'], str) or record['status'] not in _STATUSES
                or not isinstance(record['binding'], dict) or record['binding_digest'] != _digest(record['binding'])
                or set(record['binding']) != {'repository', 'number', 'head_branch', 'head_sha', 'base_branch', 'base_sha'}
                or not isinstance(record['proposal_id'], str) or re.fullmatch(r'proposal_[a-f0-9]{16}', record['proposal_id']) is None
                or type(record['version']) is not int or record['version'] < 0
                or type(record['sequence']) is not int or not 0 < record['sequence'] < 2**63 or record['sequence'] in sequences
                or any(not isinstance(record[name], str) or re.fullmatch(r'[a-f0-9]{64}', record[name]) is None
                       for name in ('selection_digest', 'config_digest'))):
            raise ReviewQueueError('Branch-update history contains an invalid review.')
        sequences.add(record['sequence'])
        binding = record['binding']
        if (not isinstance(binding['repository'], str) or re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', binding['repository']) is None
                or type(binding['number']) is not int or binding['number'] <= 0
                or any(not isinstance(binding[name], str) or not binding[name] or len(binding[name]) > 255
                       for name in ('head_branch', 'base_branch'))):
            raise ReviewQueueError('Branch-update history has an invalid target.')
        _sha(binding['head_sha']); _sha(binding['base_sha'])
        if _time(record['expires_at']) - _time(record['created_at']) != timedelta(minutes=15):
            raise ReviewQueueError('Branch-update confirmation lifetime is invalid.')
        consumed = record['status'] in _PENDING | {'updated', 'closed', 'rejected', 'released'}
        if consumed != (record['consented_at'] is not None):
            raise ReviewQueueError('Branch-update outcome lacks its single-use consent.')
        if consumed and not _time(record['created_at']) <= _time(record['consented_at']) < _time(record['expires_at']):
            raise ReviewQueueError('Branch-update consent is outside its lifetime.')
        if record['status'] == 'updated':
            _sha(record['observed_head'])
            if type(record['base_current']) is not bool:
                raise ReviewQueueError('Updated branch lacks current-base observation.')
        elif record['observed_head'] is not None or record['base_current'] is not None:
            raise ReviewQueueError('Unverified branch update contains success evidence.')
        if record['status'] == 'rejected':
            if type(record['rejection_status']) is not int or record['rejection_status'] not in {400, 401, 403, 404, 405, 409, 422}:
                raise ReviewQueueError('Branch-update refusal lacks its definite HTTP rejection.')
        elif record['rejection_status'] is not None:
            raise ReviewQueueError('Unrejected branch update contains rejection evidence.')
        if record['status'] == 'released':
            if _time(record['released_at']) < _time(record['consented_at']):
                raise ReviewQueueError('Branch-update hold release predates its consent.')
        elif record['released_at'] is not None:
            raise ReviewQueueError('Unreleased branch update contains a release receipt.')
        continuation = record['continuation_request']
        if type(record['continuation_claimed']) is not bool or record['continuation_claimed'] and continuation is None:
            raise ReviewQueueError('Branch-update continuation has an invalid launch claim.')
        if continuation is not None and (record['status'] != 'updated' or not isinstance(continuation, dict)
                or set(continuation) != {'schema_version', 'request_id', 'operation', 'proposal_id', 'version', 'message'}
                or type(continuation['schema_version']) is not int or continuation['schema_version'] != 1
                or not isinstance(continuation['request_id'], str)
                or re.fullmatch(r'decision_[1-9][0-9]{0,18}_' + key[8:], continuation['request_id']) is None
                or continuation['operation'] != 'feedback' or not isinstance(continuation['proposal_id'], str)
                or re.fullmatch(r'proposal_[a-f0-9]{16}', continuation['proposal_id']) is None
                or type(continuation['version']) is not int or continuation['version'] < 0
                or continuation['message'] != _continuation_message(record)):
            raise ReviewQueueError('Branch-update continuation contains invalid saved feedback.')
    return value


def require_no_pending_refresh(directory: Path, repository: str) -> None:
    if any(record['binding']['repository'].casefold() == repository.casefold() and record['status'] in _PENDING
           for record in _load(directory)['reviews'].values()):
        raise PendingMaintenanceError('A PR branch update needs its outcome checked before another repository action. No update will be replayed.')


def branch_refresh_revision(directory: Path) -> str:
    """Collection fence: a local approved branch write invalidates an older read."""
    return _digest({key: record for key, record in _load(directory)['reviews'].items()
                    if record['consented_at'] is not None})


class BranchRefreshReviews:
    def __init__(self, queue: ReviewQueue, client: BranchRefreshClient, config_path: Path) -> None:
        self.queue, self.client, self.config_path = queue, client, config_path

    def snapshot(self) -> list[dict[str, Any]]:
        return copy.deepcopy(sorted(_load(self.queue.directory)['reviews'].values(), key=lambda record: record['sequence']))

    def _available(self, repository: str, *, mutation: bool, idle: bool = False) -> None:
        _require_current_config(self.queue, self.config_path)
        if mutation and (self.queue.config.operation.autonomy != 'approval_required' or not self.queue.config.operation.mutation_enabled):
            raise ReviewQueueError('Enable approval-required mutations before reviewing a PR branch update.')
        if not any(item.name.casefold() == repository.casefold() for item in self.queue.config.repositories):
            raise ReviewQueueError('Branch update repository is not configured.')
        if mutation or idle:
            # Same operation lock serializes admission with supervisor.start.
            from .supervision import SupervisorJobs
            jobs = SupervisorJobs(self.queue)
            state = jobs._load()
            jobs._recover_under_lock(state, repository)
            if any(job['repository'].casefold() == repository.casefold() and job['status'] == 'running'
                   for job in state['jobs'].values()):
                raise ReviewQueueError('Wait for or stop the running investigation before updating its PR branch.')
            if mutation:
                from .review_actions import require_prior_completion
                require_prior_completion(self.queue.directory, repository, self.queue.clock())

    def _live(self, repository: str, number: int, *, observing: bool = False, require_non_draft: bool = False) -> dict[str, Any]:
        value = self.client.fetch_branch_refresh_state(repository, number)
        head, base, author = value.get('head'), value.get('base'), value.get('user')
        if (type(value.get('number')) is not int or value['number'] != number
                or type(value.get('draft')) is not bool or (not observing or require_non_draft) and value['draft'] is not False
                or not isinstance(author, dict) or not isinstance(author.get('login'), str) or author['login'].casefold() not in DEPENDABOT_AUTHORS
                or not isinstance(head, dict) or not isinstance(base, dict)
                or not isinstance(head.get('repo'), dict) or not isinstance(base.get('repo'), dict)
                or str(head['repo'].get('full_name', '')).casefold() != repository.casefold()
                or str(base['repo'].get('full_name', '')).casefold() != repository.casefold()
                or any(not isinstance(part.get('ref'), str) or not part['ref'] or len(part['ref']) > 255 for part in (head, base))):
            raise ReviewQueueError('Branch update requires an open, non-draft, same-repository Dependabot PR.')
        if observing and value.get('state') == 'closed':
            raise _ClosedPullRequest('The PR has closed; no further update or validation will be started.')
        if value.get('state') != 'open' or value.get('merged') is not False:
            raise ReviewQueueError('The PR is no longer open and unmerged.')
        if not observing and value.get('mergeable') is not True:
            raise ReviewQueueError('GitHub has not confirmed a conflict-free branch update. Resolve conflicts or wait for its mergeability check, then review again.')
        configured = next(item for item in self.queue.config.repositories if item.name.casefold() == repository.casefold())
        if base['ref'] != configured.default_branch or head['ref'] == base['ref']:
            raise ReviewQueueError('PR branch update does not match the configured target branch.')
        return {'repository': repository, 'number': number, 'head_branch': head['ref'], 'head_sha': _sha(head.get('sha')),
                'base_branch': base['ref'], 'base_sha': _sha(self.client.fetch_branch_head(repository, base['ref']))}

    def _save(self, state: dict[str, Any]) -> None:
        save_private_json(self.queue.directory, _PATH, state, maximum_bytes=1_000_000)

    def prepare(self, proposal_id: str, version: int) -> dict[str, Any]:
        with run_lock(self.queue.directory, wait_seconds=2):
            plan, selected = self.queue._selection_under_lock(proposal_id, version, self.queue.clock())
            if selected['step']['kind'] != 'pull_request':
                raise ReviewQueueError('Select the candidate PR, not an alert, for the branch-update review.')
            repository = selected['repository']
            self._available(repository, mutation=True)
            require_no_pending_refresh(self.queue.directory, repository)
            binding = self._live(repository, selected['step']['identity']['number'])
            if binding['head_sha'] != selected['step']['identity']['head_sha']:
                raise ReviewQueueError('The PR head changed; collect fresh evidence before reviewing its branch update.')
            if self.client.compare_commits(repository, binding['base_sha'], binding['head_sha']) in {'ahead', 'identical'}:
                raise ReviewQueueError('This PR already includes the current target branch. Continue validation instead.')
            state = _load(self.queue.directory)
            now = self.queue.clock()
            for old in state['reviews'].values():
                if old['status'] == 'awaiting_confirmation' and now >= _time(old['expires_at']):
                    old['status'] = 'expired'
            while len(state['reviews']) >= 100:
                terminal = [old for old in state['reviews'].values() if old['status'] in {'updated', 'closed', 'rejected', 'released', 'invalidated', 'expired'}]
                if not terminal:
                    break
                retired = min(terminal, key=lambda old: old['sequence'])
                del state['reviews'][retired['review_id']]
            if len(state['reviews']) >= 100:
                raise ReviewQueueError('Branch-update history is full; preserve/review history before further actions.')
            for record in state['reviews'].values():
                if record['binding']['repository'].casefold() == repository.casefold() and record['status'] == 'awaiting_confirmation':
                    record['status'] = 'invalidated'
            now = self.queue.clock()
            sequence = max((old['sequence'] for old in state['reviews'].values()), default=0) + 1
            if sequence >= 2**63:
                raise ReviewQueueError('Branch-update history sequence is exhausted.')
            record = {'review_id': 'refresh_' + secrets.token_hex(16), 'sequence': sequence, 'binding': binding, 'binding_digest': _digest(binding),
                      'selection_digest': _digest({'plan_id': plan['plan_id'], 'selected': selected}),
                      'config_digest': _config_digest(self.queue), 'proposal_id': proposal_id, 'version': version,
                      'created_at': _timestamp(now), 'expires_at': _timestamp(now + timedelta(minutes=15)),
                      'consented_at': None, 'status': 'awaiting_confirmation', 'observed_head': None, 'base_current': None,
                      'continuation_request': None, 'continuation_claimed': False, 'rejection_status': None, 'released_at': None}
            state['reviews'][record['review_id']] = record
            self._save(state)
            return copy.deepcopy(record)

    def confirm(self, review_id: str) -> dict[str, Any]:
        with run_lock(self.queue.directory, wait_seconds=2):
            state = _load(self.queue.directory)
            record = self._record(state, review_id)
            if record['status'] != 'awaiting_confirmation':
                raise ReviewQueueError('This branch-update confirmation was already consumed. Check its outcome; do not repeat it.')
            if self.queue.clock() >= _time(record['expires_at']):
                record['status'] = 'expired'; self._save(state)
                raise ReviewQueueError('Branch-update confirmation expired. Review again.')
            binding = record['binding']
            self._available(binding['repository'], mutation=True)
            require_no_pending_refresh(self.queue.directory, binding['repository'])
            plan, selected = self.queue._selection_under_lock(record['proposal_id'], record['version'], self.queue.clock())
            if record['config_digest'] != _config_digest(self.queue) or record['selection_digest'] != _digest({'plan_id': plan['plan_id'], 'selected': selected}):
                record['status'] = 'invalidated'; self._save(state)
                raise ReviewQueueError('Branch-update selection or configuration changed. Review again.')
            live = self._live(binding['repository'], binding['number'])
            if live != binding:
                record['status'] = 'invalidated'; self._save(state)
                raise ReviewQueueError('The PR or target branch changed after review. Review again.')
            _require_current_config(self.queue, self.config_path)
            now = self.queue.clock()
            if not _time(record['created_at']) <= now < _time(record['expires_at']):
                record['status'] = 'expired'; self._save(state)
                raise ReviewQueueError('Branch-update confirmation expired during preflight. Review again.')
            record.update(status='executing', consented_at=_timestamp(now))
            self._save(state)  # Single use before the only remote write.
            try:
                self.client.update_pull_request_branch(binding['repository'], binding['number'], head_sha=binding['head_sha'])
                record['status'] = 'awaiting_update'
            except GitHubRequestRejected as exc:
                record.update(status='rejected', rejection_status=exc.status)
            except (GitHubError, OSError, TimeoutError):
                record['status'] = 'recovery_required'
            self._save(state)
            return copy.deepcopy(record)

    def continue_validation(self, review_id: str) -> tuple[dict[str, Any], bool]:
        """Save one continuation message; only its first caller may auto-start.

        The normal supervisor route still owns fresh collection, compatible
        conversation reuse, account/policy checks and model execution. A crash
        before launch cannot turn a subsequent outcome check into a new grant.
        """
        with run_lock(self.queue.directory, wait_seconds=2):
            state = _load(self.queue.directory)
            record = self._record(state, review_id)
            first = not record['continuation_claimed']
            self._available(record['binding']['repository'], mutation=False, idle=first)
            if first:
                require_no_pending_refresh(self.queue.directory, record['binding']['repository'])
            if record['status'] != 'updated' or record['base_current'] is not True:
                raise ReviewQueueError('Observe an updated PR including its target branch before continuing validation.')
            selected: dict[str, Any] | None = None
            if first:
                binding = record['binding']
                try:
                    live = self._live(binding['repository'], binding['number'], observing=True, require_non_draft=True)
                except _ClosedPullRequest:
                    record.update(status='closed', observed_head=None, base_current=None, continuation_request=None)
                    self._save(state)
                    raise ReviewQueueError('The PR closed before validation continuation. No investigation was started.')
                if (live['head_branch'] != binding['head_branch'] or live['base_branch'] != binding['base_branch']
                        or live['head_sha'] != record['observed_head']):
                    raise ReviewQueueError('The observed PR branch changed before continuation. Collect fresh evidence; no investigation was started.')
                if self.client.compare_commits(binding['repository'], live['base_sha'], live['head_sha']) not in {'ahead', 'identical'}:
                    record.update(base_current=False, continuation_request=None)
                    self._save(state)
                    raise ReviewQueueError('The target advanced before continuation. Review a fresh branch update; no investigation was started.')
                context = self.queue._supervision_context_under_lock(record['binding']['repository'])
                candidates = [item for item in context['proposals'] if item['step']['kind'] == 'pull_request'
                              and item['step']['identity']['number'] == record['binding']['number'] and item['handling'] == 'pending']
                if len(candidates) != 1:
                    raise ReviewQueueError('Refresh the saved queue to locate this PR before continuing validation.')
                selected = candidates[0]
            queue_state = _load_queue(self.queue.directory)
            message = record['continuation_request']
            receipt = message is not None and any(item['request_id'] == message['request_id'] for item in queue_state['receipts'])
            delivered = message is not None and any(item['proposal_id'] == message['proposal_id']
                and any(feedback['request_id'] == message['request_id'] and feedback['message'] == message['message']
                        for feedback in item['feedback']) for item in [*queue_state['proposals'].values(), *queue_state['history']])
            if first and not receipt and not delivered:
                if selected is None:
                    raise ReviewQueueError('Continuation has no current selected PR.')
                sequence = queue_state['next_request_sequence']
                if message is not None and int(message['request_id'].split('_', 2)[1]) != sequence:
                    raise ReviewQueueError('Earlier continuation delivery is no longer recoverable from saved history. Use the normal investigation control deliberately; no automatic launch or update was repeated.')
                message = {'schema_version': 1, 'request_id': f'decision_{sequence}_{review_id[8:]}',
                    'operation': 'feedback', 'proposal_id': selected['proposal_id'], 'version': selected['version'],
                    'message': _continuation_message(record)}
                record['continuation_request'] = message
                self._save(state)  # Recoverable write-ahead identity, not delivery or a start grant.
            # Keep feedback admission and the start claim under one operation
            # lock. A failed queue write can be retried/rebound; a lost reply
            # after the claim cannot grant another automatic model start.
            request = message if receipt or first and not delivered else {
                'schema_version': 1, 'request_id': 'read_' + review_id, 'operation': 'snapshot'}
            if request is None:
                raise ReviewQueueError('Continuation has no saved feedback identity.')
            try:
                response = self.queue._handle_under_lock(request)
            except (ReviewQueueError, RuntimeStateError):
                if first:
                    latest_queue = _load_queue(self.queue.directory)
                    # Under this lock, an unchanged counter proves that this
                    # request did not commit. Clear only that known failure;
                    # uncertain/evicted delivery must never be rebound blindly.
                    if message is not None and latest_queue['next_request_sequence'] == int(message['request_id'].split('_', 2)[1]):
                        record['continuation_request'] = None
                        self._save(state)
                raise
            if first:
                record['continuation_claimed'] = True
                self._save(state)
            return response, first

    @staticmethod
    def _record(state: Mapping[str, Any], review_id: str) -> dict[str, Any]:
        if not isinstance(review_id, str) or re.fullmatch(r'refresh_[a-f0-9]{32}', review_id) is None or review_id not in state['reviews']:
            raise ReviewQueueError('Branch-update review is unavailable.')
        return state['reviews'][review_id]

    def release_hold(self, review_id: str, binding_digest: str) -> dict[str, Any]:
        """Explicit local decision, NOT cancellation or proof of non-application.

        GitHub supplies no asynchronous update cancellation/status handle. An
        unchanged or rewritten head cannot prove that a request will never
        land. Retain that uncertainty without an irreversible workflow fence.
        """
        with run_lock(self.queue.directory, wait_seconds=2):
            state = _load(self.queue.directory)
            record = self._record(state, review_id)
            self._available(record['binding']['repository'], mutation=False)
            if record['binding_digest'] != binding_digest or record['status'] not in _PENDING | {'released'}:
                raise ReviewQueueError('The pending update changed. Refresh its exact saved outcome before releasing its hold.')
            if record['status'] != 'released':
                self._available(record['binding']['repository'], mutation=False, idle=True)
                released_at = _timestamp(max(self.queue.clock(), _time(record['consented_at'])))
                # Releasing uncertainty is not acceptance of pre-update
                # evidence. Revoke its full lineage before lifting the fence;
                # a crash between writes conservatively retains the hold.
                from .reconciliation import _advance_plan_lineage
                run_state = load_run_state(self.queue.directory)
                previous = run_state['latest_plan'] or {}
                invalidated, overflow = _advance_plan_lineage(previous)
                run_state['latest_plan'] = {'plan_id': None, 'supersedes_plan_id': previous.get('plan_id'),
                    'invalidated_plan_ids': invalidated, 'lineage_overflow': overflow,
                    'revoked_reason': 'branch_update_hold_released', 'evidence_revision': previous.get('evidence_revision'),
                    'recorded_at': released_at}
                save_run_state(self.queue.directory, run_state)
                record.update(status='released', released_at=released_at)
                self._save(state)
            return copy.deepcopy(record)

    def check(self, review_id: str) -> dict[str, Any]:
        with run_lock(self.queue.directory, wait_seconds=2):
            state = _load(self.queue.directory)
            record = self._record(state, review_id)
            binding = record['binding']
            self._available(binding['repository'], mutation=False)
            if record['status'] not in _PENDING:
                return copy.deepcopy(record)
            try:
                live = self._live(binding['repository'], binding['number'], observing=True)
            except _ClosedPullRequest:
                record['status'] = 'closed'; self._save(state)
                return copy.deepcopy(record)
            if live['head_branch'] != binding['head_branch'] or live['base_branch'] != binding['base_branch']:
                raise ReviewQueueError('PR branch identity changed; the update outcome remains uncertain.')
            if live['head_sha'] != binding['head_sha']:
                if self.client.compare_commits(binding['repository'], binding['head_sha'], live['head_sha']) not in {'ahead', 'identical'}:
                    raise ReviewQueueError('The old PR head is not preserved. No update was replayed; inspect the remote branch.')
                record.update(status='updated', observed_head=live['head_sha'],
                              base_current=self.client.compare_commits(binding['repository'], live['base_sha'], live['head_sha']) in {'ahead', 'identical'})
                self._save(state)
            return copy.deepcopy(record)

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        operation = request.get('operation')
        fields = {'schema_version', 'request_id', 'operation'} | ({'proposal_id', 'version'} if operation == 'prepare_branch_refresh' else {'review_id'})
        if operation == 'release_branch_refresh':
            fields.add('binding_digest')
        if (set(request) != fields or type(request.get('schema_version')) is not int or request['schema_version'] != 1
                or not isinstance(request.get('request_id'), str) or re.fullmatch(r'action_[a-f0-9]{32}', request['request_id']) is None
                or operation not in {'prepare_branch_refresh', 'confirm_branch_refresh', 'check_branch_refresh', 'continue_branch_refresh', 'release_branch_refresh'}):
            raise ReviewQueueError('Branch-update request has an invalid fixed contract.')
        if operation == 'continue_branch_refresh':
            response, first = self.continue_validation(request['review_id'])
            response.update(request_id=request['request_id'], continuation_start_allowed=first)
            return response
        if operation == 'prepare_branch_refresh':
            record = self.prepare(request['proposal_id'], request['version'])
        elif operation == 'confirm_branch_refresh':
            record = self.confirm(request['review_id'])
        elif operation == 'release_branch_refresh':
            record = self.release_hold(request['review_id'], request['binding_digest'])
        else:
            record = self.check(request['review_id'])
        return {'schema_version': 1, 'request_id': request['request_id'], 'status': 'ok', 'branch_refresh': record,
                'capabilities': {'model_calls': False, 'github_mutations': operation == 'confirm_branch_refresh'}}


def _continuation_message(record: Mapping[str, Any]) -> str:
    return (f"Continue the saved investigation after the approved branch update of PR #{record['binding']['number']}. "
            f"Updated head observed: {record['observed_head']}. Refresh evidence and validate against the current target branch. "
            "Reuse relevant prior investigation and receipts; explain genuinely necessary repeated checks. "
            "No further remote branch update or merge is authorized.")
