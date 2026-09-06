from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal as D
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import subprocess

from tmon.cli import envelope, parser, main, render
from tmon.brief import run_brief, classify_news
from tmon.brief_data import calendar_context, index_prices, index_change, investor_flow, freshness, dated_index_fallback
from tmon.brief_research import validate, canonical_url, research, cache_key, empty
from tmon.brief_store import BriefStore, scope_key
from tmon.client import endpoint_group
from tmon.errors import TmonError
from tmon.watchlist import Watchlist
from tests.test_recommend import NOW, cal, candle


def story():
    return {'status': 'completed', 'summary': [{'text': '공식 발표 확인', 'sourceIds': ['s1']}],
        'news': [{'headline': '국내 기업 발표', 'summary': '공식 공시 내용', 'impact': '업황 기대에 영향을 줄 수 있다.',
                  'category': 'domestic', 'symbols': [], 'sourceIds': ['s1'], 'eventAt': None}],
        'upcomingEvents': [{'text': '다음 발표', 'eventAt': (NOW + timedelta(days=1)).isoformat(), 'sourceIds': ['s1']}],
        'sources': [{'id': 's1', 'url': 'https://example.com/news?id=1&utm_source=test', 'title': '공식 발표',
                     'publishedAt': (NOW - timedelta(hours=1)).isoformat()}]}


