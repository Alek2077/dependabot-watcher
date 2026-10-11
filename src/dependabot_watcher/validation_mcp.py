"""Job-local MCP transport for the existing validation controller.

The stdio child is a relay, not an authority or execution engine. The supervisor
owns the private socket, ValidationTools, current job checks and cancellation.
No user/global MCP configuration or credentials are written.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import socket
import struct
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .review_queue import ReviewQueueError
from .runtime_state import RuntimeStateError, _unique_json_object, save_private_json
from .validation_tools import ValidationTools, validation_tool_specs
from .validation_runtime import ValidationRuntimeError


SERVER_NAME = 'watcher_validation'
MAX_FRAME = 65_536


def _wire_receipt(result: dict[str, Any], call_id: str) -> dict[str, Any]:
    """Bound model evidence, not the full private preparation/execution receipt."""
    wire = {**result, 'transport_call_id': call_id}
    output = wire.get('output')
    if isinstance(output, str) and len(output.encode('utf-8')) > 8192:
        encoded = output.encode('utf-8')
        wire['output'] = (encoded[:4096].decode('utf-8', errors='ignore')
            + '\n[Watcher preview shortened; original output hash is retained.]\n'
            + encoded[-4096:].decode('utf-8', errors='ignore'))
        wire['output_preview_truncated'] = True
    omissions = wire.get('omissions')
    if isinstance(omissions, list):
        encoded = json.dumps(omissions, ensure_ascii=False, separators=(',', ':')).encode()
        if len(encoded) > 4096:
            sample: list[Any] = []
            occupied = 2
            for item in omissions[:20]:
                size = len(json.dumps(item, ensure_ascii=False).encode()) + 1
                if occupied + size > 4096:
                    break
                sample.append(item)
                occupied += size
            wire.update(omissions=sample, omissions_preview_truncated=True,
                omissions_total=len(omissions), omissions_sha256=hashlib.sha256(encoded).hexdigest())
    encoded = json.dumps(wire, ensure_ascii=False, separators=(',', ':')).encode()
    if len(encoded) > 16_384:
        wire = {key: wire[key] for key in ('job_id', 'context_digest', 'head_sha',
                'proposal_id', 'action_id', 'validation_id', 'transport_call_id') if key in wire}
        wire.update(status='blocked', reason='Validation evidence exceeds the bounded model receipt; no complete interpretation is established.',
                    normalized_response_sha256=hashlib.sha256(encoded).hexdigest())
    return wire


class ValidationMcpBridge:
    """One bounded, transient request; admission belongs to the active turn."""

    def __init__(self, job_id: str, context_digest: str) -> None:
        if os.name != 'posix':
            raise ReviewQueueError('Validation MCP requires the local POSIX backend.')
        self._directory = tempfile.TemporaryDirectory(prefix='watcher-validation-')
        self.path = Path(self._directory.name) / 'bridge.sock'
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._listener.bind(str(self.path))
            self.path.chmod(0o600)
            self._listener.listen(1)
            self._listener.setblocking(False)
        except Exception:
            self._listener.close()
            self._directory.cleanup()
            raise
        self._client: socket.socket | None = None
        self._input = bytearray()
        self._output = bytearray()
        self._call: str | None = None
        self._nonce = secrets.token_hex(32)
        self._job_id = job_id
        self._context_digest = context_digest
        self._runtime: tuple[str, str] | None = None
        self._deadline = 0.0
        self._receipt: dict[str, Any] | None = None
        self._seen_calls: set[str] = set()
        self.delivered_calls = 0
        self.delivered_receipts: list[tuple[str, dict[str, Any]]] = []

    def bind_turn(self, session_id: str, turn_id: str) -> None:
        if self._runtime is not None:
            raise ReviewQueueError('Validation bridge already belongs to a turn.')
        self._runtime = session_id, turn_id

    def configuration(self, timeout_seconds: int) -> dict[str, Any]:
        if not Path('/usr/bin/env').is_file():
            raise ReviewQueueError('The trusted validation relay environment launcher is unavailable.')
        return {'command': '/usr/bin/env',
                'args': ['-i', sys.executable, '-I', '-m', 'dependabot_watcher.validation_mcp', str(self.path), self._nonce,
                         self._job_id, self._context_digest, str(timeout_seconds + 15)],
                'enabled': True, 'required': True, 'startup_timeout_sec': 10,
                'tool_timeout_sec': timeout_seconds + 15,
                'enabled_tools': [item['name'] for item in validation_tool_specs()],
                'tools': {item['name']: {'output_token_limit': 20_000} for item in validation_tool_specs()},
                'default_tools_approval_mode': 'approve'}

    def pump(self, validation: ValidationTools, *, active: bool, dynamic_pending: bool) -> None:
        """Called by the supervisor's responsive event loop, never a reader thread."""
        validation.ensure_authority()
        if self._client is None:
            try:
                self._client, _ = self._listener.accept()
                self._client.setblocking(False)
                if hasattr(socket, 'SO_PEERCRED'):
                    _pid, uid, _gid = struct.unpack('3i', self._client.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                    if uid != os.getuid():
                        raise ReviewQueueError('Validation relay peer does not belong to the backend user.')
                self._deadline = time.monotonic() + 5
            except BlockingIOError:
                return
        client = self._client
        if time.monotonic() >= self._deadline:
            raise ReviewQueueError('Validation relay exceeded its request/result deadline.')
        if self._output:
            try:
                del self._output[:client.send(self._output)]
            except BlockingIOError:
                return
            except OSError as exc:
                raise ReviewQueueError('Validation tool consumer disconnected.') from exc
            if not self._output:
                assert self._receipt is not None
                self._receipt['status'] = 'completed'
                save_private_json(validation.root, Path('transport_' + self._receipt['call_id'] + '.json'), self._receipt)
                self.delivered_calls += 1
                self._reset_client()
            return
        if self._call is not None:
            try:
                premature = client.recv(1, socket.MSG_PEEK)
                if not premature:
                    raise ReviewQueueError('Validation relay disconnected during execution.')
                raise ReviewQueueError('Validation relay sent an unexpected concurrent request.')
            except BlockingIOError:
                pass
            completed = validation.poll()
            if completed is not None:
                if completed[0] != self._call:
                    raise ReviewQueueError('Validation MCP result belongs to a different call.')
                assert self._receipt is not None
                wire_receipt = _wire_receipt(completed[1], self._call)
                self._output.extend((json.dumps(wire_receipt, ensure_ascii=False) + '\n').encode())
                self.delivered_receipts.append((self._receipt['tool'], wire_receipt))
                self._receipt.update(status='controller_completed', operation_id=completed[1].get('operation_id'),
                    result_status=completed[1].get('status'), head_sha=completed[1].get('head_sha'),
                    proposal_id=completed[1].get('proposal_id'), action_id=completed[1].get('action_id'),
                    validation_id=completed[1].get('validation_id'))
                save_private_json(validation.root, Path('transport_' + self._call + '.json'), self._receipt)
                self._call = None
                self._deadline = time.monotonic() + 5
            return
        try:
            chunk = client.recv(MAX_FRAME + 1)
        except BlockingIOError:
            return
        except OSError as exc:
            raise ReviewQueueError('Validation tool consumer disconnected.') from exc
        if not chunk:
            raise ReviewQueueError('Validation tool consumer disconnected before its request.')
        self._input.extend(chunk)
        if len(self._input) > MAX_FRAME:
            raise ReviewQueueError('Validation MCP request exceeds its transport bound.')
        if b'\n' not in self._input:
            return
        if not self._input.endswith(b'\n') or self._input.count(b'\n') != 1:
            raise ReviewQueueError('Validation MCP accepts one exact request per connection.')
        try:
            request = json.loads(self._input, object_pairs_hook=_unique_json_object)
        except (ValueError, RecursionError, UnicodeError, RuntimeStateError) as exc:
            raise ReviewQueueError('Validation MCP request is malformed.') from exc
        if (not active or self._runtime is None or dynamic_pending or not isinstance(request, dict)
                or set(request) != {'call_id', 'tool', 'arguments', 'nonce', 'job_id', 'context_digest'}
                or not isinstance(request.get('nonce'), str)
                or not secrets.compare_digest(request['nonce'], self._nonce)
                or request.get('job_id') != self._job_id or request.get('context_digest') != self._context_digest):
            raise ReviewQueueError('Validation MCP request has no exclusive active-turn authority.')
        if (not isinstance(request['call_id'], str) or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request['call_id']) is None
                or request['call_id'] in self._seen_calls or len(self._seen_calls) >= 40):
            raise ReviewQueueError('Validation MCP call identity is malformed.')
        call_id = request['call_id']
        self._seen_calls.add(call_id)
        self._call = call_id
        self._receipt = {'job_id': self._job_id, 'context_digest': self._context_digest,
            'session_id': self._runtime[0], 'turn_id': self._runtime[1], 'call_id': self._call,
            'tool': request['tool'], 'status': 'pending'}
        save_private_json(validation.root, Path('transport_' + call_id + '.json'), self._receipt)
        try:
            validation.submit(call_id, request['tool'], request['arguments'])
        except ValidationRuntimeError as exc:
            validation.ensure_authority()
            result = _wire_receipt({'status': 'blocked', 'reason': str(exc),
                'job_id': self._job_id, 'context_digest': self._context_digest}, call_id)
            self._output.extend((json.dumps(result, ensure_ascii=False) + '\n').encode())
            self.delivered_receipts.append((request['tool'], result))
            self._receipt.update(status='controller_completed', result_status='blocked')
            save_private_json(validation.root, Path('transport_' + call_id + '.json'), self._receipt)
            self._call = None
            self._deadline = time.monotonic() + 5
            return
        self._deadline = time.monotonic() + validation.limits.timeout_seconds + 10

    @property
    def pending(self) -> bool:
        return self._client is not None

    def _reset_client(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None
        self._input.clear()
        self._output.clear()
        self._receipt = None

    def close(self) -> None:
        self._reset_client()
        self._listener.close()
        self._directory.cleanup()


def _relay_call(path: str, nonce: str, job_id: str, context_digest: str,
                timeout_seconds: int, name: object, arguments: object) -> dict[str, Any]:
    request = {'call_id': 'mcp_' + uuid.uuid4().hex, 'tool': name, 'arguments': arguments,
               'nonce': nonce, 'job_id': job_id, 'context_digest': context_digest}
    encoded = (json.dumps(request, ensure_ascii=False) + '\n').encode()
    if len(encoded) > MAX_FRAME:
        raise ValueError('Validation request is too large.')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout_seconds)
        connection.connect(path)
        connection.sendall(encoded)
        stream = connection.makefile('rb')
        try:
            # Existing validation output is capped by its runtime; keep relay
            # parsing separately bounded, without persisting tool output.
            raw = stream.readline(2_000_001)
        finally:
            stream.close()
        if not raw.endswith(b'\n') or len(raw) > 2_000_000:
            raise ValueError('Validation result is missing or exceeds its bound.')
        result = json.loads(raw, object_pairs_hook=_unique_json_object)
        if not isinstance(result, dict):
            raise ValueError('Validation result is malformed.')
        return result


