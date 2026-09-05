import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from tmon.cli import main, envelope
from tmon.client import endpoint_group, TossClient, Transport
from tmon.errors import TmonError
from tmon.intraday import completed_minutes, five_minutes, fresh, session
from tmon.recommend_config import load_config
from tmon.recommend_data import orderbook, stock_info, warnings_ok
from tmon.recommend import Engine
from tmon.recommend_render import render_recommend
from tmon.recommend_store import Store
from tmon.research import safe_env, validate, research, unavailable
from tmon.strategies import signal, entry, NoMatch, sort_key

KST = timezone(timedelta(hours=9))
NOW = datetime(2026,9,4,10,0,10,tzinfo=KST)
START = NOW.replace(hour=9,minute=0,second=0)


def cal(now=NOW):
    def day(d):
        return {'date':d.date().isoformat(),'integrated':{'preMarket':None,'afterMarket':None,
                'regularMarket':{'startTime':d.replace(hour=9,minute=0,second=0).isoformat(),
                  'singlePriceAuctionStartTime':d.replace(hour=15,minute=20,second=0).isoformat(),
                  'endTime':d.replace(hour=15,minute=30,second=0).isoformat()}}}
    return {'today':day(now),'previousBusinessDay':day(now-timedelta(days=1)),
            'nextBusinessDay':day(now+timedelta(days=3))}


def candle(t, close='100', low='99', high='101', volume='100000000', op=None):
    return {'timestamp':t.isoformat(),'currency':'KRW','openPrice':op or close,'highPrice':high,
            'lowPrice':low,'closePrice':close,'volume':volume}


def days():
    # Latest completed day breaks out; previous SMA20 > SMA60, rising SMA20.
    rows=[]
    for i in range(120):
        close = '80' if i<70 else '90' if i<100 else '100'
        rows.append(candle(START-timedelta(days=120-i),close,str(D(close)-1),str(D(close)+1)))
    rows[-1]=candle(START-timedelta(days=1),'102','100','103','200000000')
    return rows


def minutes():
    rows=[candle(START+timedelta(minutes=i),'100','99','101','100000') for i in range(60)]
    for i in range(55,60):
        rows[i]=candle(START+timedelta(minutes=i),'101.2','100','101.3','200000',op='100')
    return rows


def normalized(raw):
    from tmon.market import normalize_candle
    return [normalize_candle(r,'KRW')[1] for r in raw]


def stock(sym='000001',nxt=False):
    return {'symbol':sym,'name':'테스트보통주','market':'KOSPI','securityType':'STOCK','isCommonShare':True,
            'status':'ACTIVE','currency':'KRW','koreanMarketDetail':{'nxtSupported':nxt,
               'liquidationTrading':False,'krxTradingSuspended':False,
               'nxtTradingSuspended':False if nxt else None}}


def book(now=NOW):
    return {'timestamp':now.isoformat(),'currency':'KRW','asks':[{'price':'101.2','volume':'100000'}],
            'bids':[{'price':'101.1','volume':'100000'}]}


class FakeClient:
    def __init__(self):
        self.calls=[]
        self.price='101.2'
        self.warning=[]
        self.closed=False
        self.nxt=False
        self.stale=False
        self.missing=False
        self.ranking_fail=False

    def get(self,path,**params):
        self.calls.append((path,params))
        if path.endswith('/market-calendar/KR'):
            c=cal()
            if params.get('date') not in (None,NOW.date().isoformat()):
                d=datetime.fromisoformat(params['date']).replace(hour=10,tzinfo=KST)
                c=cal(d)
            if self.closed:c['today']['integrated']=None
            return c
        if path.endswith('/rankings'):
            if self.ranking_fail:raise TmonError('network-error','failed',4)
            return {'rankedAt':NOW.isoformat(),'rankings':[{'rank':1,'symbol':'000001','currency':'KRW',
                 'price':{'lastPrice':'101.2','basePrice':'100','changeRate':'0.012'},
                 'tradingVolume':'100000000','tradingAmount':'10000000000'}]}
        if path.endswith('/stocks'):return [stock(nxt=self.nxt)]
        if path.endswith('/warnings'):return self.warning
        if path.endswith('/prices'):
            return [{'symbol':'000001','currency':'KRW','lastPrice':self.price,
                     'timestamp':(NOW-timedelta(seconds=100) if self.stale else NOW).isoformat()}]
        if path.endswith('/orderbook'):
            b=book(); b['asks'][0]['price']=self.price
            if self.price=='99':b['bids'][0]['price']='98.9'
            return b
        if path.endswith('/price-limits'):
            return {'timestamp':NOW.isoformat(),'currency':'KRW','upperLimitPrice':'130'}
        if 'market-indicators' in path:raise TmonError('no-data','unavailable')
        if path.endswith('/candles'):
            data=days() if params['interval']=='1d' else minutes()
            if self.missing and params['interval']=='1m':data=data[:-1]
            return {'candles':list(reversed(data)),'nextBefore':None}
        raise AssertionError(path)


