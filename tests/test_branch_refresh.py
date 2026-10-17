from __future__ import annotations

import io
import json
from datetime import timedelta
from typing import Any, Sequence

import pytest

from dependabot_watcher.branch_refresh import BranchRefreshReviews, require_no_pending_refresh
from dependabot_watcher.github import GhCliClient, GitHubError, GitHubRequestRejected
from dependabot_watcher.reconciliation import ReconciliationError, _collection_window, reconcile
from dependabot_watcher.review_actions import ActionReviews, require_prior_completion
from dependabot_watcher.review_bridge import exchange
from dependabot_watcher.review_queue import ReviewQueueError, _load_queue, _QUEUE_PATH
from dependabot_watcher.runtime_state import RuntimeStateError, load_run_state, save_private_json
from dependabot_watcher.supervision import SupervisorJobs
from test_execution import BASE_SHA, HEAD_SHA, ExecutionClient
from test_review_actions import action_case
from test_reconciliation import _inspection


class BranchClient(ExecutionClient):
    def __init__(self) -> None:
        super().__init__()
        self.head = HEAD_SHA
        self.base = BASE_SHA
        self.calls: list[tuple[str, int, str]] = []
        self.fail_write = False
        self.finish_write = True
        self.closed = False
        self.fork = False
        self.preserve = True
        self.includes_base = True
        self.author = 'dependabot[bot]'
        self.draft = False

    def fetch_branch_refresh_state(self, repository: str, number: int) -> dict[str, Any]:
        return {'number': number, 'state': 'closed' if self.closed else 'open', 'merged': False, 'draft': self.draft, 'mergeable': True,
                'user': {'login': self.author},
                'head': {'sha': self.head, 'ref': 'dependabot/example', 'repo': {'full_name': 'elsewhere/fork' if self.fork else repository}},
                'base': {'sha': self.base, 'ref': 'main', 'repo': {'full_name': repository}}}

    def fetch_branch_head(self, repository: str, branch: str) -> str:
        return self.base

    def compare_commits(self, repository: str, base: str, head: str) -> str:
        if head == HEAD_SHA:
            return 'diverged'
        return 'ahead' if (self.preserve if base == HEAD_SHA else self.includes_base) else 'diverged'

    def update_pull_request_branch(self, repository: str, number: int, *, head_sha: str) -> None:
        self.calls.append((repository, number, head_sha))
        if self.finish_write:
            self.head = 'c' * 40
        if self.fail_write:
            raise GitHubError('synthetic lost acknowledgement')


@pytest.fixture
def branch_case(action_case: dict[str, Any]) -> dict[str, Any]:
    client = BranchClient()
    return {**action_case, 'client': client,
            'controller': BranchRefreshReviews(action_case['queue'], client, action_case['config_path'])}


def prepare(case: dict[str, Any]) -> dict[str, Any]:
    return case['controller'].prepare(case['selected']['proposal_id'], case['selected']['version'])


def test_exact_update_then_single_use_validation_continuation(branch_case: dict[str, Any]) -> None:
    controller, client, queue = (branch_case[name] for name in ('controller', 'client', 'queue'))
    record = prepare(branch_case)
    assert record['binding']['head_sha'] == HEAD_SHA and record['binding']['base_sha'] == BASE_SHA
    assert client.calls == []
    assert controller.confirm(record['review_id'])['status'] == 'awaiting_update'
    with pytest.raises(ReviewQueueError, match='already consumed'):
        controller.confirm(record['review_id'])
    observed = controller.check(record['review_id'])
    assert observed['status'] == 'updated' and observed['base_current'] is True
    assert observed['observed_head'] == 'c' * 40 and client.merges == []
    first, allowed = controller.continue_validation(record['review_id'])
    again, allowed_again = controller.continue_validation(record['review_id'])
    assert allowed is True and allowed_again is False and again['replayed'] is True
    assert len(first['proposals'][0]['feedback']) == len(again['proposals'][0]['feedback']) == 1
    assert 'No further remote branch update or merge' in first['proposals'][0]['feedback'][0]['message']
    assert client.calls == [('example-org/example-app', 42, HEAD_SHA)]
    assert len(controller.snapshot()) == 1
    require_no_pending_refresh(queue.directory, 'example-org/example-app')