def serve(path: str, nonce: str, job_id: str, context_digest: str, timeout_seconds: int) -> None:
    """Minimal stdio MCP: expose only the two existing validation operations."""
    while line := sys.stdin.buffer.readline(MAX_FRAME + 1):
        if len(line) > MAX_FRAME or not line.endswith(b'\n'):
            raise ValueError('MCP input exceeds its bound.')
        message = json.loads(line, object_pairs_hook=_unique_json_object)
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            raise ValueError('MCP input is malformed.')
        if 'id' not in message:
            continue
        identifier = message['id']
        method = message.get('method')
        if method == 'initialize':
            result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}},
                      'serverInfo': {'name': SERVER_NAME, 'version': '1'},
                      'instructions': 'Use these tools for exact-revision isolated validation only. Saved job permissions and resource limits remain authoritative; results do not authorize a merge.'}
        elif method == 'tools/list':
            result = {'tools': [{key: value for key, value in item.items() if key != 'type'}
                                for item in validation_tool_specs()]}
        elif method == 'resources/list':
            result = {'resources': []}
        elif method == 'resources/templates/list':
            result = {'resourceTemplates': []}
        elif method == 'ping':
            result = {}
        elif method == 'tools/call':
            params = message.get('params', {})
            try:
                receipt = _relay_call(path, nonce, job_id, context_digest, timeout_seconds,
                                      params.get('name'), params.get('arguments'))
                # A controller refusal is useful structured evidence, not an
                # MCP transport failure. The agent must interpret status/code.
                result = {'content': [{'type': 'text', 'text': json.dumps(receipt, ensure_ascii=False)}],
                          'isError': False}
            except (OSError, ValueError, RecursionError):
                # A requested operation without a controller receipt is a lost
                # capability, not normal evidence that a test failed. Exit so
                # the provider reports a failed MCP item and the job fails.
                raise SystemExit(1)
        else:
            print(json.dumps({'jsonrpc': '2.0', 'id': identifier,
                              'error': {'code': -32601, 'message': 'Unsupported MCP method'}}), flush=True)
            continue
        print(json.dumps({'jsonrpc': '2.0', 'id': identifier, 'result': result}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    if len(sys.argv) != 6:
        raise SystemExit('Expected the backend-owned validation endpoint.')
    serve(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