class ConfigTests(unittest.TestCase):
    def test_overrides_and_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'c.json';p.write_text('{"capital":"10","research":"off"}')
            self.assertEqual(load_config(p,capital='20')['capital'],'20')
            for contents in ('{"unknown":1}','{"capital":true}','{"capital":"NaN"}',
                             '{"configVersion":true}','{"universe":{"researchLimit":6}}',
                             '{"capital":1,"capital":2}','{"freshness":{"quoteSeconds":0}}'):
                p.write_text(contents)
                with self.assertRaises(TmonError):load_config(p)
    def test_cli_invalid_before_auth(self):
        with patch('tmon.recommend.Auth',side_effect=AssertionError('network')):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['recommend','--horizon','day','--capital','NaN','--json']),2)
            self.assertEqual(json.loads(out.getvalue())['error']['code'],'invalid-recommend-config')
    def test_endpoint_allowlist(self):
        self.assertEqual(endpoint_group('/api/v1/stocks/000001/warnings'),'STOCK')
        for p in ('/api/v1/orders','/api/v1/stocks/../orders/warnings','/api/v1/market-indicators/AAPL/candles'):
            self.assertIsNone(endpoint_group(p))
        transport=Transport()
        with patch.object(transport,'send',return_value=(200,{}, {'result':[]})) as send:
            transport.request('GET','/api/v1/stocks/000001/warnings',token='fake')
            transport.request('GET','/api/v1/market-indicators/KOSPI/candles',{'interval':'1m'},token='fake')
            self.assertEqual(send.call_count,2)
            with self.assertRaises(TmonError):transport.request('POST','/api/v1/orders',token='fake')
            self.assertEqual(send.call_count,2)


class DataTests(unittest.TestCase):
    def test_incomplete_bar_and_aggregation(self):
        raw={'candles':minutes()+[candle(NOW.replace(second=0))]}
        rows=completed_minutes(raw,NOW,START)
        self.assertEqual(len(rows),60)
        five=five_minutes(rows,START)
        self.assertEqual(len(five),12)
        self.assertEqual(five[-1]['volume'],D('1000000'))
        self.assertEqual(len(five_minutes(rows[:-1],START)),11)
    def test_duplicates_missing_and_stale(self):
        data=minutes(); data.append({**data[-1],'volume':'1'})
        with self.assertRaises(TmonError):completed_minutes({'candles':data},NOW,START)
        with self.assertRaises(TmonError):fresh((NOW-timedelta(seconds=16)).isoformat(),NOW,15)
        with self.assertRaises(TmonError):fresh((NOW+timedelta(seconds=6)).isoformat(),NOW,15)
    def test_sessions(self):
        self.assertIsNone(session(cal(),NOW,'day')[1])
        c=cal();c['today']['integrated']=None
        self.assertEqual(session(c,NOW,'day')[1],'market-closed')
        c=cal();c['today']['integrated']['regularMarket']['singlePriceAuctionStartTime']=None
        with self.assertRaises(TmonError):session(c,NOW,'day')
        self.assertEqual(session(cal(),NOW.replace(hour=15,minute=1),'day')[1],'outside-entry-window')
        self.assertIsNone(session(cal(),NOW.replace(hour=15,minute=1),'swing')[1])
    def test_book_and_venue(self):
        self.assertEqual(orderbook(book(),NOW,15)['bestAsk'],D('101.2'))
        b=book();b['bids'][0]['price']='102'
        with self.assertRaises(TmonError):orderbook(b,NOW,15)
        for nxt in (False,True):
            info=stock_info([stock(nxt=nxt)],'000001')
            self.assertEqual(info['venueScope'],'TOSS_PROVIDED')
            self.assertEqual(info['nxtSupported'],nxt)
        for exchange in ('KRX','NXT',None):
            with self.subTest(exchange=exchange), self.assertRaises(NoMatch):
                warnings_ok([{'warningType':'VI_STATIC','exchange':exchange}])

    def test_trading_suspension_with_provider_data(self):
        for field in ('krxTradingSuspended','nxtTradingSuspended','liquidationTrading'):
            s=stock(nxt=True);s['koreanMarketDetail'][field]=True
            with self.subTest(field=field), self.assertRaises(NoMatch):
                stock_info([s],'000001')
        # The provider's nullable NXT status is not a reason to exclude a stock.
        for nxt in (False,True):
            s=stock(nxt=nxt);s['koreanMarketDetail']['nxtTradingSuspended']=None
            self.assertEqual(stock_info([s],'000001')['nxtSupported'],nxt)
        s=stock(nxt=True);s['koreanMarketDetail']['nxtTradingSuspended']='false'
        with self.assertRaises(TmonError):stock_info([s],'000001')


