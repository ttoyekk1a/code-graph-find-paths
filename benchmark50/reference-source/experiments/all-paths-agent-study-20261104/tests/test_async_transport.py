from pathlib import Path
import json
import sys
import tempfile
import time
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from async_transport import AsyncTransport, validate_async_connection
from transport import ModelServiceError


def connection(**overrides):
    return {
        'enabled': True, 'base_url': 'https://example.invalid/v1', 'model': 'provided-model',
        'api_key_env': 'GRAPH_STUDY_ASYNC_KEY', 'api_key_default': 'offline-secret',
        'protocol': 'responses_background_stateless', 'stream': False, **overrides,
    }


def reply(status, response_id='resp_owned', **overrides):
    return httpx.Response(200, json={
        'id': response_id, 'status': status, 'model': 'provided-model', 'output': [],
        **overrides,
    })


class AsyncTransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name)
        self.calls = []

    def read(self, filename='http.json'):
        return json.loads((self.out / filename).read_text())

    def transport(self, handler, **options):
        def record(request):
            self.calls.append((request.method, request.url.path))
            return handler(request)
        client = httpx.Client(transport=httpx.MockTransport(record))
        transport = AsyncTransport(connection(), client=client, poll_interval=0.01, **options)
        self.addCleanup(transport.close)
        return transport

    def run_round(self, transport, seconds=1):
        return transport({'messages': [{'role': 'user', 'content': 'hello'}]}, self.out, seconds)

    def test_future_date_connection_and_validation(self):
        from unittest.mock import patch
        import datetime
        future = datetime.datetime(2040, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
        with patch('time.time', return_value=future):
            self.assertEqual(validate_async_connection(connection())['model'], 'provided-model')
            self.assertEqual(self.run_round(self.transport(lambda _: reply('completed')))['id'], 'resp_owned')
        for overrides in ({'enabled': False}, {'protocol': 'chat_completions'}, {'stream': True},
                          {'base_url': 'https://user:secret@example.invalid'}, {'base_url': 'https://example.invalid?secret=1'},
                          {'model': ''}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                validate_async_connection(connection(**overrides))

    def test_creation_intent_durable_then_poll_same_id_and_retry(self):
        def handler(request):
            if request.method == 'POST':
                self.assertEqual(self.read()['state'], 'creation_intent')
                body = self.read('wire-request.json')
                self.assertEqual(body, json.loads(request.content))
                self.assertEqual(body['model'], 'provided-model')
                self.assertIs(body['background'], True)
                self.assertIs(body['store'], True)
                self.assertIs(body['stream'], False)
                self.assertNotIn('previous_response_id', body)
                return reply('queued')
            self.assertEqual(self.read()['response_id'], 'resp_owned')
            if len(self.calls) == 2:
                return httpx.Response(504)
            if len(self.calls) == 3:
                return reply('in_progress')
            return reply('incomplete', incomplete_details={'reason': 'max_output_tokens'},
                         output=[{'type': 'reasoning', 'summary': ['audit only']}],
                         usage={'input_tokens': 5, 'output_tokens': 9, 'output_tokens_details': {'reasoning_tokens': 8}})
        chat = self.run_round(self.transport(handler))
        self.assertEqual(chat['choices'][0]['finish_reason'], 'length')
        self.assertIsNone(chat['choices'][0]['message']['content'])
        self.assertEqual(chat['usage']['completion_tokens'], 9)
        self.assertEqual(self.read()['usage']['output_tokens'], 9)
        self.assertEqual(self.calls, [('POST', '/v1/responses')] + [('GET', '/v1/responses/resp_owned')] * 3)
        for path in self.out.rglob('*'):
            if path.is_file():
                self.assertNotIn('offline-secret', path.read_text())

    def test_ambiguous_creation_is_never_retried(self):
        for handler in (lambda _: httpx.Response(504), lambda req: self.raise_reset(req)):
            with self.subTest(handler=handler):
                self.calls.clear()
                with self.assertRaises((ModelServiceError, httpx.TransportError)):
                    self.run_round(self.transport(handler))
                self.assertEqual(self.calls, [('POST', '/v1/responses')])
                self.assertIsNone(self.read()['response_id'])
                self.assertEqual(self.read()['usage'], 'unknown')
                self.out = self.out / 'next-round'

    @staticmethod
    def raise_reset(request):
        raise httpx.ReadError('connection reset', request=request)

    def test_poll_id_mismatch_cancels_only_persisted_id(self):
        def handler(request):
            if len(self.calls) == 1:
                return reply('queued')
            if len(self.calls) == 2:
                return reply('completed', response_id='resp_unowned')
            self.assertEqual(self.read()['response_id'], 'resp_owned')
            return reply('cancelled')
        with self.assertRaises(ModelServiceError):
            self.run_round(self.transport(handler))
        self.assertEqual(self.calls, [
            ('POST', '/v1/responses'), ('GET', '/v1/responses/resp_owned'),
            ('POST', '/v1/responses/resp_owned/cancel'), ('GET', '/v1/responses/resp_owned'),
        ])
        self.assertEqual(self.read()['state'], 'cancelled')

    def test_invalid_creation_id_never_poll_or_cancel(self):
        with self.assertRaises(ModelServiceError):
            self.run_round(self.transport(lambda _: reply('queued', response_id='resp_../other')))
        self.assertEqual(self.calls, [('POST', '/v1/responses')])

    def test_no_creation_after_deadline(self):
        with self.assertRaises(TimeoutError):
            self.run_round(self.transport(lambda _: self.fail('unexpected request')), seconds=0)
        self.assertEqual(self.calls, [])

    def test_deadline_leaves_time_for_bounded_cleanup(self):
        def handler(request):
            if request.url.path.endswith('/cancel'):
                time.sleep(1)
            return reply('queued')
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.run_round(self.transport(handler), seconds=0.18)
        self.assertLess(time.monotonic() - start, 0.35)
        self.assertIn(('POST', '/v1/responses/resp_owned/cancel'), self.calls)
        self.assertEqual(self.read()['response_id'], 'resp_owned')
        self.assertEqual(self.read()['usage'], 'unknown')
        self.assertEqual(self.read()['state'], 'cleanup_unconfirmed')

    def test_interrupt_cleanup_preserves_original_exception(self):
        def handler(request):
            if len(self.calls) == 1:
                return reply('in_progress')
            if len(self.calls) == 2:
                raise KeyboardInterrupt('original interruption')
            return httpx.Response(503)
        with self.assertRaisesRegex(KeyboardInterrupt, 'original interruption'):
            self.run_round(self.transport(handler))
        self.assertEqual(self.calls[2:], [
            ('POST', '/v1/responses/resp_owned/cancel'), ('GET', '/v1/responses/resp_owned'),
        ])
        self.assertEqual(self.read()['state'], 'cleanup_unconfirmed')

    def test_transient_get_retries_are_capped(self):
        def handler(request):
            if len(self.calls) == 1:
                return reply('queued')
            if request.method == 'GET':
                return httpx.Response(503)
            return reply('cancelled')
        with self.assertRaises(ModelServiceError):
            self.run_round(self.transport(handler))
        cancel_index = self.calls.index(('POST', '/v1/responses/resp_owned/cancel'))
        self.assertEqual(self.calls[1:cancel_index], [('GET', '/v1/responses/resp_owned')] * 4)

    def test_round_directory_cannot_create_twice(self):
        transport = self.transport(lambda _: reply('completed'))
        self.run_round(transport)
        original = self.read()
        with self.assertRaises(FileExistsError):
            self.run_round(transport)
        self.assertEqual(self.calls, [('POST', '/v1/responses')])
        self.assertEqual(self.read(), original)

    def test_nontransient_get_transport_failure_skips_poll_retries(self):
        def handler(request):
            if len(self.calls) == 1:
                return reply('queued')
            if len(self.calls) == 2:
                raise httpx.UnsupportedProtocol('invalid scheme', request=request)
            return reply('cancelled')
        with self.assertRaises(httpx.UnsupportedProtocol):
            self.run_round(self.transport(handler))
        self.assertEqual(self.calls, [
            ('POST', '/v1/responses'), ('GET', '/v1/responses/resp_owned'),
            ('POST', '/v1/responses/resp_owned/cancel'), ('GET', '/v1/responses/resp_owned'),
        ])

    def test_unconfirmed_cancel_keeps_last_provider_status(self):
        def handler(request):
            if len(self.calls) == 1:
                return reply('queued')
            if len(self.calls) == 2:
                return reply('in_progress')
            if len(self.calls) == 3:
                raise KeyboardInterrupt()
            return httpx.Response(503)
        with self.assertRaises(KeyboardInterrupt):
            self.run_round(self.transport(handler))
        self.assertEqual(self.read()['state'], 'cleanup_unconfirmed')
        self.assertEqual(self.read()['provider_status'], 'in_progress')

    def test_redacts_echoed_secret_from_receipt_and_raw_body(self):
        secret = connection()['api_key_default']
        def handler(_):
            return reply('completed', output=[{'type': 'reasoning', 'summary': [secret]}],
                         usage={'input_tokens': 1, 'output_tokens': 2, 'detail': secret})
        self.run_round(self.transport(handler))
        for path in self.out.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret, path.read_text())

    def test_interrupt_after_atomic_owned_write_retains_id_for_cleanup(self):
        from unittest.mock import patch
        import async_transport

        real_save = async_transport.save
        interrupted = False

        def interrupted_save(path, data):
            nonlocal interrupted
            real_save(path, data)
            if Path(path).name == 'http.json' and data.get('state') == 'owned' and not interrupted:
                interrupted = True
                raise KeyboardInterrupt('after owned write')

        def handler(request):
            if len(self.calls) == 1:
                return reply('queued')
            self.assertEqual(self.read()['response_id'], 'resp_owned')
            return reply('cancelled')

        with patch.object(async_transport, 'save', side_effect=interrupted_save):
            with self.assertRaisesRegex(KeyboardInterrupt, 'after owned write'):
                self.run_round(self.transport(handler))
        self.assertEqual(self.calls, [
            ('POST', '/v1/responses'), ('POST', '/v1/responses/resp_owned/cancel'),
            ('GET', '/v1/responses/resp_owned'),
        ])
        self.assertEqual(self.read()['state'], 'cancelled')

    def test_interrupt_after_atomic_terminal_write_preserves_known_usage(self):
        from unittest.mock import patch
        import async_transport

        real_save = async_transport.save
        interrupted = False

        def interrupted_save(path, data):
            nonlocal interrupted
            real_save(path, data)
            if Path(path).name == 'http.json' and data.get('state') == 'completed' and not interrupted:
                interrupted = True
                raise KeyboardInterrupt('after terminal write')

        usage = {'input_tokens': 4, 'output_tokens': 6}
        with patch.object(async_transport, 'save', side_effect=interrupted_save):
            with self.assertRaisesRegex(KeyboardInterrupt, 'after terminal write'):
                self.run_round(self.transport(lambda _: reply('completed', usage=usage)))
        self.assertEqual(self.calls, [('POST', '/v1/responses')])
        self.assertEqual(self.read()['state'], 'completed')
        self.assertEqual(self.read()['usage'], usage)

    def test_interrupt_after_atomic_intent_write_preserves_unattempted_state(self):
        from unittest.mock import patch
        import async_transport

        real_save = async_transport.save
        interrupted = False

        def interrupted_save(path, data):
            nonlocal interrupted
            real_save(path, data)
            if Path(path).name == 'http.json' and data.get('state') == 'creation_intent' and not interrupted:
                interrupted = True
                raise KeyboardInterrupt('after intent write')

        with patch.object(async_transport, 'save', side_effect=interrupted_save):
            with self.assertRaisesRegex(KeyboardInterrupt, 'after intent write'):
                self.run_round(self.transport(lambda _: self.fail('unexpected HTTP request')))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.read()['creation_attempts'], 0)
        self.assertEqual(self.read()['state'], 'creation_not_attempted')

    def test_interrupt_before_atomic_owned_write_cannot_cancel_unpersisted_id(self):
        from unittest.mock import patch
        import async_transport

        real_save = async_transport.save

        def interrupted_save(path, data):
            if Path(path).name == 'http.json' and data.get('state') == 'owned':
                raise KeyboardInterrupt('before owned write')
            real_save(path, data)

        with patch.object(async_transport, 'save', side_effect=interrupted_save):
            with self.assertRaisesRegex(KeyboardInterrupt, 'before owned write'):
                self.run_round(self.transport(lambda _: reply('queued')))
        self.assertEqual(self.calls, [('POST', '/v1/responses')])
        self.assertIsNone(self.read()['response_id'])
        self.assertEqual(self.read()['state'], 'creation_unconfirmed')

    def test_interrupt_after_cleanup_terminal_write_preserves_terminal_usage(self):
        from unittest.mock import patch
        import async_transport

        real_save = async_transport.save
        usage = {'input_tokens': 7, 'output_tokens': 2}
        interrupted = False

        def interrupted_save(path, data):
            nonlocal interrupted
            real_save(path, data)
            if Path(path).name == 'http.json' and data.get('state') == 'cancelled' and not interrupted:
                interrupted = True
                raise KeyboardInterrupt('after cancellation write')

        def handler(_):
            if len(self.calls) == 1:
                return reply('queued')
            if len(self.calls) == 2:
                raise KeyboardInterrupt('original interrupt')
            return reply('cancelled', usage=usage)

        with patch.object(async_transport, 'save', side_effect=interrupted_save):
            with self.assertRaisesRegex(KeyboardInterrupt, 'original interrupt'):
                self.run_round(self.transport(handler))
        self.assertEqual(self.read()['state'], 'cancelled')
        self.assertEqual(self.read()['usage'], usage)