class Client:
    def __init__(self, closed=False):
        self.calls = []
        self.closed = closed
    def get(self, path, **params):
        self.calls.append((path, params))
        if 'market-calendar' in path:
            day = datetime.fromisoformat(params['date']).replace(tzinfo=NOW.tzinfo)
            result = cal(day)
            if self.closed and params['date'] == NOW.date().isoformat():
                result['today']['integrated'] = None
            return result
        if path.endswith('/prices') and 'market-indicators' in path:
            return [{'symbol': s, 'lastPrice': '110', 'timestamp': NOW.isoformat()} for s in ('KOSPI', 'KOSDAQ')]
        if path.endswith('/candles'):
            day = NOW.replace(hour=9, minute=0, second=0)
            return {'candles': [candle(day - timedelta(days=1), '100'), candle(day - timedelta(days=2), '90', '89', '91')], 'nextBefore': None}
        if path.endswith('/investor-trading'):
            return {'records': [{'date': params['until'], 'updatedAt': NOW.isoformat(),
                    'individual': {'buyAmount': '30', 'sellAmount': '50'},
                    'foreigner': {'buyAmount': '50', 'sellAmount': '10'},
                    'institution': {'buyAmount': '10', 'sellAmount': '30'},
                    'otherCorporation': {'buyAmount': '10', 'sellAmount': '10'}}]}
        if path == '/api/v1/rankings':
            return {'rankedAt': NOW.isoformat(), 'rankings': []}
        if path == '/api/v1/prices':
            return [{'symbol': s, 'currency': 'KRW', 'lastPrice': '100', 'timestamp': NOW.isoformat()} for s in params['symbols'].split(',')]
        if path == '/api/v1/stocks':
            return [{'symbol': s, 'name': '종목 ' + s, 'market': 'KOSPI'} for s in params['symbols'].split(',')]
        raise AssertionError(path)


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BriefStore(Path(self.tmp.name).resolve() / 'brief')
        self.args = parser().parse_args(['brief'])
    def tearDown(self):
        self.tmp.cleanup()
    def run_brief(self, **kwargs):
        result = envelope('brief')
        defaults = dict(client=Client(), store=self.store, now=lambda: NOW,
                        researcher=lambda *a, **k: (deepcopy(story()), {'cacheHit': False, 'asOf': NOW.isoformat(), 'model': 'test'}))
        defaults.update(kwargs)
        code = run_brief(self.args, result, **defaults)
        return code, result

    def test_calendar_auto_boundaries_and_holiday(self):
        raw = cal(NOW)
        for hour, minute, expected in [(8, 59, 'premarket'), (9, 0, 'intraday'), (15, 29, 'intraday'), (15, 30, 'close')]:
            at = NOW.replace(hour=hour, minute=minute)
            self.assertEqual(calendar_context(raw, at)['session'], expected)
        raw['today']['integrated'] = None
        ctx = calendar_context(raw, NOW)
        self.assertEqual(ctx['phase'], 'closed')
        self.assertEqual(ctx['referenceDate'], raw['previousBusinessDay']['date'])

    def test_calendar_uses_kst_midnight_and_delayed_open(self):
        at = NOW.replace(hour=0, minute=10)
        self.assertEqual(calendar_context(cal(at), at.astimezone(__import__('datetime').timezone.utc))['date'], at.date().isoformat())
        raw = cal(NOW)
        raw['today']['integrated']['regularMarket']['startTime'] = NOW.replace(hour=10, minute=30).isoformat()
        self.assertEqual(calendar_context(raw, NOW)['phase'], 'premarket')

    def test_calendar_rejects_wrong_date_or_reversed_session(self):
        for mutate in (lambda r: r['today'].update(date='2000-01-01'),
                       lambda r: r['today']['integrated']['regularMarket'].update(endTime=NOW.replace(hour=8).isoformat())):
            raw = cal(NOW)
            mutate(raw)
            with self.assertRaises(TmonError):
                calendar_context(raw, NOW)

    def test_index_change_uses_exact_previous_trading_day(self):
        client = Client()
        rows = index_prices(client, calendar_context(cal(), NOW), NOW)
        index_change(client, rows[0], NOW)
        self.assertEqual(rows[0]['changePct'], D('10'))
        self.assertEqual(rows[0]['baseDate'], '2026-09-03')

    def test_missing_baseline_does_not_invent_change(self):
        client = Client()
        row = index_prices(client, calendar_context(cal(), NOW), NOW)[0]
        original = client.get
        client.get = lambda p, **kw: {'candles': [candle(NOW - timedelta(days=2))]} if p.endswith('/candles') else original(p, **kw)
        with self.assertRaises(TmonError):
            index_change(client, row, NOW)
        self.assertIsNone(row['changePct'])

    def test_null_quote_timestamp_falls_back_to_separate_completed_bar(self):
        row = {'symbol': 'KOSPI', 'lastPrice': D('110'), 'providerLastPrice': D('110'), 'providerAsOf': None}
        client = MagicMock()
        client.get.return_value = {'candles': [candle(NOW.replace(second=0), '120', '119', '121'), candle(NOW.replace(second=0) - timedelta(minutes=1), '105', '104', '106')]}
        dated_index_fallback(client, row, calendar_context(cal(), NOW), NOW)
        self.assertEqual(row['lastPrice'], D('105'))
        self.assertEqual(row['providerLastPrice'], D('110'))
        self.assertEqual(row['priceBasis'], 'completed-minute-close')
        self.assertEqual(row['asOf'], (NOW.replace(second=0) - timedelta(minutes=1)).isoformat())
        self.assertEqual(row['timeBasis'], 'bar-start')

    def test_turnover_is_one_sided_and_net_is_signed(self):
        flow = investor_flow(Client(), 'KOSPI', calendar_context(cal(), NOW), NOW)
        self.assertEqual(flow['turnoverKrw'], D('100'))
        self.assertEqual(flow['netBuyingKrw']['foreigner'], D('40'))
        self.assertEqual(flow['netBuyingKrw']['individual'], D('-20'))
        self.assertTrue(flow['provisional'])

    def test_flow_rejects_unbalanced_totals(self):
        client = Client()
        row = client.get('/investor-trading', until='2026-09-04')
        row['records'][0]['individual']['buyAmount'] = '31'
        client.get = lambda *a, **k: row
        with self.assertRaises(TmonError):
            investor_flow(client, 'KOSPI', calendar_context(cal(), NOW), NOW)

    def test_freshness_is_market_aware(self):
        ctx = calendar_context(cal(), NOW)
        self.assertEqual(freshness(None, ctx, NOW), 'unknown')
        self.assertEqual(freshness((NOW - timedelta(minutes=4)).isoformat(), ctx, NOW), 'stale')
        with self.assertRaises(TmonError):
            freshness((NOW + timedelta(minutes=1)).isoformat(), ctx, NOW)
        raw = cal()
        raw['today']['integrated'] = None
        self.assertEqual(freshness((NOW - timedelta(days=1)).isoformat(), calendar_context(raw, NOW), NOW), 'dated-snapshot')

    def test_complete_run_records_and_korean_render(self):
        code, result = self.run_brief()
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'ok')
        path = Path(result['meta']['recordPath'])
        saved = json.loads(path.read_text())
        self.assertEqual(saved['data']['market']['indices'][0]['changePct'], '10')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue((path.parent / 'inputs.json').exists())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render(result, False)
        self.assertIn('국내 시황 브리핑', out.getvalue())
        self.assertIn('영향 해석:', out.getvalue())
        self.assertIn('+09:00', out.getvalue())

    def test_profile_is_explicit_bounded_and_unchanged(self):
        watch = Watchlist(Path(self.tmp.name) / 'watch')
        watch.profile('create', '국내')
        watch.apply('add', ['000001', '000002'], '국내')
        before = watch.path.read_bytes()
        self.args.profile, self.args.profile_limit = '국내', 1
        code, result = self.run_brief(watchlist=watch)
        self.assertEqual(code, 0)
        self.assertEqual(result['meta']['selectedSymbols'], ['000001'])
        self.assertEqual(result['meta']['omittedSymbols'], ['000002'])
        self.assertEqual(len(result['data']['market']['watchlist']), 1)
        self.assertEqual(before, watch.path.read_bytes())

    def test_unknown_profile_fails_before_network_or_sdk(self):
        self.args.profile = '없음'
        client, researcher = MagicMock(), MagicMock()
        with self.assertRaises(TmonError):
            self.run_brief(client=client, researcher=researcher, watchlist=Watchlist(Path(self.tmp.name) / 'watch'))
        client.get.assert_not_called()
        researcher.assert_not_called()

    def test_research_off_does_not_call_sdk_or_advance_news_history(self):
        self.args.research = 'off'
        researcher = MagicMock()
        code, result = self.run_brief(researcher=researcher)
        self.assertEqual(code, 0)
        researcher.assert_not_called()
        self.assertEqual(result['data']['research']['status'], 'disabled')
        self.assertIsNone(self.store.cache_read('previous-' + scope_key(None, [])))

    def test_sdk_failure_preserves_market_and_does_not_advance_history(self):
        code, result = self.run_brief(researcher=lambda *a, **k: (empty('research-timeout'), {}))
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['data']['market']['indices'])
        self.assertIsNone(self.store.cache_read('previous-' + scope_key(None, [])))

    def test_market_failure_still_researches_with_unknown_calendar(self):
        client = MagicMock()
        client.get.side_effect = TmonError('network-error', '연결 실패', 4)
        code, result = self.run_brief(client=client)
        self.assertEqual(code, 5)
        self.assertEqual(result['meta']['context']['phase'], 'unknown')
        self.assertEqual(result['data']['research']['status'], 'completed')

    def test_closed_market_still_calls_research(self):
        code, result = self.run_brief(client=Client(closed=True))
        self.assertEqual(result['meta']['context']['session'], 'closed')
        self.assertTrue(result['data']['research']['news'])

    def test_partial_market_failure_preserves_other_sections(self):
        client = Client()
        original = client.get
        def get(path, **kw):
            if 'KOSPI/investor-trading' in path:
                raise TmonError('network-error', '수급 조회 실패', 4)
            return original(path, **kw)
        client.get = get
        code, result = self.run_brief(client=client)
        self.assertEqual(code, 5)
        self.assertEqual(len(result['data']['market']['indices']), 2)
        self.assertEqual(len(result['data']['market']['investorTrading']), 1)

    def test_cached_news_does_not_advance_watermark(self):
        previous = {'asOf': (NOW - timedelta(hours=1)).isoformat(), 'urls': []}
        key = 'previous-' + scope_key(None, [])
        self.store.cache_write(key, previous)
        self.run_brief(researcher=lambda *a, **k: (story(), {'cacheHit': True}))
        self.assertEqual(self.store.cache_read(key), previous)

    def test_slow_research_refreshes_prices_before_output(self):
        elapsed = [0]
        client = Client()
        original = client.get
        def get(path, **kw):
            raw = original(path, **kw)
            if path == '/api/v1/market-indicators/prices' and elapsed[0] > 0:
                for row in raw:
                    row['lastPrice'] = '115'
            return raw
        client.get = get
        def slow(*a, **k):
            elapsed[0] = 45
            return story(), {'cacheHit': False}
        code, result = self.run_brief(client=client, researcher=slow, clock=lambda: elapsed[0],
                                      now=lambda: NOW + timedelta(seconds=elapsed[0]))
        self.assertEqual(code, 0)
        self.assertTrue(result['meta']['marketRefreshed'])
        self.assertEqual(result['data']['market']['indices'][0]['lastPrice'], D('115'))
        self.assertEqual(sum(p == '/api/v1/market-indicators/prices' for p, _ in client.calls), 2)

    def test_failed_final_refresh_keeps_last_observations(self):
        elapsed = [0]
        client = Client()
        original = client.get
        def get(path, **kw):
            if elapsed[0]:
                raise TmonError('network-error', '일시 장애', 4)
            return original(path, **kw)
        client.get = get
        def slow(*a, **k):
            elapsed[0] = 200
            return story(), {'cacheHit': False}
        code, result = self.run_brief(client=client, researcher=slow, clock=lambda: elapsed[0],
                                      now=lambda: NOW + timedelta(seconds=elapsed[0]))
        self.assertEqual(code, 5)
        self.assertFalse(result['meta']['marketRefreshed'])
        self.assertEqual(result['data']['market']['indices'][0]['lastPrice'], D('110'))
        self.assertEqual(result['data']['market']['indices'][0]['freshness'], 'stale')

    def test_overlap_is_rejected(self):
        with self.store.locked(scope_key(None, [])):
            with self.assertRaisesRegex(TmonError, '실행 중'):
                self.run_brief()

    def test_record_failure_preserves_output(self):
        with patch.object(self.store, 'save', side_effect=OSError()):
            code, result = self.run_brief()
        self.assertEqual(code, 0)
        self.assertIn('record-write-failed', [w['code'] for w in result['warnings']])
        self.assertTrue(result['data']['research']['news'])

    def test_cli_validation_and_json_envelope(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['brief', '--timeout', '0', '--json'])
        self.assertEqual(code, 2)
        data = json.loads(out.getvalue())
        self.assertEqual(data['command'], 'brief')
        self.assertEqual(data['error']['code'], 'invalid-brief-timeout')

    def test_only_read_only_indicator_endpoints_allowed(self):
        self.assertEqual(endpoint_group('/api/v1/market-indicators/prices'), 'MARKET_INDICATOR')
        self.assertEqual(endpoint_group('/api/v1/market-indicators/KOSPI/investor-trading'), 'MARKET_INDICATOR')
        self.assertIsNone(endpoint_group('/api/v1/market-indicators/AAPL/investor-trading'))
        self.assertIsNone(endpoint_group('/api/v1/orders'))