class StrategyTests(unittest.TestCase):
    def sig(self,h='day'):
        cfg=load_config()
        rows=normalized(minutes())
        return signal(normalized(days()),five_minutes(rows,START),h,cfg[h],NOW)
    def test_day_calculation(self):
        s=self.sig();self.assertEqual(s['breakoutLevel'],D('101'))
        self.assertEqual(s['volumeRatio'],D('2'))
        self.assertEqual(s['invalidation'],D('99'))
        r=entry(s,orderbook(book(),NOW,15),D('101.2'),D('130'),'day',load_config())
        self.assertEqual(r['riskPerShare'],D('2.2'))
        self.assertEqual(r['targetRange'],[D('104.50'),D('105.6')])
        self.assertIsNone(r['quantity'])
    def test_swing_calculation(self):
        s=self.sig('swing'); self.assertEqual(s['breakoutLevel'],101)
        self.assertEqual(s['invalidation'],99)
    def test_loss_capital_and_depth(self):
        args=(self.sig(),orderbook(book(),NOW,15),D('101.2'),D('130'),'day')
        with self.assertRaises(NoMatch):entry(*args,load_config(max_loss_pct='1'))
        with self.assertRaises(NoMatch):entry(*args,load_config(capital='50'))
        r=entry(*args,load_config(capital='1012'))
        self.assertEqual(r['quantity'],10)
        self.assertEqual(r['estimatedPriceRiskKrw'],D('22'))
        with self.assertRaises(NoMatch):entry(*args,load_config(capital='100000000'))
    def test_invalidation_and_target_cap(self):
        args=(self.sig(),orderbook(book(),NOW,15))
        with self.assertRaises(NoMatch):entry(*args,D('100'),D('130'),'day',load_config())
        with self.assertRaises(NoMatch):entry(*args,D('101.2'),D('104'),'day',load_config())
        r=entry(*args,D('101.2'),D('105'),'day',load_config())
        self.assertEqual(r['targetRange'][-1],105)
    def test_zero_baseline_and_gap(self):
        cfg=load_config();d=normalized(days());m=normalized(minutes())
        for r in m:r['volume']=D(0)
        with self.assertRaises(TmonError):signal(d,five_minutes(m,START),'day',cfg['day'],NOW)
        m=normalized(minutes()); del m[35:40]
        with self.assertRaises(TmonError):signal(d,five_minutes(m,START),'day',cfg['day'],NOW)