@pytest.mark.parametrize('changed', ['head', 'base', 'config', 'expired'])
def test_changed_or_expired_review_cannot_write(branch_case: dict[str, Any], changed: str) -> None:
    record = prepare(branch_case)
    if changed == 'config':
        path = branch_case['config_path']
        path.write_text(path.read_text().replace('mutation_enabled = true', 'mutation_enabled = false'))
    elif changed == 'expired':
        old = branch_case['queue'].clock()
        branch_case['queue'].clock = lambda: old + timedelta(minutes=16)
    else:
        setattr(branch_case['client'], changed, 'd' * 40)
    with pytest.raises(ReviewQueueError):
        branch_case['controller'].confirm(record['review_id'])
    assert branch_case['client'].calls == []


def test_lost_ack_is_observed_without_replaying_write(branch_case: dict[str, Any]) -> None:
    controller, client = branch_case['controller'], branch_case['client']
    record = prepare(branch_case)
    client.fail_write = True
    assert controller.confirm(record['review_id'])['status'] == 'recovery_required'
    assert controller.check(record['review_id'])['status'] == 'updated'
    assert len(client.calls) == 1 and client.merges == []


def test_pending_update_fences_jobs_and_merge_until_observed(branch_case: dict[str, Any]) -> None:
    queue, controller, client = (branch_case[name] for name in ('queue', 'controller', 'client'))
    jobs = SupervisorJobs(queue)
    prepared_job = jobs.prepare('example-org/example-app')
    record = prepare(branch_case)
    client.finish_write = False
    controller.confirm(record['review_id'])
    assert controller.check(record['review_id'])['status'] == 'awaiting_update'
    for action in (lambda: jobs.prepare('example-org/example-app'), lambda: jobs.start(prepared_job['job_id']),
                   lambda: require_prior_completion(queue.directory, 'example-org/example-app', queue.clock())):
        with pytest.raises(ReviewQueueError, match='outcome checked'):
            action()
    client.head = 'c' * 40
    controller.check(record['review_id'])
    require_no_pending_refresh(queue.directory, 'example-org/example-app')


def test_running_investigation_blocks_branch_review(branch_case: dict[str, Any]) -> None:
    jobs = SupervisorJobs(branch_case['queue'])
    job = jobs.prepare('example-org/example-app')
    jobs.start(job['job_id'])
    try:
        with pytest.raises(ReviewQueueError, match='running investigation'):
            prepare(branch_case)
        assert branch_case['client'].calls == []
    finally:
        jobs.release_owner(job['job_id'])


@pytest.mark.parametrize('case', ['fork', 'closed', 'base_moved', 'lost_head'])
def test_observation_never_claims_unproved_current_base(branch_case: dict[str, Any], case: str) -> None:
    controller, client = branch_case['controller'], branch_case['client']
    if case == 'fork':
        client.fork = True
        with pytest.raises(ReviewQueueError, match='same-repository'):
            prepare(branch_case)
        return
    record = prepare(branch_case)
    controller.confirm(record['review_id'])
    if case == 'closed':
        client.closed = True
        assert controller.check(record['review_id'])['status'] == 'closed'
        require_no_pending_refresh(branch_case['queue'].directory, 'example-org/example-app')
    elif case == 'base_moved':
        client.includes_base = False
        assert controller.check(record['review_id'])['base_current'] is False
        with pytest.raises(ReviewQueueError, match='including its target'):
            controller.continue_validation(record['review_id'])
    else:
        client.preserve = False
        with pytest.raises(ReviewQueueError, match='old PR head'):
            controller.check(record['review_id'])
    assert len(client.calls) == 1 and client.merges == []


