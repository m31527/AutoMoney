"""Run with OpenTeddy's Python: no model, credentials or external network required."""
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.modules['config'] = types.SimpleNamespace(config=types.SimpleNamespace(
    qwen_base_url='http://unused:11434', openai_api_key='not-a-real-key'))
api = importlib.import_module('automoney_api')
PROPOSAL = dict(symbol='BTCUSDT', action='HOLD', confidence=0.5,
                requested_notional_usd=0, reason='Research', time_horizon_minutes=240, invalid_if=[])


class Response:
    status_code = 200
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def aiter_bytes(self):
        yield json.dumps({'done': True, 'done_reason': 'stop', 'model': 'test:1',
                          'message': {'content': json.dumps(PROPOSAL)}}).encode()


class Upstream:
    calls = 0
    def __init__(self, **kwargs):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    def stream(self, *args, **kwargs):
        Upstream.calls += 1
        return Response()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, AUTOMONEY_TOKEN='x'*64, AUTOMONEY_MODEL='test:1',
                              AUTOMONEY_PROVIDER='ollama', AUTOMONEY_DAILY_CALL_LIMIT='2',
                              AUTOMONEY_ALLOW_OPENAI='false')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = patch.object(api, '_root', Path(self.tmp.name))
        self.root.start()
        self.addCleanup(self.root.stop)
        app = FastAPI()
        app.include_router(api.router)
        self.client = TestClient(app)
        self.payload = dict(provider='ollama', model='test:1', instructions='Return JSON',
                            context=json.dumps({'snapshot': {'symbol': 'BTCUSDT'}}))
        self.headers = {'Authorization': 'Bearer ' + 'x'*64}
        Upstream.calls = 0

    def post(self):
        return self.client.post('/automoney/analyze', headers=self.headers, json=self.payload)

    def test_health_no_inference_and_auth(self):
        self.assertEqual(self.client.get('/automoney/health').status_code, 401)
        self.assertEqual(self.client.get('/automoney/health', headers=self.headers).status_code, 200)
        self.assertEqual(Upstream.calls, 0)

    def test_validated_single_call_and_durable_quota(self):
        with patch.object(api.httpx, 'AsyncClient', Upstream):
            self.assertEqual(self.post().json()['proposal']['action'], 'HOLD')
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(self.post().status_code, 429)
        self.assertEqual(Upstream.calls, 2)

    def test_model_and_cloud_fail_closed(self):
        self.payload['model'] = 'different'
        self.assertEqual(self.post().status_code, 403)
        self.payload.update(model='test:1', provider='openai')
        with patch.dict(os.environ, AUTOMONEY_PROVIDER='openai'):
            self.assertEqual(self.post().status_code, 403)

    def test_invalid_proposal_no_retry(self):
        with patch.dict(PROPOSAL, symbol='ETHUSDT'), patch.object(api.httpx, 'AsyncClient', Upstream):
            result = self.post()
        self.assertEqual(result.status_code, 502)
        self.assertEqual(Upstream.calls, 1)

    def test_timeout_is_classified(self):
        with patch.object(api.httpx, 'AsyncClient', side_effect=TimeoutError()):
            result = self.post()
        self.assertEqual(result.status_code, 504)
        self.assertEqual(result.json()['detail'], 'UPSTREAM_TIMEOUT')

    def test_openai_explicit_opt_in(self):
        async def cloud_bytes(_):
            yield json.dumps({'model': 'test:1-snapshot', 'choices': [{
                'finish_reason': 'stop', 'message': {'content': json.dumps(PROPOSAL)}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}).encode()
        self.payload['provider'] = 'openai'
        with patch.dict(os.environ, AUTOMONEY_PROVIDER='openai', AUTOMONEY_ALLOW_OPENAI='true'), \
             patch.object(api.httpx, 'AsyncClient', Upstream), \
             patch.object(Response, 'aiter_bytes', cloud_bytes):
            result = self.post()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['model'], 'test:1-snapshot')
        self.assertEqual(result.json()['usage']['prompt_tokens'], 100)
        self.assertEqual(Upstream.calls, 1)


    def test_compact_receipt_and_budget_validation(self):
        receipt = dict(marker='abc', can_buy=True, can_sell=False,
                       max_buy_notional_usd='100', horizon_minutes=240,
                       round_trip_cost_pct='0.4', future_exit_allowed_subject_to_checks=True)
        self.payload['context'] = json.dumps(dict(research_version='compact-entry-v4',
            snapshot={'symbol': 'BTCUSDT'}, input_receipt=receipt,
            max_buy_notional_usd='100', max_sell_notional_usd='0'))
        with patch.dict(os.environ, AUTOMONEY_DAILY_CALL_LIMIT='10'), \
             patch.object(api.httpx, 'AsyncClient', Upstream), \
             patch.dict(PROPOSAL, research_check=receipt, time_horizon_minutes=240):
            self.assertTrue(self.post().json()['input_audit']['receipt_verified'])
            with patch.dict(PROPOSAL, research_check={**receipt, 'can_buy': False}):
                self.assertEqual(self.post().json()['detail'], 'INPUT_RECEIPT_MISMATCH')
            with patch.dict(PROPOSAL, time_horizon_minutes=15):
                self.assertEqual(self.post().json()['detail'], 'RESEARCH_HORIZON_MISMATCH')
            with patch.dict(PROPOSAL, action='BUY', requested_notional_usd=101):
                self.assertEqual(self.post().json()['detail'], 'RESEARCH_BUDGET_EXCEEDED')

    def test_replay_opt_in_pin_and_separate_quota(self):
        response = self.client.post('/automoney/replay', headers=self.headers, json=self.payload)
        self.assertEqual(response.status_code, 403)
        with patch.dict(os.environ, AUTOMONEY_REPLAY_ENABLED='true',
                        AUTOMONEY_REPLAY_DAILY_CALL_LIMIT='1', AUTOMONEY_MODEL='test:1'), \
             patch.object(api.httpx, 'AsyncClient', Upstream):
            self.assertEqual(self.client.post('/automoney/replay', headers=self.headers,
                                             json=self.payload).status_code, 200)
            self.assertEqual(self.client.post('/automoney/replay', headers=self.headers,
                                             json=self.payload).status_code, 429)
            self.assertEqual(self.post().status_code, 200)
            self.payload.update(provider='openai', model='cloud:1')
            self.assertEqual(self.post().status_code, 403)

    def test_input_limit(self):
        self.payload['context'] = 'x'*160001
        self.assertEqual(self.post().status_code, 413)


if __name__ == '__main__':
    unittest.main()