class EngineTests(unittest.TestCase):
    def run_engine(self,client=None,researcher=research,horizon='day',config=None):
        with tempfile.TemporaryDirectory() as td:
            e=Engine(config or load_config(research='off'),horizon,3,client or FakeClient(),Store(Path(td).resolve()),now=lambda:NOW,researcher=researcher)
            r=envelope('recommend');code=e.run(r)
            p=Path(r['meta']['recordPath'])
            self.assertTrue(p.exists())
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            saved=json.loads(p.read_text());self.assertEqual(saved['status'],r['status'])
            return code,r,e
    def test_day_end_to_end(self):
        code,r,e=self.run_engine();self.assertEqual(code,0)
        self.assertEqual(len(r['data']),1);self.assertEqual(r['data'][0]['research']['researchStatus'],'disabled')
        self.assertTrue(any(x['stage']=='screen' for x in e.inputs['excluded']) is False)
        self.assertEqual(r['meta']['outcomeReason'],'recommended')
    def test_swing_end_to_end(self):
        code,r,_=self.run_engine(horizon='swing');self.assertEqual(code,0)
        self.assertEqual(r['data'][0]['holdingSessions'],[2,5])
        self.assertIsNotNone(r['data'][0]['exitBy'])
    def test_closed_does_not_research(self):
        c=FakeClient();c.closed=True
        def fail(*a,**k):raise AssertionError()
        code,r,e=self.run_engine(c,researcher=fail)
        self.assertEqual(code,0);self.assertEqual(r['data'],[])
        self.assertEqual(len(c.calls),1)
    def test_news_delay_price_invalidation(self):
        c=FakeClient()
        def change(*a,**k):
            c.price='99'
            return {'000001':unavailable()}, {'status':'unavailable'}
        code,r,_=self.run_engine(c,researcher=change)
        self.assertEqual(r['data'],[])
        self.assertIn('entry-invalidated',r['meta']['excludedCounts'])
    def test_news_delay_vi(self):
        for exchange in ('KRX','NXT'):
            with self.subTest(exchange=exchange):
                c=FakeClient();c.nxt=True
                def change(*a,**k):
                    c.warning=[{'warningType':'VI_DYNAMIC','exchange':exchange}]
                    return {'000001':unavailable()}, {'status':'unavailable'}
                _,r,e=self.run_engine(c,researcher=change)
                self.assertEqual(r['data'],[])
                self.assertTrue(any(x['stage']=='final' and x['reason']=='stock-warning-VI_DYNAMIC'
                                    for x in e.inputs['excluded']))
    def test_data_failure_vs_no_match(self):
        c=FakeClient();c.stale=True
        code,r,_=self.run_engine(c);self.assertEqual(code,5);self.assertEqual(r['error']['code'],'insufficient-data')
        c=FakeClient();c.price='99'
        code,r,_=self.run_engine(c);self.assertEqual(code,0);self.assertEqual(r['meta']['outcomeReason'],'no-match')
    def test_nxt_suspension_during_research(self):
        c=FakeClient();c.nxt=True
        original=c.get
        def change(*a,**k):
            def suspended(path,**params):
                value=original(path,**params)
                if path.endswith('/stocks'):
                    value[0]['koreanMarketDetail']['nxtTradingSuspended']=True
                return value
            c.get=suspended
            return {'000001':unavailable()}, {'status':'unavailable'}
        _,r,e=self.run_engine(c,researcher=change)
        self.assertEqual(r['data'],[])
        self.assertTrue(any(x['stage']=='final' and x['reason']=='trading-suspended'
                            for x in e.inputs['excluded']))
    def test_nxt_stocks_included_with_provider_basis(self):
        for horizon in ('day','swing'):
            with self.subTest(horizon=horizon):
                c=FakeClient();c.nxt=True
                code,r,_=self.run_engine(c,horizon=horizon)
                self.assertEqual(code,0);self.assertEqual(r['status'],'ok')
                self.assertEqual(len(r['data']),1)
                self.assertEqual(r['meta']['venuePolicy'],'TOSS_PROVIDED')
                row=r['data'][0]
                self.assertEqual(row['venueScope'],'TOSS_PROVIDED')
                self.assertEqual(row['venueBasis'],'provider-default')
                self.assertTrue(row['nxtSupported'])
                self.assertEqual(r['meta']['excludedCounts'],{})
                _,baseline,_=self.run_engine(horizon=horizon)
                for key in ('volumeRatio','breakoutLevel','entryReference'):
                    self.assertEqual(row[key],baseline['data'][0][key])
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    render_recommend(r['data'],r['meta'],lambda *args:None)
                self.assertIn('토스 제공 시세 기준',out.getvalue())
                self.assertNotIn('NXT 미지원',out.getvalue())
    def test_all_ranking_failure(self):
        c=FakeClient();c.ranking_fail=True
        code,r,_=self.run_engine(c);self.assertEqual(code,4)
    def test_news_unavailable_preserves_quantitative(self):
        def fail(*a,**k):return {'000001':unavailable()}, {'status':'unavailable'}
        code,r,_=self.run_engine(researcher=fail)
        self.assertEqual(code,0);self.assertEqual(r['status'],'partial');self.assertEqual(len(r['data']),1)

    def test_market_closes_during_research(self):
        c=FakeClient()
        def change(*a,**k):
            c.closed=True
            return {'000001':unavailable()}, {'status':'unavailable'}
        _,r,_=self.run_engine(c,researcher=change)
        self.assertEqual(r['data'],[])
        self.assertIn('session-ended',r['meta']['excludedCounts'])

    def test_missing_latest_minute_rejected(self):
        c=FakeClient();c.missing=True
        code,r,_=self.run_engine(c)
        self.assertEqual(code,5)
        self.assertEqual(r['data'],None)

    def test_record_failure_preserves_result(self):
        store=SimpleNamespace(save=lambda *a: (_ for _ in ()).throw(OSError('disk full')))
        e=Engine(load_config(research='off'),'day',3,FakeClient(),store,now=lambda:NOW)
        r=envelope('recommend');code=e.run(r)
        self.assertEqual(code,0);self.assertEqual(r['status'],'partial')
        self.assertEqual(len(r['data']),1);self.assertIsNone(r['meta']['recordPath'])

    def test_data_budget_yields_partial_not_no_match(self):
        c=FakeClient();clock=[0.0]
        original=c.get
        def delayed(path,**params):
            data=original(path,**params)
            if path.endswith('/rankings') and params['type']=='TOP_GAINERS':clock[0]=81.0
            return data
        c.get=delayed
        with tempfile.TemporaryDirectory() as td:
            e=Engine(load_config(research='off'),'day',3,c,Store(Path(td).resolve()),now=lambda:NOW,clock=lambda:clock[0])
            r=envelope('recommend');code=e.run(r)
            self.assertEqual(code,5);self.assertEqual(r['status'],'partial')
            self.assertEqual(r['meta']['outcomeReason'],'insufficient-data')
            self.assertEqual(r['meta']['notEvaluated'][0]['reason'],'not-evaluated-budget')