def test_bridge_routes_update_without_accepting_client_bindings(branch_case: dict[str, Any]) -> None:
    actions = ActionReviews(branch_case['queue'], branch_case['client'], config_path=branch_case['config_path'])
    request = {'schema_version': 1, 'request_id': 'action_' + 'd' * 32, 'operation': 'prepare_branch_refresh',
               'proposal_id': branch_case['selected']['proposal_id'], 'version': branch_case['selected']['version']}
    output = io.BytesIO()
    assert exchange(branch_case['queue'], io.BytesIO(json.dumps(request).encode()), output, actions=actions) == 0
    response = json.loads(output.getvalue())
    assert response['branch_refresh']['status'] == 'awaiting_confirmation'
    assert response['capabilities'] == {'model_calls': False, 'github_mutations': False}
    request['head_sha'] = 'e' * 40
    with pytest.raises(ReviewQueueError, match='fixed contract'):
        branch_case['controller'].handle(request)


def test_github_transport_uses_expected_head_and_never_merge_endpoint() -> None:
    captured: list[list[str]] = []
    def runner(command: Sequence[str], timeout: int) -> str:
        captured.append(list(command))
        return 'HTTP/2.0 202 Accepted\r\nContent-Type: application/json\r\n\r\n' + json.dumps({'message': 'Updating pull request branch.'})
    client = GhCliClient(runner=runner)
    client.update_pull_request_branch('example-org/example-app', 42, head_sha=HEAD_SHA)
    assert captured == [['gh', 'api', '--include', '--method', 'PUT', '/repos/example-org/example-app/pulls/42/update-branch', '-f', 'expected_head_sha=' + HEAD_SHA]]


