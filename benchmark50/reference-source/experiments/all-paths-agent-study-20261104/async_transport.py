"""Background Responses transport with one creation attempt per round."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import time

import httpx

from support import ROOT, save
from transport import ModelServiceError, validate_connection, wall_limit


CODEC_PATH = ROOT / 'decision_lab/responses_codec.py'
CODEC_SHA256 = '7c1c493801d2272f479be4738d81829004268fedb52fd0c554326081e99fe3de'
PENDING = {'queued', 'in_progress'}
TERMINAL = {'completed', 'incomplete', 'failed', 'cancelled'}
TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}
TRANSIENT_TRANSPORT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
RESPONSE_ID = re.compile(r'resp_[A-Za-z0-9_-]+')


def _load_codec():
    source = CODEC_PATH.read_bytes()
    if hashlib.sha256(source).hexdigest() != CODEC_SHA256:
        raise ValueError('Frozen Responses codec hash mismatch')
    spec = importlib.util.spec_from_file_location('_agentstudy_frozen_responses_codec', CODEC_PATH)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, str(CODEC_PATH), 'exec'), module.__dict__)
    return module


def validate_async_connection(config):
    if config.get('protocol') != 'responses_background_stateless':
        raise ValueError('This transport requires responses_background_stateless')
    checked = validate_connection({**config, 'protocol': 'chat_completions'})
    return {**checked, 'protocol': config['protocol']}


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Responses round deadline reached')
    return remaining


def _usage(obj):
    usage = obj.get('usage')
    if isinstance(usage, dict) and all(
        type(usage.get(key)) is int and usage[key] >= 0 for key in ('input_tokens', 'output_tokens')
    ):
        return usage
    return 'unknown'


class _HTTPError(ModelServiceError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f'Responses HTTP {status_code}')


class _Round:
    def __init__(self, out, client, endpoint, key, model):
        self.out = out
        self.client = client
        self.endpoint = endpoint
        self.key = key
        self.model = model
        self.wire = out / 'wire-http'
        self.wire.mkdir()  # Exclusive claim. Re-entry cannot start another creation.
        self.state = {
            'state': 'creation_intent', 'response_id': None, 'usage': 'unknown',
            'requested_model': model, 'creation_attempts': 0,
        }
        self.sequence = 0

    def persist(self, **updates):
        state = {**self.state, **updates}
        save(self.out / 'http.json', state)
        self.state = state

    def reload_state(self):
        # An interrupt can arrive after the atomic replace, before persist updates memory.
        self.state = json.loads((self.out / 'http.json').read_text())
        return self.state

    def _redact(self, text):
        if self.key and self.key != 'EMPTY':
            return text.replace(self.key, '[REDACTED]')
        return text

    def http(self, method, url, deadline, body=None):
        _remaining(deadline)
        self.sequence += 1
        prefix = self.wire / f'{self.sequence:04d}-{method.lower()}'
        started = time.monotonic()
        receipt = {'method': method, 'endpoint': url, 'usage': 'unknown'}
        save(prefix.with_suffix('.json'), receipt)
        try:
            remaining = _remaining(deadline)
            kwargs = {} if body is None else {'json': body}
            with wall_limit(remaining):
                response = self.client.request(
                    method, url, headers={'Authorization': 'Bearer ' + self.key},
                    timeout=remaining, follow_redirects=False, **kwargs,
                )
            receipt['status_code'] = response.status_code
            prefix.with_suffix('.txt').write_text(self._redact(response.text))
            if not response.is_success:
                raise _HTTPError(response.status_code)
            try:
                obj = response.json()
            except ValueError as exc:
                raise ModelServiceError('Invalid Responses JSON') from exc
            if not isinstance(obj, dict):
                raise ModelServiceError('Nonobject Responses payload')
            receipt['returned_id'] = self._redact(str(obj.get('id')))
            receipt['returned_status'] = self._redact(str(obj.get('status')))
            usage = _usage(obj)
            receipt['usage'] = json.loads(self._redact(json.dumps(usage)))
            return obj
        except BaseException as exc:
            receipt['error_type'] = type(exc).__name__
            raise
        finally:
            receipt['elapsed_seconds'] = time.monotonic() - started
            save(prefix.with_suffix('.json'), receipt)

    def accept(self, obj, *, created=False):
        response_id = obj.get('id')
        status = obj.get('status')
        if not isinstance(response_id, str) or not RESPONSE_ID.fullmatch(response_id):
            raise ModelServiceError('Invalid Responses ID')
        if created:
            self.persist(response_id=response_id, state='owned')
        elif response_id != self.state['response_id']:
            raise ModelServiceError('Responses ID mismatch')
        if not isinstance(status, str) or status not in PENDING | TERMINAL:
            raise ModelServiceError('Invalid Responses status')
        if obj.get('model') not in (None, self.model):
            raise ModelServiceError('Responses model mismatch')
        if self.state['state'] in TERMINAL and status != self.state['state']:
            raise ModelServiceError('Conflicting terminal Responses status')
        usage = _usage(obj)
        if usage == 'unknown' and status == self.state['state']:
            usage = self.state['usage']
        self.persist(state=status, provider_status=status, usage=json.loads(self._redact(json.dumps(usage))))

    def cleanup(self, deadline):
        # Consult the on-disk ownership record so an unpersisted ID is never cancelled.
        state = self.reload_state()
        response_id = state['response_id']
        if not response_id or state['state'] in TERMINAL:
            return
        url = self.endpoint + '/' + response_id
        self.persist(cleanup='attempted')
        cancel_deadline = time.monotonic() + _remaining(deadline) / 2
        try:
            self.accept(self.http('POST', url + '/cancel', cancel_deadline))
        except Exception as exc:
            self.persist(cancel_error=type(exc).__name__)
        try:
            self.accept(self.http('GET', url, deadline))
        except Exception as exc:
            self.persist(cleanup_get_error=type(exc).__name__)
        if self.state['state'] not in TERMINAL:
            self.persist(state='cleanup_unconfirmed', usage='unknown')
        else:
            self.persist(cleanup='terminal')


class AsyncTransport:
    """Present the study's Chat request/response interface over Responses."""

    def __init__(self, connection, client=None, *, poll_interval=0.5):
        self.config = validate_async_connection(connection)
        if not 0 < poll_interval <= 2:
            raise ValueError('Expected a poll interval in (0, 2] seconds')
        self.poll_interval = poll_interval
        self.codec = _load_codec()
        self.client = client if client is not None else httpx.Client(trust_env=False, follow_redirects=False)

    def __call__(self, chat_request, out, remaining_seconds):
        started = time.monotonic()
        deadline = started + remaining_seconds
        _remaining(deadline)
        work_deadline = deadline - min(2.0, remaining_seconds / 3)
        cfg = self.config
        body = self.codec.build_response_request(chat_request, cfg['model'])
        endpoint = cfg['base_url'] + '/responses'
        key = os.environ.get(cfg['api_key_env'], cfg.get('api_key_default', 'EMPTY'))
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        round_state = _Round(out, self.client, endpoint, key, cfg['model'])
        try:
            save(out / 'wire-request.json', body)
            round_state.persist()
            _remaining(work_deadline)
            round_state.persist(creation_attempts=1)
            obj = round_state.http('POST', endpoint, work_deadline, body)
            round_state.accept(obj, created=True)
            retries = 0
            while round_state.state['state'] in PENDING:
                time.sleep(min(self.poll_interval, _remaining(work_deadline)))
                url = endpoint + '/' + round_state.state['response_id']
                try:
                    obj = round_state.http('GET', url, work_deadline)
                except (*TRANSIENT_TRANSPORT, _HTTPError) as exc:
                    permanent_http = isinstance(exc, _HTTPError) and exc.status_code not in TRANSIENT_HTTP
                    if retries == 3 or permanent_http:
                        raise
                    retries += 1
                    continue
                round_state.accept(obj)
            chat = self.codec.response_to_chat(obj)
            chat.pop('_raw_response')  # Raw reasoning is already retained in the wire audit.
            if round_state.state['usage'] == 'unknown':
                chat['usage'] = None
            else:
                chat['usage'] = json.loads(round_state._redact(json.dumps(chat['usage'])))
            return chat
        except BaseException as exc:
            try:
                state = round_state.reload_state()
                updates = {'error_type': type(exc).__name__}
                if not state['response_id']:
                    updates['state'] = 'creation_unconfirmed' if state['creation_attempts'] else 'creation_not_attempted'
                round_state.persist(**updates)
                round_state.cleanup(deadline)
            except BaseException as cleanup_error:
                try:
                    state = round_state.reload_state()
                    updates = {'cleanup_error': type(cleanup_error).__name__}
                    if state['response_id'] and state['state'] not in TERMINAL:
                        updates.update(state='cleanup_unconfirmed', usage='unknown')
                    round_state.persist(**updates)
                except BaseException:
                    pass
            raise

    def close(self):
        self.client.close()