class ResearchTests(unittest.TestCase):
    def item(self):
        return {'symbol':'000001','researchStatus':'verified','summary':'확인된 공시',
                'catalysts':[{'text':'실적 공시','sourceIds':['a'],'eventAt':None}],
                'counterEvidence':[],'upcomingEvents':[],
                'sources':[{'id':'a','url':'https://example.com/news','title':'공시','publishedAt':None}]}
    def test_schema_evidence_and_unknown(self):
        item=self.item();self.assertEqual(validate([item],['000001'],NOW)['000001']['researchStatus'],'verified')
        item['entryPrice']='123'
        with self.assertRaises(ValueError):validate([item],['000001'],NOW)
        item=self.item();item['catalysts'][0]['sourceIds']=['missing']
        with self.assertRaises(ValueError):validate([item],['000001'],NOW)
        item=self.item();item['sources'][0]['publishedAt']=(NOW+timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):validate([item],['000001'],NOW)
    def test_secrets_not_in_child_environment(self):
        with patch.dict(os.environ,{'TOSS_CLIENT_SECRET':'hidden','OPENAI_API_KEY':'hidden','GITHUB_TOKEN':'hidden'}):
            e=safe_env()
            self.assertNotIn('TOSS_CLIENT_SECRET',e);self.assertNotIn('OPENAI_API_KEY',e);self.assertNotIn('GITHUB_TOKEN',e)
    def test_no_sdk_dependency_when_off(self):
        with patch('subprocess.Popen',side_effect=AssertionError('SDK invoked')):
            r,_=research([{'symbol':'000001'}],'day',load_config(research='off'),10)
            self.assertEqual(r['000001']['researchStatus'],'disabled')

    def test_timeout_kills_process_group_and_falls_back(self):
        p=SimpleNamespace(pid=999999,returncode=-9,communicate=lambda *a,**k: (_ for _ in ()).throw(subprocess.TimeoutExpired('worker',1)),wait=lambda:None)
        with patch('tmon.research.subprocess.Popen',return_value=p),patch('tmon.research.os.killpg') as kill:
            r,m=research([{'symbol':'000001','name':'test','market':'KOSPI'}],'day',load_config(),1,asof=NOW)
        self.assertEqual(r['000001']['reason'],'research-timeout');kill.assert_called_once()

    def test_malformed_source_time_falls_back(self):
        item=self.item();item['sources'][0]['publishedAt']='not-a-time'
        data=json.dumps({'items':[item],'webSearchCount':1,'model':'test'})
        p=SimpleNamespace(pid=999999,returncode=0,communicate=lambda *a,**k:(data,None),wait=lambda:None)
        with patch('tmon.research.subprocess.Popen',return_value=p),patch('tmon.research.os.killpg'):
            r,m=research([{'symbol':'000001','name':'test','market':'KOSPI'}],'day',load_config(),1,asof=NOW)
        self.assertEqual(r['000001']['researchStatus'],'unavailable')

    def test_cached_evidence_does_not_start_sdk(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td).resolve())
            import hashlib
            key=hashlib.sha256(json.dumps(['000001','day',None,'news-v1',NOW.date().isoformat()]).encode()).hexdigest()
            store.cache_write(key,{'item':self.item(),'model':'test','retrievedAt':NOW.isoformat()})
            with patch('subprocess.Popen',side_effect=AssertionError('cache missed')):
                r,m=research([{'symbol':'000001','name':'test','market':'KOSPI'}],'day',load_config(),1,store=store,asof=NOW)
            self.assertTrue(r['000001']['cacheHit'])

    def test_storage_symlink_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();(root/'link').symlink_to(root,target_is_directory=True)
            with self.assertRaises(OSError):Store(root/'link').cache_write('test',{})


if __name__=='__main__':unittest.main()