def test_definite_rejection_is_terminal_and_does_not_block_other_actions(branch_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    controller, client = branch_case['controller'], branch_case['client']
    record = prepare(branch_case)
    def rejected(repository: str, number: int, *, head_sha: str) -> None:
        client.calls.append((repository, number, head_sha))
        raise GitHubRequestRejected(422)
    monkeypatch.setattr(client, 'update_pull_request_branch', rejected)
    result = controller.confirm(record['review_id'])
    assert result['status'] == 'rejected' and result['rejection_status'] == 422
    assert controller.check(record['review_id']) == result
    require_no_pending_refresh(branch_case['queue'].directory, 'example-org/example-app')
    with pytest.raises(ReviewQueueError, match='already consumed'):
        controller.confirm(record['review_id'])
    assert len(client.calls) == 1 and client.head == HEAD_SHA


def test_history_order_uses_sequence_not_random_ids_or_wall_clock(branch_case: dict[str, Any]) -> None:
    first = prepare(branch_case)
    old = branch_case['queue'].clock()
    branch_case['queue'].clock = lambda: old - timedelta(minutes=1)
    second = prepare(branch_case)
    records = branch_case['controller'].snapshot()
    assert [item['review_id'] for item in records] == [first['review_id'], second['review_id']]
    assert records[0]['status'] == 'invalidated' and records[1]['status'] == 'awaiting_confirmation'
    assert first['sequence'] < second['sequence']


@pytest.mark.parametrize('lost_ack', [False, True])
def test_approved_branch_write_fences_an_inflight_collection(branch_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, lost_ack: bool) -> None:
    controller, queue = branch_case['controller'], branch_case['queue']
    record = prepare(branch_case)
    branch_case['client'].fail_write = lost_ack
    def collect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        controller.confirm(record['review_id'])
        return _inspection()
    monkeypatch.setattr('dependabot_watcher.reconciliation.inspect', collect)
    with pytest.raises(ReconciliationError, match='changed during collection'):
        with _collection_window(queue.config, branch_case['client'], queue.directory, queue.clock()):
            pytest.fail('Stale collection was allowed to commit across an approved branch write.')
    assert len(branch_case['client'].calls) == 1


@pytest.mark.parametrize('author', ['dependabot', 'Dependabot[bot]', 'APP/DEPENDABOT'])
def test_update_uses_shared_dependabot_author_policy(branch_case: dict[str, Any], author: str) -> None:
    branch_case['client'].author = author
    assert prepare(branch_case)['status'] == 'awaiting_confirmation'


@pytest.mark.parametrize('change', ['closed', 'base', 'head', 'draft'])
def test_first_continuation_rechecks_remote_state(branch_case: dict[str, Any], change: str) -> None:
    controller, client = branch_case['controller'], branch_case['client']
    record = prepare(branch_case)
    controller.confirm(record['review_id']); controller.check(record['review_id'])
    if change == 'closed':
        client.closed = True
    elif change == 'base':
        client.includes_base = False
    elif change == 'draft':
        client.draft = True
    else:
        client.head = 'd' * 40
    with pytest.raises(ReviewQueueError, match='before continuation|before validation continuation|non-draft'):
        controller.continue_validation(record['review_id'])
    saved = controller.snapshot()[0]
    assert saved['continuation_claimed'] is False and saved['continuation_request'] is None
    assert len(client.calls) == 1


def test_claimed_continuation_is_readable_during_its_running_investigation(branch_case: dict[str, Any]) -> None:
    controller, queue = branch_case['controller'], branch_case['queue']
    record = prepare(branch_case)
    controller.confirm(record['review_id']); controller.check(record['review_id'])
    controller.continue_validation(record['review_id'])
    jobs = SupervisorJobs(queue)
    job = jobs.prepare('example-org/example-app'); jobs.start(job['job_id'])
    try:
        response, allowed = controller.continue_validation(record['review_id'])
        assert allowed is False and len(response['proposals'][0]['feedback']) == 1
    finally:
        jobs.release_owner(job['job_id'])


@pytest.mark.parametrize('after_write', [False, True])
def test_failed_continuation_delivery_recovers_without_stale_sequence_or_duplicate_feedback(
    branch_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, after_write: bool,
) -> None:
    controller, queue = branch_case['controller'], branch_case['queue']
    record = prepare(branch_case)
    controller.confirm(record['review_id']); controller.check(record['review_id'])
    original = queue._handle_under_lock
    def fail(request: dict[str, Any]) -> dict[str, Any]:
        if after_write:
            original(request)
        raise RuntimeStateError('Synthetic lost queue acknowledgement')
    monkeypatch.setattr(queue, '_handle_under_lock', fail)
    with pytest.raises(RuntimeStateError):
        controller.continue_validation(record['review_id'])
    saved = controller.snapshot()[0]
    assert (saved['continuation_request'] is not None) is after_write
    assert saved['continuation_claimed'] is False
    monkeypatch.setattr(queue, '_handle_under_lock', original)
    # Other local decisions may retire the reserved sequence or evict its
    # generic receipt. Durable branch delivery/claim must still recover.
    frame = queue.handle({'schema_version': 1, 'request_id': 'read_recover', 'operation': 'snapshot'})
    for index in range(100):
        selected = frame['proposals'][0]
        frame = queue.handle({'schema_version': 1, 'request_id': f"decision_{frame['next_request_sequence']}_other",
            'operation': 'reject' if index % 2 == 0 else 'resume', 'proposal_id': selected['proposal_id'], 'version': selected['version']})
    response, allowed = controller.continue_validation(record['review_id'])
    assert allowed is True and len(response['proposals'][0]['feedback']) == 1
    assert controller.snapshot()[0]['continuation_claimed'] is True
    assert controller.continue_validation(record['review_id'])[1] is False


def test_ambiguous_evicted_continuation_stops_instead_of_duplicating_feedback(branch_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    controller, queue = branch_case['controller'], branch_case['queue']
    record = prepare(branch_case)
    controller.confirm(record['review_id']); controller.check(record['review_id'])
    original = queue._handle_under_lock
    def lost(request: dict[str, Any]) -> dict[str, Any]:
        original(request)
        raise RuntimeStateError('Synthetic acknowledgement lost after committed feedback')
    monkeypatch.setattr(queue, '_handle_under_lock', lost)
    with pytest.raises(RuntimeStateError):
        controller.continue_validation(record['review_id'])
    monkeypatch.setattr(queue, '_handle_under_lock', original)
    state = _load_queue(queue.directory)
    # Boundary fixture: bounded generic receipts and proposal history have
    # both been evicted after the write. Absence is no proof of non-delivery.
    state['receipts'] = []; state['history'] = []
    for proposal in state['proposals'].values():
        proposal['feedback'] = []
    save_private_json(queue.directory, _QUEUE_PATH, state)
    with pytest.raises(ReviewQueueError, match='no longer recoverable'):
        controller.continue_validation(record['review_id'])
    assert controller.snapshot()[0]['continuation_claimed'] is False
    assert _load_queue(queue.directory)['proposals'] == state['proposals']
    assert len(branch_case['client'].calls) == 1


@pytest.mark.parametrize('changed', ['unchanged', 'rewritten'])
def test_explicit_hold_release_preserves_uncertainty_and_revokes_stale_plan(branch_case: dict[str, Any], changed: str) -> None:
    controller, client, queue = (branch_case[name] for name in ('controller', 'client', 'queue'))
    record = prepare(branch_case)
    client.finish_write = False; client.fail_write = True
    controller.confirm(record['review_id'])
    if changed == 'rewritten':
        client.head = 'd' * 40; client.preserve = False
        with pytest.raises(ReviewQueueError, match='old PR head'):
            controller.check(record['review_id'])
    else:
        assert controller.check(record['review_id'])['status'] == 'recovery_required'
    with pytest.raises(ReviewQueueError, match='pending update changed'):
        controller.release_hold(record['review_id'], '0' * 64)
    original_plan = load_run_state(queue.directory)['latest_plan']['plan_id']
    result = controller.release_hold(record['review_id'], record['binding_digest'])
    assert result['status'] == 'released' and result['released_at'] is not None
    assert result['observed_head'] is None and result['continuation_claimed'] is False
    require_no_pending_refresh(queue.directory, 'example-org/example-app')
    latest = load_run_state(queue.directory)['latest_plan']
    assert latest['plan_id'] is None and original_plan in latest['invalidated_plan_ids']
    assert controller.release_hold(record['review_id'], record['binding_digest']) == result
    with pytest.raises(ReviewQueueError, match='Observe an updated PR'):
        controller.continue_validation(record['review_id'])
    assert len(client.calls) == 1 and client.merges == []


def test_fresh_unchanged_collection_restores_plan_after_hold_release(branch_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    controller, client, queue = (branch_case[name] for name in ('controller', 'client', 'queue'))
    monkeypatch.setattr('dependabot_watcher.reconciliation.inspect', lambda *args, **kwargs: _inspection())
    now = queue.clock()
    first = reconcile(queue.config, client, config_path=branch_case['config_path'], state_directory=queue.directory, clock=lambda: now)
    frame = queue.handle({'schema_version': 1, 'request_id': 'read_collected', 'operation': 'snapshot'})
    branch_case['selected'] = frame['proposals'][0]
    record = prepare(branch_case)
    client.finish_write = False; client.fail_write = True
    controller.confirm(record['review_id'])
    controller.release_hold(record['review_id'], record['binding_digest'])
    assert load_run_state(queue.directory)['latest_plan']['plan_id'] is None
    fresh = reconcile(queue.config, client, config_path=branch_case['config_path'], state_directory=queue.directory,
                      clock=lambda: now + timedelta(minutes=1))
    assert fresh['evidence_revision'] == first['evidence_revision']
    assert fresh['outcome'] == 'action_required' and fresh['plan'] is not None
    restored = load_run_state(queue.directory)['latest_plan']
    assert restored['plan_id'] is not None and 'revoked_reason' not in restored
    # Replaying the local release receipt must not revoke this fresh plan.
    jobs = SupervisorJobs(queue)
    job = jobs.prepare('example-org/example-app'); jobs.start(job['job_id'])
    try:
        assert controller.release_hold(record['review_id'], record['binding_digest'])['status'] == 'released'
        assert load_run_state(queue.directory)['latest_plan'] == restored
    finally:
        jobs.release_owner(job['job_id'])
