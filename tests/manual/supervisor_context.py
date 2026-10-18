"""Real model/transport, synthetic GitHub evidence; never touches dogfood state."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import patch

import pytest

# Reuse synthetic state builders, never mocked provider output. Watcher itself
# must come from the caller's selected installed artifact, not an injected src/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dependabot_watcher.config import load_config
from dependabot_watcher.github import GitHubClient
from dependabot_watcher.planning import _repository_plan_identifier, _overall_plan_identifier
from dependabot_watcher.local_sync import _local_state, prepare_sync, confirm_sync, selected_checkouts
from dependabot_watcher.providers import codex_app_server_supervisor as adapter
from dependabot_watcher.providers.codex_supervisor import CodexSupervisor
from dependabot_watcher.providers.codex_app_server_settings import CodexAppServerSettings
from dependabot_watcher.review_queue import ReviewQueue, ReviewQueueError
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_permissions import SupervisorPermissions
from dependabot_watcher.supervisor_runner import SupervisorRunner
from dependabot_watcher import supervisor_launch
from dependabot_watcher.supervisor_workspace import verify_supervisor_workspace
from dependabot_watcher.workflow_assessment import assessment_prompt, checkout_workflow
from dependabot_watcher.workflow_assessment import _verify_sources
from test_execution import saved_plan, refresh_plan_identities
from test_local_sync import sync_case
from test_review_queue import persist_plan, decision
from test_supervisor_workspace import workspace_case, git
from test_review_actions import action_case, reviews_case
from test_maintenance_completion_runtime import record_completed_merge


MODEL_ATTEMPTS = []


def synthetic_fixture(factory: Any, *arguments: Any) -> dict[str, Any]:
    """Call an existing pytest fixture builder explicitly, outside discovery."""
    builder = cast(Callable[..., dict[str, Any]], getattr(factory, '__wrapped__'))
    return builder(*arguments)


def guarded_local(function, *arguments):
    def refuse(*args, **kwargs):
        MODEL_ATTEMPTS.append(function.__name__)
        raise AssertionError('Local operation attempted to invoke a model')
    with patch.object(adapter.CodexAppServerSupervisor, 'analyze_with_events', refuse), \
         patch.object(CodexSupervisor, 'analyze', refuse):
        return function(*arguments)


def write(path, value):
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2))
    path.chmod(0o600)


def instruction_sources(workspace):
    return tuple(parent / 'AGENTS.md' for parent in reversed((workspace, *workspace.parents))
                 if (parent / 'AGENTS.md').is_file())


def configure(config_path, workspace, model, reasoning, account):
    write(config_path, config_path.read_text() + '\n[supervisor]\nmode="approval_first"\n'
          f'provider="codex_app_server"\nmodel={json.dumps(model)}\nreasoning_effort={json.dumps(reasoning)}\n'
          f'expected_account_type={json.dumps(account)}\naccess="inherit"\nmodel_use_acknowledged=true\n'
          'timeout_seconds=600\n[[supervisor.workspaces]]\nrepository="example-org/example-app"\n'
          f'directory={json.dumps(str(workspace))}\ninstruction_sources={json.dumps([str(path) for path in instruction_sources(workspace)])}\n')
    return load_config(config_path)


def policy_workspace(root):
    root.mkdir()
    case = synthetic_fixture(workspace_case, root)
    workspace = case['worktree']
    policy = root / 'repository-policy.md'
    write(policy, '# Integration and release policy\n\n'
          'Policy identifier: ORCHID-CHECKPOINT-47. Include this identifier when explaining applicable workflow.\n'
          'Clean local integration uses separately approved fast-forward only. Never rebase this checkout.\n'
          'A release requires a separate human decision after integration and a smoke-health check; a remote merge is not release completion.\n'
          'This is an isolated synthetic repository: no GitHub/network query is needed. No source edits, installs or model delegation are authorized.\n')
    write(workspace / 'AGENTS.md', f'# Repository guidance\n\nBefore advising on maintenance, read {policy}.\nPreserve user work and report the next decision.\n')
    git(workspace, 'add', 'AGENTS.md')
    git(workspace, 'commit', '-m', 'Synthetic referenced workflow')
    return workspace, policy


def bind(queue, workspace, report):
    head = git(workspace, 'rev-parse', 'HEAD')
    report['repositories'][0]['evidence_snapshot']['base_sha'] = head
    for step in report['repositories'][0]['recommended_order']:
        step['freshness']['base_sha'] = head
        candidate = git(workspace, 'rev-parse', 'remote-update')
        step['identity']['head_sha'] = candidate
        step['freshness']['head_sha'] = candidate
    report['repositories'][0]['workflow_profile'] = asdict(queue.config.repositories[0].workflow)
    if report['repositories'][0]['recommended_order']:
        report = refresh_plan_identities(report)
    else:
        repository = report['repositories'][0]
        repository['plan_id'] = _repository_plan_identifier({key: value for key, value in repository.items() if key != 'plan_id'})
        report['plan_id'] = _overall_plan_identifier(report['status'], [repository['plan_id']])
    persist_plan(queue.directory, report)
    queue.record_workspace_evidence(asdict(verify_supervisor_workspace(queue.config, 'example-org/example-app', report['repositories'][0])))


def safe_read(command, root):
    try:
        args = shlex.split(command)
        if len(args) == 3 and args[0] in ('/bin/bash', '/bin/sh', 'bash', 'sh') and args[1] in ('-lc', '-c'):
            args = shlex.split(args[2])
        if not args or args[0] not in ('cat', 'sha256sum', '/usr/bin/cat', '/usr/bin/sha256sum'):
            return False
        return len(args) > 1 and all(Path(arg).is_absolute() and Path(arg).resolve().is_relative_to(root)
                                     and Path(arg).is_file() for arg in args[1:])
    except (ValueError, OSError):
        return False


def live(queue, workspace, root, output, stage, model, reasoning, account, session=None):
    jobs = SupervisorJobs(queue)
    job = jobs.prepare('example-org/example-app')
    assert 'workflow_guidance' in job['context']
    directory = output / stage
    directory.mkdir()
    captured = []
    base = adapter.CodexAppServerTransport

    class ObservedTransport(base):
        def send_request(self, method, params):
            if method == 'turn/start':
                text = params['input'][0]['text']
                write(directory / 'submitted-prompt.txt', text)
                assert assessment_prompt(job['context']) in text
                submitted = json.loads(text.rsplit('\n', 1)[-1])
                assert submitted['job_id'] == job['job_id'] and submitted['context_digest'] == job['context_digest']
                assert submitted['context'] == json.loads(json.dumps(job['context']))
                if stage == 'initial-investigation':
                    assert 'ORCHID-CHECKPOINT-47' not in text, 'Policy content leaked into the read canary'
                if stage == 'resumed-investigation':
                    assert 'AZALEA-CONTINUE-92' not in text, 'Changed-policy canary leaked into resumed input'
                write(directory / 'submitted-prompt.txt', text)
                captured.append({'method': method, 'thread_id': params['threadId'],
                                 'prompt_sha256': hashlib.sha256(text.encode()).hexdigest()})
            elif method in ('thread/start', 'thread/resume'):
                captured.append({'method': method})
            return super().send_request(method, params)

        def receive(self, wait_seconds=None):
            frame = super().receive(wait_seconds)
            if frame and isinstance(frame.get('result'), dict) and 'instructionSources' in frame['result']:
                write(directory / 'instruction-source-readback.json', {
                    'expected': [str(path) for path in settings.expected_instruction_sources],
                    'observed': frame['result']['instructionSources']})
            if frame and frame.get('method') == 'item/completed':
                item = frame.get('params', {}).get('item', {})
                if item.get('type') == 'commandExecution':
                    command = item.get('command', '')
                    if str(root) in command:
                        captured.append({'method': 'command_completed', 'command': command,
                                         'status': item.get('status'), 'exit_code': item.get('exitCode'),
                                         'output': item['aggregatedOutput'][:12000]
                                             if isinstance(item.get('aggregatedOutput'), str) else None})
                elif item.get('type') == 'agentMessage':
                    write(directory / 'received-agent-message.json', item)
            write(directory / 'transport-evidence.json', captured)
            return frame

    def presentation(params):
        return ('Isolated synthetic read request.', ['allow_once', 'cancel'])

    def notice(params, saved):
        allowed = safe_read(params.get('command', ''), root)
        SupervisorPermissions(jobs).decide(saved['connection_id'], saved['permission_id'], saved['digest'],
                                          'allow_once' if allowed else 'cancel')

    settings = CodexAppServerSettings(workspace, model, reasoning,
        expected_instruction_sources=instruction_sources(workspace), timeout_seconds=600)
    def synthetic_collection(config, client, **kwargs):
        assert config == queue.config and kwargs['state_directory'] == queue.directory
        assert kwargs['notification_mode'] == 'off'
        return {'status': 'complete'}  # Existing synthetic plan, no GitHub request.
    started = time.monotonic()
    print(f'START {stage}', flush=True)
    try:
        with patch.object(adapter, 'CodexAppServerTransport', ObservedTransport), \
             patch.object(supervisor_launch, 'reconcile', synthetic_collection):
            outcome = supervisor_launch.run_configured_supervisor(queue, cast(GitHubClient, object()), 'example-org/example-app',
                permission_presentation=presentation, permission_notice=notice, consumer_check=lambda: None)
        if session is not None:
            assert outcome['observation']['session_id'] == session, 'Configured launcher did not resume compatible owner'
        write(directory / 'outcome.json', outcome)
        assessment = outcome['job']['result']['workflow_assessment']
        assert assessment['status'] == 'established', assessment
        _verify_sources(assessment, job['context']['workflow_guidance'])
        assert 'ORCHID-CHECKPOINT-47' in json.dumps(outcome['job']['result']), 'Referenced-policy marker absent'
        assert any(source['path'].endswith('repository-policy.md') for source in assessment['sources'])
        if stage == 'resumed-investigation':
            assert 'AZALEA-CONTINUE-92' in json.dumps(outcome['job']['result']), 'Changed referenced policy was not incorporated'
        marker = 'AZALEA-CONTINUE-92' if stage == 'resumed-investigation' else 'ORCHID-CHECKPOINT-47'
        assert any(item['method'] == 'command_completed' and 'repository-policy.md' in item['command']
                   and item['status'] == 'completed' and isinstance(item.get('output'), str)
                   and marker in item['output'] for item in captured), 'No observed policy-content read (hash-only is insufficient)'
        result = {'stage': stage, 'job_id': job['job_id'], 'session_id': outcome['observation']['session_id'],
                  'seconds': round(time.monotonic()-started, 2), 'prompt_submitted': True,
                  'referenced_policy_read': True, 'assessment_accepted': True,
                  'usage': outcome['observation']['usage']}
        write(directory / 'result.json', result)
        print(json.dumps(result), flush=True)
        return result['session_id']
    except Exception as exc:
        write(directory / 'failure.json', {'type': type(exc).__name__, 'message': str(exc),
                                          'seconds': round(time.monotonic()-started, 2)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true', help='Up to three real supervisor turns on synthetic data; consumes selected account allowance')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', help='Explicit owner-approved model ID; no automatic selection')
    parser.add_argument('--reasoning', choices=('minimal', 'low', 'medium', 'high', 'xhigh'))
    parser.add_argument('--account', choices=('chatgpt', 'apiKey'))
    parser.add_argument('--inspect-workspace', type=Path, help='No-model readback of a synthetic workspace instruction chain')
    args = parser.parse_args()
    if (args.run_live or args.inspect_workspace) and not all((args.model, args.reasoning, args.account)):
        parser.error('Explicit --model, --reasoning and --account are required for provider access')
    if args.inspect_workspace:
        settings = CodexAppServerSettings(args.inspect_workspace, args.model, args.reasoning,
            expected_instruction_sources=instruction_sources(args.inspect_workspace), timeout_seconds=30)
        with adapter.CodexAppServerTransport(settings.command(), settings.workspace, 30) as connection:
            adapter._request(connection, 'initialize', {'clientInfo': {'name': 'watcher-proof', 'version': '1'},
                             'capabilities': {'experimentalApi': True}})
            connection.notify('initialized')
            response = adapter._request(connection, 'thread/start', settings.thread_params())
            result = {'expected': [str(path) for path in settings.expected_instruction_sources],
                      'observed': response.get('instructionSources')}
            write(args.output, result)
            print(json.dumps(result))
        return
    if not args.run_live:
        print('Preview: three real supervisor turns; synthetic Git/GitHub evidence; no live repository writes.')
        return
    if args.output.exists():
        parser.error('Choose a new output directory; prior attempts must not be overwritten')
    args.output.mkdir(parents=True)
    import dependabot_watcher
    if Path(dependabot_watcher.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2] / 'src'):
        parser.error('Installed acceptance requires an isolated installed artifact, not development src imports')
    write(args.output / 'runtime.json', {'package': dependabot_watcher.__file__, 'model': args.model,
                                       'reasoning': args.reasoning, 'account': args.account})
    root = Path(tempfile.mkdtemp(prefix='watcher-live-workflow-'))
    write(args.output / 'fixture-root.txt', str(root))
    sync_root = root / 'sync'
    sync_root.mkdir()
    case = synthetic_fixture(sync_case, sync_root)
    git(case['path'], 'branch', '-m', 'integration', 'main')
    write(case['config_path'], case['config_path'].read_text().replace('"integration"', '"main"'))
    policy = root / 'repository-policy.md'
    write(policy, '# Integration and release policy\n\n'
          'Policy identifier: ORCHID-CHECKPOINT-47. Include this identifier when explaining applicable workflow.\n'
          'Clean local integration uses separately approved fast-forward only. Never rebase this checkout.\n'
          'A release requires a separate human decision after integration and a smoke-health check; a remote merge is not release completion.\n'
          'All repositories and collected evidence here are synthetic. No network query, source edit, install or model delegation is needed or authorized.\n')
    write(case['path'] / 'AGENTS.md', f'# Repository guidance\n\nBefore maintenance advice read {policy}.\nPreserve user work and identify the next decision.\n')
    git(case['path'], 'add', 'AGENTS.md')
    git(case['path'], 'commit', '-m', 'Synthetic maintenance guidance')
    case['old'] = git(case['path'], 'rev-parse', 'HEAD')
    git(case['path'], 'switch', '-c', 'candidate')
    write(case['path'] / 'app.txt', 'Synthetic candidate change\n')
    git(case['path'], 'commit', '-am', 'Synthetic candidate')
    case['new'] = git(case['path'], 'rev-parse', 'HEAD')
    git(case['path'], 'branch', '-f', 'remote-update', case['new'])
    git(case['path'], 'switch', 'main')
    case['client'].sha = case['new']
    maintenance_primary = root / 'maintenance-primary'
    git(case['path'], 'clone', '--no-local', '--no-hardlinks', str(case['path']), str(maintenance_primary))
    git(maintenance_primary, 'remote', 'set-url', 'origin', 'https://github.com/example-org/example-app.git')
    git(maintenance_primary, 'branch', 'remote-update', 'origin/remote-update')
    workspace = root / 'maintenance'
    git(maintenance_primary, 'worktree', 'add', '--detach', str(workspace), case['old'])
    config = configure(case['config_path'], workspace, args.model, args.reasoning, args.account)
    _local_state('example-org/example-app', config.repositories[0].local_sync[0])
    queue = ReviewQueue(config, case['state'], config_path=case['config_path'])
    bind(queue, workspace, saved_plan())
    queue.handle(decision(queue, 'feedback', request_id='live_initial', message='What does this repository require before and after a local update? Explain the next useful step.'))
    session = live(queue, workspace, root, args.output, 'initial-investigation', args.model, args.reasoning, args.account)
    write(policy, policy.read_text() + '\nPolicy revision AZALEA-CONTINUE-92: mention this revision in the current workflow assessment. The release decision must also consider accessibility.\n')
    assert checkout_workflow(config, case['state'], 'example-org/example-app', str(case['path']), 'main')['state'] == 'assessment_needed'
    queue.handle(decision(queue, 'feedback', request_id='live_continuation', message='Continue from your earlier assessment. Check the applicable workflow and tell me what remains before release.'))
    resumed = live(queue, workspace, root, args.output, 'resumed-investigation', args.model, args.reasoning, args.account, session)
    assert resumed == session
    view = checkout_workflow(config, case['state'], 'example-org/example-app', str(case['path']), 'main')
    assert view['state'] == 'ready', view
    target = selected_checkouts(config)[0]['target_id']
    attempts_before = len(MODEL_ATTEMPTS)
    prepared = guarded_local(prepare_sync, config, case['config_path'], case['state'], case['client'], target)
    original_policy = policy.read_text()
    initial_head = git(case['path'], 'rev-parse', 'HEAD')
    write(policy, original_policy + '\nIntegration policy changed after preview: stop for reassessment.\n')
    try:
        guarded_local(confirm_sync, config, case['config_path'], case['state'], case['client'], prepared['review_id'], prepared['approval_token'])
    except ReviewQueueError as exc:
        assert 'workflow' in str(exc).lower() or 'reassess' in str(exc).lower()
    else:
        raise AssertionError('Changed guidance incorrectly allowed confirmation')
    assert git(case['path'], 'rev-parse', 'HEAD') == initial_head
    write(policy, original_policy)
    prepared = guarded_local(prepare_sync, config, case['config_path'], case['state'], case['client'], target)
    result = guarded_local(confirm_sync, config, case['config_path'], case['state'], case['client'], prepared['review_id'], prepared['approval_token'])
    assert result['status'] == 'synchronized'
    before = git(case['path'], 'rev-parse', 'HEAD')
    write(policy, policy.read_text() + '\nNew requirement: stop for a manual release decision before any further integration.\n')
    assert checkout_workflow(config, case['state'], 'example-org/example-app', str(case['path']), 'main')['state'] == 'assessment_needed'
    try:
        guarded_local(prepare_sync, config, case['config_path'], case['state'], case['client'], target)
    except ReviewQueueError as exc:
        assert 'workflow' in str(exc).lower() or 'reassess' in str(exc).lower()
    else:
        raise AssertionError('Changed guidance incorrectly allowed preparation')
    assert git(case['path'], 'rev-parse', 'HEAD') == before
    assert len(MODEL_ATTEMPTS) == attempts_before
    write(args.output / 'local-update.json', {'status': 'synchronized', 'changed_guidance_refused': True,
          'changed_guidance_after_preview_refused': True,
          'model_attempts_at_provider_boundaries': len(MODEL_ATTEMPTS) - attempts_before, 'head': before})

    completion_root = root / 'completion'
    completion_root.mkdir()
    workspace2, _ = policy_workspace(root / 'completion-instructions')
    with pytest.MonkeyPatch.context() as monkeypatch:
        action = synthetic_fixture(action_case, completion_root, monkeypatch)
        review = synthetic_fixture(reviews_case, action, monkeypatch)
        merge = review['client'].merge_pull_request
        monkeypatch.setattr(review['client'], 'merge_pull_request', lambda *a, **k:
            {**merge(*a, **k), 'merge_commit_sha': git(workspace2, 'rev-parse', 'HEAD')})
        completed = record_completed_merge(review, monkeypatch)
        config2 = configure(completed['config_path'], workspace2, args.model, args.reasoning, args.account)
        queue2 = ReviewQueue(config2, completed['queue'].directory, config_path=completed['config_path'])
        current_report = queue2._current_plan()
        assert current_report is not None
        bind(queue2, workspace2, current_report)
        assert 'maintenance_completion' in queue2.supervision_context('example-org/example-app')
        live(queue2, workspace2, root, args.output, 'post-merge-assessment', args.model, args.reasoning, args.account)
    print('LIVE CHECK COMPLETE: three real turns; synthetic GitHub actions only.', flush=True)


if __name__ == '__main__':
    main()