class NewsTests(unittest.TestCase):
    def test_valid_story_normalizes_timestamps(self):
        data = validate(story(), NOW)
        self.assertTrue(data['sources'][0]['publishedAt'].endswith('+09:00'))

    def test_rejects_uncited_future_duplicate_and_old_sources(self):
        mutations = [lambda r: r['news'][0].update(sourceIds=['missing']),
                     lambda r: r['sources'][0].update(publishedAt=(NOW + timedelta(seconds=1)).isoformat()),
                     lambda r: r['sources'][0].update(publishedAt=(NOW - timedelta(days=4)).isoformat()),
                     lambda r: r['sources'].append({**r['sources'][0], 'id': 's2'}),
                     lambda r: r['news'][0].update(symbols=['AAPL']),
                     lambda r: r['news'][0].update(eventAt=(NOW + timedelta(days=1)).isoformat()),
                     lambda r: r['sources'][0].update(url='https://user:password@example.com/'),
                     lambda r: r['sources'][0].update(url=None)]
        for mutation in mutations:
            raw = story()
            mutation(raw)
            with self.assertRaises((ValueError, TmonError)):
                validate(raw, NOW)

    def test_unknown_publication_is_explicit(self):
        raw = story()
        raw['sources'][0]['publishedAt'] = None
        self.assertIsNone(validate(raw, NOW)['sources'][0]['publishedAt'])

    def test_comparison_distinguishes_newly_found_from_newly_published(self):
        raw = story()
        previous = {'asOf': (NOW - timedelta(minutes=30)).isoformat(), 'urls': []}
        classify_news(raw, previous, NOW)
        self.assertEqual(raw['news'][0]['updateStatus'], 'newly-found-source')
        raw['sources'][0]['publishedAt'] = (NOW - timedelta(minutes=10)).isoformat()
        classify_news(raw, previous, NOW)
        self.assertEqual(raw['news'][0]['updateStatus'], 'published-since-previous')
        previous['urls'] = [canonical_url(raw['sources'][0]['url'])]
        classify_news(raw, previous, NOW)
        self.assertEqual(raw['news'][0]['updateStatus'], 'previously-covered')

    def test_cache_reuses_news_only_and_refresh_bypasses(self):
        payload = {'asOf': NOW.isoformat(), 'session': 'intraday', 'context': {'phase': 'intraday', 'date': '2026-09-04'}, 'watchlist': []}
        store = MagicMock()
        store.cache_read.return_value = {'result': story(), 'asOf': NOW.isoformat(), 'retrievedAt': NOW.isoformat()}
        with patch('tmon.brief_research.subprocess.Popen') as popen:
            result, meta = research(payload, 5, store)
            self.assertTrue(meta['cacheHit'])
            popen.assert_not_called()
            result, meta = research(payload, 0, store, refresh=True)
            self.assertEqual(result['reason'], 'research-timeout')

    def test_timeout_kills_worker_group(self):
        payload = {'asOf': NOW.isoformat(), 'session': 'intraday', 'context': {'phase': 'intraday', 'date': '2026-09-04'}, 'watchlist': []}
        store = MagicMock()
        store.cache_read.return_value = None
        process = MagicMock(pid=123, returncode=-9)
        process.communicate.side_effect = subprocess.TimeoutExpired('worker', 1)
        with patch('tmon.brief_research.subprocess.Popen', return_value=process), patch('tmon.brief_research.os.killpg') as kill:
            result, _ = research(payload, 1, store)
        self.assertEqual(result['reason'], 'research-timeout')
        kill.assert_called_once()
        process.wait.assert_called_once()

    def test_future_or_corrupt_cache_is_ignored(self):
        payload = {'asOf': NOW.isoformat(), 'session': 'intraday', 'context': {'phase': 'intraday', 'date': '2026-09-04'}, 'watchlist': []}
        for value in ({'result': story(), 'asOf': (NOW + timedelta(days=1)).isoformat(), 'retrievedAt': NOW.isoformat()}, ['invalid']):
            store = MagicMock()
            store.cache_read.return_value = value
            result, meta = research(payload, 0, store)
            self.assertFalse(meta['cacheHit'])
            self.assertEqual(result['status'], 'unavailable')

    def test_subprocess_receives_no_toss_or_api_key_credentials(self):
        payload = {'asOf': NOW.isoformat(), 'session': 'intraday', 'context': {'phase': 'intraday', 'date': '2026-09-04'}, 'watchlist': []}
        store = MagicMock()
        store.cache_read.return_value = None
        process = MagicMock(pid=123, returncode=0)
        process.communicate.return_value = (json.dumps({'error': 'sdk-not-installed'}), '')
        with patch.dict(os.environ, {'TOSS_CLIENT_ID': 'test', 'TOSS_CLIENT_SECRET': 'test', 'OPENAI_API_KEY': 'test'}), patch('tmon.brief_research.subprocess.Popen', return_value=process) as popen, patch('tmon.brief_research.os.killpg'):
            result, _ = research(payload, 1, store)
        env = popen.call_args.kwargs['env']
        self.assertNotIn('TOSS_CLIENT_ID', env)
        self.assertNotIn('TOSS_CLIENT_SECRET', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertEqual(result['reason'], 'sdk-not-installed')

    def test_no_search_event_rejects_worker_output(self):
        payload = {'asOf': NOW.isoformat(), 'session': 'intraday', 'context': {'phase': 'intraday', 'date': '2026-09-04'}, 'watchlist': []}
        store = MagicMock()
        store.cache_read.return_value = None
        process = MagicMock(pid=123, returncode=0)
        process.communicate.return_value = (json.dumps({'result': story(), 'webSearchCount': 0}), '')
        with patch('tmon.brief_research.subprocess.Popen', return_value=process), patch('tmon.brief_research.os.killpg'):
            result, _ = research(payload, 1, store)
        self.assertEqual(result['status'], 'unavailable')
        store.cache_write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
