import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from async_transport import AsyncTransport
from hybrid_transport import HybridTransport, summarize_transport, transport_usage, validate_hybrid_connection
from run_parallel_hybrid import main
from support import HERE, save
from transport import ChatTransport, ModelServiceError

CONFIG = json.loads((HERE / 'config/connection.hybrid-gemma31b.json').read_text())
GUIDED = 'guided-legacy-tools-agent'
CHAT = 'legacy-tools-agent'
REQUEST = {'messages': [{'role': 'tool', 'tool_call_id': 'call_1', 'content': 'prior output'}]}
RESPONSE = {'usage': {'prompt_tokens': 10, 'completion_tokens': 3}}


class Attempt:
    def __init__(self, *, error=None, status=200, async_mode=False):
        self.calls = []
        self.error = error
        self.status = status
        self.async_mode = async_mode

    def __call__(self, request, path, remaining):
        self.calls.append((request, path, remaining))
        usage = {'input_tokens': 10, 'output_tokens': 3} if self.async_mode else RESPONSE['usage']
        save(path / 'http.json', {
            'status_code': self.status,
            'creation_attempts': int(self.async_mode),
            'attempts': int(not self.async_mode),
            'usage': 'unknown' if self.error else usage,
        })
        if self.error:
            raise self.error
        return RESPONSE

    def close(self):
        pass


class HybridTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name)
        self.round = self.out / 'rounds/001'
        self.async_attempt = Attempt(async_mode=True)

    def hybrid(self, chat=None, arm=CHAT):
        return HybridTransport(CONFIG, arm, chat=chat or Attempt(), responses=self.async_attempt)

    def test_arm_defaults(self):
        for arm in CONFIG['async_default_arms'] + ['legacy-tools-agent', 'all-paths-tool-agent']:
            with self.subTest(arm=arm):
                chat = Attempt()
                transport = self.hybrid(chat, arm)
                transport(REQUEST, self.out / arm, 120)
                self.assertEqual(bool(chat.calls), arm != GUIDED)
        self.assertEqual(len(self.async_attempt.calls), 1)

    def test_gateway_and_local_timeouts_fall_back_once_and_stick(self):
        errors = [(ModelServiceError('timeout'), status) for status in (408, 504)]
        errors += [(TimeoutError(), None), (httpx.ReadTimeout('timeout'), None)]
        for i, (error, status) in enumerate(errors):
            with self.subTest(error=error, status=status):
                path = self.out / str(i)
                chat = Attempt(error=error, status=status)
                transport = self.hybrid(chat)
                self.assertEqual(transport(REQUEST, path / '001', 120), RESPONSE)
                transport(REQUEST, path / '002', 120)
                self.assertEqual(len(chat.calls), 1)
                self.assertIs(chat.calls[0][0], self.async_attempt.calls[-2][0])
                self.assertLessEqual(chat.calls[0][2], 65)
                self.assertTrue((path / '001/chat-attempt/http.json').is_file())
                self.assertTrue((path / '001/async-attempt/http.json').is_file())
        self.assertEqual(len(self.async_attempt.calls), 8)

    def test_http_400_and_valid_chat_do_not_fall_back(self):
        with self.assertRaises(ModelServiceError):
            self.hybrid(Attempt(error=ModelServiceError('bad request'), status=400))(REQUEST, self.round, 120)
        self.hybrid()(REQUEST, self.out / 'rounds/002', 120)
        self.assertEqual(self.async_attempt.calls, [])

    def test_deadline_is_shared_and_exhaustion_prevents_fallback(self):
        chat = Attempt(error=TimeoutError(), status=None)
        with patch('hybrid_transport.time.monotonic', side_effect=lambda: 100 + 65 * bool(chat.calls)):
            self.hybrid(chat)(REQUEST, self.round, 80)
        self.assertEqual(self.async_attempt.calls[0][2], 15)
        chat = Attempt(error=TimeoutError(), status=None)
        with patch('hybrid_transport.time.monotonic', side_effect=lambda: 100 + 65 * bool(chat.calls)):
            with self.assertRaises(TimeoutError):
                self.hybrid(chat)(REQUEST, self.out / 'rounds/002', 65)
        self.assertEqual(len(self.async_attempt.calls), 1)

    def test_physical_usage_lower_bound_and_exclusive_round(self):
        chat = Attempt(error=TimeoutError(), status=None)
        transport = self.hybrid(chat)
        transport(REQUEST, self.round, 120)
        with self.assertRaises(FileExistsError):
            transport(REQUEST, self.round, 120)
        self.assertEqual(len(self.async_attempt.calls), 1)
        route = json.loads((self.round / 'route.json').read_text())
        self.assertEqual([r['mode'] for r in route['attempts']], ['chat', 'async'])
        usage = transport_usage(self.out)
        self.assertEqual(usage['physical_generation_attempts'], 2)
        self.assertEqual(usage['known_input_tokens_lower_bound'], 10)
        self.assertEqual(usage['known_output_tokens_lower_bound'], 3)
        self.assertEqual(usage['unknown_chat_usage_attempts'], 1)
        self.assertFalse(usage['physical_usage_complete'])
        summary = summarize_transport([{'transport_usage': usage}, {}])
        self.assertEqual(summary['episodes_without_transport_usage'], 1)
        self.assertFalse(summary['physical_usage_complete'])

    def test_real_transport_interfaces_with_mock_http(self):
        calls = []

        def handle(request):
            calls.append((request.method, request.url.path, json.loads(request.content)))
            if request.url.path.endswith('/chat/completions'):
                return httpx.Response(504, text='gateway timeout')
            if len(calls) == 2:
                return httpx.Response(200, json={
                    'id': 'resp_owned', 'status': 'completed', 'model': CONFIG['model'],
                    'output': [{'type': 'function_call', 'call_id': 'call_next',
                                'name': 'get_code_neighbors', 'arguments': '{}'}],
                    'usage': {'input_tokens': 10, 'output_tokens': 3},
                })
            return httpx.Response(504, text='creation timed out')

        connection = {**CONFIG, 'base_url': 'https://example.invalid/v1'}
        chat = ChatTransport({**connection, 'protocol': 'chat_completions'},
                             client=httpx.Client(transport=httpx.MockTransport(handle)))
        responses = AsyncTransport({**connection, 'protocol': 'responses_background_stateless'},
                                   client=httpx.Client(transport=httpx.MockTransport(handle)))
        hybrid = HybridTransport(connection, CHAT, chat=chat, responses=responses)
        self.addCleanup(hybrid.close)
        request = {'messages': [{'role': 'user', 'content': 'question'},
                                {'role': 'assistant', 'content': None, 'tool_calls': [{
                                    'id': 'call_1', 'type': 'function',
                                    'function': {'name': 'get_code_neighbors', 'arguments': '{}'},
                                }]}, *REQUEST['messages']]}
        answer = hybrid(request, self.round, 120)
        self.assertEqual(answer['choices'][0]['message']['tool_calls'][0]['id'], 'call_next')
        with self.assertRaises(ModelServiceError):
            hybrid(request, self.out / 'rounds/002', 120)
        self.assertEqual([(method, path) for method, path, _ in calls],
                         [('POST', '/v1/chat/completions'), ('POST', '/v1/responses'), ('POST', '/v1/responses')])
        self.assertEqual(calls[0][2]['messages'], request['messages'])
        async_input = calls[1][2]['input']
        self.assertEqual(async_input[-2]['call_id'], 'call_1')
        self.assertEqual(async_input[-1]['output'], 'prior output')
        self.assertNotIn('previous_response_id', calls[1][2])
        self.assertEqual(transport_usage(self.out)['unknown_async_usage_attempts'], 1)

    def test_invalid_policy_and_no_implicit_launch(self):
        with self.assertRaises(ValueError):
            validate_hybrid_connection({**CONFIG, 'fallback_status_codes': [400]})
        with patch('run_parallel_hybrid.dispatch') as dispatch:
            with self.assertRaises(SystemExit):
                main([])
            dispatch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
