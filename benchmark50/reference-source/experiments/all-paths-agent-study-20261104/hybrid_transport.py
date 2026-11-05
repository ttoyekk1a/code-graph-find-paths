"""Episode-local routing between Chat and background Responses."""
import json
from pathlib import Path
import time

import httpx

from async_transport import AsyncTransport
from support import CONFIG, save
from transport import ChatTransport, ModelServiceError, validate_connection


ASYNC_DEFAULT = 'guided-legacy-tools-agent'
CHAT_TIMEOUT_SECONDS = 65
FALLBACK_STATUS_CODES = (408, 504)
USAGE_COUNTERS = (
    'physical_generation_attempts', 'chat_generation_attempts', 'async_generation_attempts',
    'unconfirmed_generation_attempts', 'unknown_chat_usage_attempts', 'unknown_async_usage_attempts',
    'known_input_tokens_lower_bound', 'known_output_tokens_lower_bound', 'fallback_rounds',
)


def validate_hybrid_connection(config):
    if (config.get('protocol') != 'hybrid_chat_responses'
            or config.get('async_default_arms') != [ASYNC_DEFAULT]
            or config.get('fallback_status_codes') != list(FALLBACK_STATUS_CODES)
            or config.get('sticky_async_after_timeout') is not True
            or config.get('model') != 'Gemma-4-31B'):
        raise ValueError('Expected the frozen Gemma-4-31B hybrid policy')
    checked = validate_connection({**config, 'protocol': 'chat_completions'})
    return {**checked, 'protocol': config['protocol']}


def _receipt(path):
    try:
        return json.loads((path / 'http.json').read_text())
    except FileNotFoundError:
        return {}


class HybridTransport:
    def __init__(self, connection, arm, *, chat=None, responses=None):
        connection = validate_hybrid_connection(connection)
        if arm not in CONFIG['arms']:
            raise ValueError('Unknown study arm')
        self.arm = arm
        self.mode = 'async' if arm == ASYNC_DEFAULT else 'chat'
        self.chat = chat if chat is not None else ChatTransport({**connection, 'protocol': 'chat_completions'})
        self.responses = responses if responses is not None else AsyncTransport(
            {**connection, 'protocol': 'responses_background_stateless'})

    def __call__(self, request, out, remaining_seconds):
        started = time.monotonic()
        deadline = started + remaining_seconds
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        route_path = out / 'route.json'
        route = {'arm': self.arm, 'initial_mode': self.mode, 'attempts': [],
                 'fallback_reason': None, 'prior_chat_usage_unknown': False, 'state': 'routing'}
        with route_path.open('x') as claim:
            json.dump(route, claim)

        def attempt(mode, transport, max_seconds=None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Hybrid round deadline reached')
            path = out / f'{mode}-attempt'
            path.mkdir()
            attempt_started = time.monotonic()
            entry = {'mode': mode, 'directory': path.name, 'state': 'intent'}
            route['attempts'].append(entry)
            save(route_path, route)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    entry['state'] = 'not_started'
                    raise TimeoutError('Hybrid round deadline reached')
                response = transport(request, path, min(remaining, max_seconds) if max_seconds else remaining)
                entry['state'] = 'returned'
                return response
            except BaseException as exc:
                if entry['state'] != 'not_started':
                    entry['state'] = 'error'
                entry['error_type'] = type(exc).__name__
                raise
            finally:
                receipt = _receipt(path)
                entry['elapsed_seconds'] = time.monotonic() - attempt_started
                entry['status_code'] = receipt.get('status_code')
                entry['usage'] = receipt.get('usage', 'unknown')
                save(route_path, route)

        try:
            if self.mode == 'chat':
                try:
                    response = attempt('chat', self.chat, CHAT_TIMEOUT_SECONDS)
                    route['returned_mode'] = 'chat'
                    return response
                except (TimeoutError, httpx.TimeoutException, ModelServiceError) as exc:
                    if not route['attempts'] or route['attempts'][-1]['state'] == 'not_started':
                        raise
                    chat_entry = route['attempts'][-1]
                    status = chat_entry.get('status_code')
                    if not isinstance(exc, (TimeoutError, httpx.TimeoutException)) and status not in FALLBACK_STATUS_CODES:
                        raise
                    self.mode = 'async'
                    route['fallback_reason'] = f'HTTP {status}' if status in FALLBACK_STATUS_CODES else type(exc).__name__
                    route['prior_chat_usage_unknown'] = chat_entry.get('usage', 'unknown') == 'unknown'
                    save(route_path, route)
            response = attempt('async', self.responses)
            route['returned_mode'] = 'async'
            return response
        except BaseException as exc:
            route['error_type'] = type(exc).__name__
            raise
        finally:
            route['state'] = 'returned' if 'returned_mode' in route else 'error'
            route['next_mode'] = self.mode
            route['elapsed_seconds'] = time.monotonic() - started
            save(route_path, route)

    def close(self):
        try:
            self.chat.close()
        finally:
            self.responses.close()


def transport_usage(out):
    totals = dict.fromkeys(USAGE_COUNTERS, 0)
    for route_path in sorted((Path(out) / 'rounds').glob('*/route.json')):
        route = json.loads(route_path.read_text())
        totals['fallback_rounds'] += int(route['fallback_reason'] is not None)
        for entry in route['attempts']:
            mode = entry['mode']
            receipt = _receipt(route_path.parent / entry['directory'])
            if not receipt:
                if entry['state'] != 'not_started':
                    totals['unconfirmed_generation_attempts'] += 1
                continue
            if receipt.get('creation_attempts' if mode == 'async' else 'attempts') != 1:
                continue
            totals['physical_generation_attempts'] += 1
            totals[f'{mode}_generation_attempts'] += 1
            usage = receipt.get('usage')
            keys = ('input_tokens', 'output_tokens') if mode == 'async' else ('prompt_tokens', 'completion_tokens')
            if isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0 for key in keys):
                totals['known_input_tokens_lower_bound'] += usage[keys[0]]
                totals['known_output_tokens_lower_bound'] += usage[keys[1]]
            else:
                totals[f'unknown_{mode}_usage_attempts'] += 1
    return _with_completeness(totals)


def _with_completeness(totals, missing=0):
    complete = not (missing or totals['unconfirmed_generation_attempts']
                    or totals['unknown_chat_usage_attempts'] or totals['unknown_async_usage_attempts'])
    return {**totals, 'physical_usage_complete': complete}


def summarize_transport(rows, *, missing=0):
    totals = dict.fromkeys(USAGE_COUNTERS, 0)
    for row in rows:
        usage = row.get('transport_usage')
        if usage is None:
            missing += 1
            continue
        for key in totals:
            totals[key] += usage[key]
    return {**_with_completeness(totals, missing), 'episodes_without_transport_usage': missing}
