"""One opt-in real supervisor turn after a synthetic exact branch update."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import pytest

from supervisor_context import bind, configure, git, live, policy_workspace, synthetic_fixture, write
from test_review_actions import action_case
from dependabot_watcher.branch_refresh import BranchRefreshReviews
from dependabot_watcher.review_queue import ReviewQueue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model')
    parser.add_argument('--reasoning', choices=('minimal', 'low', 'medium', 'high', 'xhigh'))
    parser.add_argument('--account', choices=('chatgpt', 'apiKey'))
    args = parser.parse_args()
    if not args.run_live:
        print('Preview: one real supervisor turn after a branch-update/continuation using synthetic GitHub transport.')
        return
    if not all((args.model, args.reasoning, args.account)):
        parser.error('Explicit model, reasoning and account are required')
    if args.output.exists():
        parser.error('Use a new evidence directory; preserve previous attempts')
    import dependabot_watcher
    if Path(dependabot_watcher.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2] / 'src'):
        parser.error('Use an isolated installed artifact, not development src')
    args.output.mkdir(parents=True)
    root = Path(tempfile.mkdtemp(prefix='watcher-live-branch-'))
    workspace, _policy = policy_workspace(root / 'instructions')
    primary = workspace.parent / 'primary'
    git(primary, 'switch', '-c', 'remote-update')
    write(primary / 'app.py', 'VALUE = 2\n')
    git(primary, 'commit', '-am', 'Synthetic candidate')
    base = git(workspace, 'rev-parse', 'HEAD')

    class SyntheticGitHub:
        def __init__(self):
            self.head = git(primary, 'rev-parse', 'HEAD')
            self.writes = []

        def fetch_branch_refresh_state(self, repository, number):
            return {'number': number, 'state': 'open', 'merged': False, 'draft': False,
                    'mergeable': True, 'user': {'login': 'dependabot[bot]'},
                    'head': {'sha': self.head, 'ref': 'dependabot/example', 'repo': {'full_name': repository}},
                    'base': {'sha': base, 'ref': 'main', 'repo': {'full_name': repository}}}

        def fetch_branch_head(self, repository, branch):
            return base

        def compare_commits(self, repository, base, head):
            if base == head:
                return 'identical'
            common = git(primary, 'merge-base', base, head)
            return 'ahead' if common == base else 'behind' if common == head else 'diverged'

        def update_pull_request_branch(self, repository, number, *, head_sha):
            assert head_sha == self.head
            self.writes.append((repository, number, head_sha))
            git(primary, 'merge', '--no-ff', '--no-edit', base)
            self.head = git(primary, 'rev-parse', 'HEAD')

    state_root = root / 'state-builder'
    state_root.mkdir()
    with pytest.MonkeyPatch.context() as monkeypatch:
        case = synthetic_fixture(action_case, state_root, monkeypatch)
        config = configure(case['config_path'], workspace, args.model, args.reasoning, args.account)
        queue = ReviewQueue(config, case['queue'].directory, config_path=case['config_path'])
        bind(queue, workspace, case['report'])
        selected = queue.handle({'schema_version': 1, 'request_id': 'read_initial', 'operation': 'snapshot'})['proposals'][0]
        client = SyntheticGitHub()
        controller = BranchRefreshReviews(queue, client, case['config_path'])
        prepared = controller.prepare(selected['proposal_id'], selected['version'])
        assert not client.writes
        controller.confirm(prepared['review_id'])
        observed = controller.check(prepared['review_id'])
        assert observed['status'] == 'updated'
        report = queue._current_plan()
        assert report is not None
        bind(queue, workspace, report)
        response, allowed = controller.continue_validation(prepared['review_id'])
        assert allowed
        saved_request = controller.snapshot()[-1]['continuation_request']
        saved_message = saved_request['message']
        delivered = next(item for item in response['proposals'] if item['proposal_id'] == saved_request['proposal_id'])
        assert delivered['version'] == saved_request['version'] + 1
        context = queue.supervision_context('example-org/example-app')
        assert any(message['message'] == saved_message for item in context['proposals'] for message in item['feedback'])
        live(queue, workspace, root, args.output, 'branch-refresh-continuation', args.model, args.reasoning, args.account)
        submitted_text = (args.output / 'branch-refresh-continuation/submitted-prompt.txt').read_text()
        submitted = json.loads(submitted_text.rsplit('\n', 1)[-1])
        recipients = [item for item in submitted['context']['proposals']
                      if item['proposal_id'] == saved_request['proposal_id']]
        assert len(recipients) == 1 and recipients[0]['version'] == delivered['version']
        assert any(message['request_id'] == saved_request['request_id'] and message['message'] == saved_message
                   for message in recipients[0]['feedback'])
        outcome = json.loads((args.output / 'branch-refresh-continuation/outcome.json').read_text())
        assert any(reply['proposal_id'] == saved_request['proposal_id'] and reply['request_id'] == saved_request['request_id']
                   for reply in outcome['job']['result']['replies'])
        _again, allowed_again = controller.continue_validation(prepared['review_id'])
        assert not allowed_again and len(client.writes) == 1
        write(args.output / 'branch-result.json', {'synthetic_updates': len(client.writes),
              'continuation_delivered': True, 'duplicate_start_refused': True,
              'message': saved_message, 'package': dependabot_watcher.__file__})
    print('Branch context acceptance complete; no GitHub requests or real repository writes.')


if __name__ == '__main__':
    main()
