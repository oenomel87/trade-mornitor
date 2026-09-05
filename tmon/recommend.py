"""Snapshot recommendation orchestration with final revalidation and audit records."""
from collections import Counter
from datetime import timedelta
from decimal import Decimal, localcontext
import time
import uuid

from .auth import Auth
from .client import TossClient, Transport
from .errors import TmonError, invalid_data
from .intraday import fresh, session, utcnow
from .market import timestamp, market_zone
from .ranking import rank
from .recommend_config import load_config, config_hash
from .recommend_data import (RecordingClient, daily_data, minute_data, current_data,
                             relative_return, stock_info, VENUE_SCOPE)
from .recommend_store import Store
from .research import research
from .strategies import VERSION, NoMatch, signal, entry, sort_key


class Engine:
    def __init__(self, config, horizon, limit, client=None, store=None, now=utcnow, clock=time.monotonic,
                 researcher=research):
        self.config, self.horizon, self.limit = config, horizon, limit
        self.now, self.clock, self.researcher = now, clock, researcher
        self.started = clock()
        self.deadline = self.started + config['budget']['totalSeconds']
        self.transport = None
        if client is None:
            self.transport = Transport(config['budget']['totalSeconds'])
            client = TossClient(Auth(self.transport), self.transport)
        self.client = RecordingClient(client, now)
        self.store = store if store is not None else Store()
        self.inputs = {'requests':self.client.records, 'candidates':[], 'excluded':[], 'notEvaluated':[]}
        self.partial = False
        self.phase_times = {}
        self.daily = {}
        self.index_cache = {}

    def phase(self, end):
        self.phase_end = min(end, self.deadline)
        if self.transport is not None:
            self.transport.deadline = self.phase_end

    def remaining(self):
        return self.phase_end - self.clock()

    def check_budget(self):
        if self.remaining() <= 0:
            raise TmonError('recommend-budget', '추천 단계의 시간 예산을 초과했습니다.', 4)

    def calendar(self, refresh=False):
        today = self.now().astimezone(market_zone('KRW')).date().isoformat()
        if refresh:
            self.client.calendar_cache.clear()
        return self.client.get('/api/v1/market-calendar/KR', date=today)

    def reject(self, symbol, error, stage):
        is_match = isinstance(error, NoMatch)
        self.inputs['excluded'].append({'symbol':symbol,'reason':error.code,'stage':stage,
                                       'kind':'condition' if is_match else 'data'})
        if not is_match:
            self.partial = True
        if error.exit_code == 3:
            raise error

    def universe(self, result):
        specs = [('amount','1d'),('volume','1d'),('gain','1d')] if self.horizon == 'day' else [('amount','1d'),('gain','1w')]
        merged, succeeded = {}, 0
        for metric, duration in specs:
            self.check_budget()
            try:
                rows, meta, warnings, _ = rank(self.client, 'KR', metric, duration, 'market', self.config['universe']['rankCount'], True)
                if rows:
                    fresh(meta['rankedAt'],self.now(),self.config['freshness']['rankingSeconds'])
                succeeded += 1
                result['warnings'].extend(warnings)
                for row in rows:
                    sym = row['symbol']
                    item = merged.setdefault(sym, {'symbol':sym,'ranks':[], 'preRank':Decimal(0)})
                    item['ranks'].append({'by':metric,'duration':duration,'rank':row['rank'], 'asOf':meta['rankedAt']})
                    item['preRank'] += Decimal(1) / (60 + row['rank'])
            except TmonError as e:
                if e.exit_code == 3:
                    raise
                self.partial = True
                result['warnings'].append({'code':'ranking-unavailable','message':metric+'/'+duration+': '+e.code})
        if not succeeded:
            raise TmonError('rankings-unavailable','후보군 랭킹을 조회할 수 없습니다.',4)
        ordered = sorted(merged.values(),key=lambda x:(-x['preRank'],x['symbol']))
        self.inputs['candidates'] = ordered
        cap = self.config['universe']['detailLimit']
        self.inputs['notEvaluated'].extend({'symbol':r['symbol'],'reason':'detail-limit'} for r in ordered[cap:])
        return ordered[:cap]

    def evaluate(self, sym, calendar, sess, final=False):
        self.check_budget()
        if sym not in self.daily:
            # Check stock type/trading state before spending chart requests.
            stock_info(self.client.get('/api/v1/stocks',symbols=sym),sym)
            self.daily[sym] = daily_data(self.client,sym,calendar,self.now())
        daily = self.daily[sym]
        mins, five = ([], [])
        if self.horizon == 'day':
            mins, five = minute_data(self.client,sym,sess,self.now(),self.config['freshness'])
        sig = signal(daily,five,self.horizon,self.config[self.horizon],self.now())
        stock, price, book, upper, state_at = current_data(self.client,sym,self.config['freshness'],self.now)
        row = {**stock, **entry(sig,book,price['lastPrice'],upper,self.horizon,self.config),
               'horizon':self.horizon,'strategyVersion':VERSION,'currency':'KRW',
               'quoteAsOf':price['timestamp'],'orderbookAsOf':book['timestamp'],'stateAsOf':state_at.isoformat(),
               'reviewDate':None, 'exitBy':sess['exitBy'].isoformat() if self.horizon == 'day' else None,
               'limitations':['목표는 위험폭의 1.5~2배 가정이며 예상 수익률이 아닙니다.',
                              '평균 일거래대금은 종가×거래량 추정값입니다.']}
        if not final:
            row['relativeReturnPct'] = relative_return(self.client,stock['market'],self.horizon,daily,mins,self.now(),self.index_cache)
        # Final pass keeps no stale index comparison: refetch index data per market in a new cache.
        else:
            row['relativeReturnPct'] = relative_return(self.client,stock['market'],self.horizon,daily,mins,self.now(),self.final_index_cache)
        if row['relativeReturnPct'] is None:
            row['limitations'].append('같은 구간의 시장 상대수익률을 확인하지 못했습니다.')
        if self.config['capital'] is None:
            row['limitations'].append('투입 금액 미설정: 주문 수량과 호가잔량 적합성은 평가하지 않았습니다.')
        now = self.now()
        f = self.config['freshness']
        expiry = min(now + timedelta(seconds=f['resultSeconds']),
                     timestamp(price['timestamp']) + timedelta(seconds=f['quoteSeconds']),
                     timestamp(book['timestamp']) + timedelta(seconds=f['quoteSeconds']),
                     state_at + timedelta(seconds=f['stateSeconds']),sess['entryEnd'])
        row['expiresAt'] = expiry.isoformat()
        if expiry <= now:
            raise TmonError('expired-candidate','재검증 전에 후보 시세가 만료되었습니다.')
        return row

    def holding_dates(self, calendar):
        """Resolve future trading days without counting weekends as sessions."""
        if self.horizon == 'day':
            return None, None
        dates = [calendar['today']]
        try:
            current = calendar
            for _ in range(4):
                self.check_budget()
                nxt = current['nextBusinessDay']
                if nxt['date'] <= dates[-1]['date']:
                    raise invalid_data()
                dates.append(nxt)
                if len(dates) < 5:
                    current = self.client.get('/api/v1/market-calendar/KR',date=nxt['date'])
            end = dates[-1]['integrated']['regularMarket']['singlePriceAuctionStartTime']
            timestamp(end)
            return dates[1]['date'], end
        except (TmonError, KeyError, TypeError):
            return None, None

    def run(self, result):
        meta = result['meta']
        meta.update(runId=uuid.uuid4().hex, recommendSchemaVersion=1, horizon=self.horizon,
                    strategyVersion=VERSION, effectiveConfig=self.config, configHash=config_hash(self.config),
                    startedAt=self.now().isoformat(), recordPath=None,
                    venuePolicy=VENUE_SCOPE, outcomeReason=None)
        result['data'] = []
        code = 0
        self.phase(self.started + self.config['budget']['dataSeconds'])
        try:
            code = self._run(result)
        except TmonError as e:
            result.update(status='error',data=None,error=e.as_dict())
            code = e.exit_code
        except KeyboardInterrupt:
            result.update(status='error',data=None,error=TmonError('interrupted','사용자가 중단했습니다.',130).as_dict())
            code = 130
        meta.update(completedAt=self.now().isoformat(), elapsedSeconds=round(self.clock()-self.started,3),
                    timings=self.phase_times, universeCount=len(self.inputs['candidates']),
                    excluded=self.inputs['excluded'], notEvaluated=self.inputs['notEvaluated'],
                    excludedCounts=dict(Counter(r['reason'] for r in self.inputs['excluded'])))
        try:
            self.store.save(result,self.inputs)
        except OSError:
            meta['recordPath'] = None
            result['warnings'].append({'code':'record-write-failed','message':'실행 기록을 저장하지 못했습니다.'})
            if result['status'] == 'ok':
                result['status'] = 'partial'
        return code

    def _run(self, result):
        cal = self.calendar()
        sess, reason = session(cal,self.now(),self.horizon)
        result['meta']['session'] = {k:v.isoformat() if hasattr(v,'isoformat') else v for k,v in (sess or {}).items()}
        if reason:
            result['meta']['outcomeReason'] = reason
            return 0
        selected = self.universe(result)
        candidates = []
        for i, item in enumerate(selected):
            sym = item['symbol']
            if self.remaining() <= 0:
                self.partial = True
                self.inputs['notEvaluated'].extend({'symbol':x['symbol'],'reason':'not-evaluated-budget'} for x in selected[i:])
                break
            try:
                candidates.append(self.evaluate(sym,cal,sess))
            except TmonError as e:
                self.reject(sym,e,'screen')
        review_date, exit_by = self.holding_dates(cal) if candidates else (None,None)
        self.phase_times['dataSeconds'] = round(self.clock()-self.started,3)
        if not candidates:
            if self.partial:
                data_errors = [r for r in self.inputs['excluded'] if r['kind']=='data']
                if selected and len(data_errors) == len(selected):
                    raise TmonError('insufficient-data','필수 데이터를 확인할 수 있는 후보가 없습니다.')
                result['status'] = 'partial'
            result['meta']['outcomeReason'] = 'insufficient-data' if self.partial else 'no-match'
            return 5 if self.partial else 0
        candidates.sort(key=sort_key)
        shortlist = candidates[:self.config['universe']['researchLimit']]
        result['meta']['quantitativePassCount'] = len(candidates)
        research_start = self.clock()
        budget = self.config['budget']
        available = max(0,min(budget['researchSeconds'], self.deadline-budget['finalSeconds']-self.clock()))
        investigated, research_meta = self.researcher(shortlist,self.horizon,self.config,available,
                                                       store=self.store,asof=self.now())
        result['meta']['research'] = research_meta
        self.phase_times['researchSeconds'] = round(self.clock()-research_start,3)
        self.phase(self.deadline)
        self.final_index_cache = {}
        final = []
        for row in shortlist:
            sym = row['symbol']
            if self.remaining() <= 0:
                self.partial = True
                self.inputs['notEvaluated'].append({'symbol':sym,'reason':'final-budget'})
                continue
            try:
                latest_cal = self.calendar(refresh=True)
                latest_session, reason = session(latest_cal,self.now(),self.horizon)
                if reason or latest_cal['today']['date'] != cal['today']['date']:
                    raise NoMatch('session-ended')
                verified = self.evaluate(sym,latest_cal,latest_session,final=True)
                verified['research'] = investigated[sym]
                if self.horizon == 'swing':
                    verified.update(reviewDate=review_date,exitBy=exit_by)
                    if exit_by is None:
                        verified['limitations'].append('향후 5거래일의 정확한 종료 일정을 확인하지 못했습니다.')
                final.append(verified)
            except TmonError as e:
                self.reject(sym,e,'final')
        # Older candidates can expire while subsequent candidates are fetched. Retry each once.
        for i, row in enumerate(final):
            if timestamp(row['expiresAt']) <= self.now():
                try:
                    self.check_budget()
                    latest, reason = session(self.calendar(refresh=True),self.now(),self.horizon)
                    if reason:
                        raise NoMatch('session-ended')
                    fresh_row = self.evaluate(row['symbol'],cal,latest,final=True)
                    fresh_row.update(research=row['research'],reviewDate=row['reviewDate'],exitBy=row['exitBy'])
                    final[i] = fresh_row
                except TmonError as e:
                    self.reject(row['symbol'],e,'expiry-retry')
        valid = []
        for r in final:
            if timestamp(r['expiresAt']) > self.now():
                valid.append(r)
            else:
                self.reject(r['symbol'],TmonError('expired-candidate','표시 전에 시세가 만료되었습니다.'),'output')
        valid.sort(key=sort_key)
        result['data'] = [dict(row,rank=i+1) for i,row in enumerate(valid[:self.limit])]
        result['meta']['outcomeReason'] = 'recommended' if result['data'] else 'insufficient-data' if self.partial else 'no-match'
        self.phase_times['finalSeconds'] = round(self.clock()-research_start-self.phase_times['researchSeconds'],3)
        if research_meta.get('status') == 'unavailable':
            result['warnings'].append({'code':'research-unavailable','message':'웹 조사 불가: 정량 후보만 제공합니다.'})
            result['status'] = 'partial'
        if self.partial:
            result['status'] = 'partial'
        return 5 if self.partial else 0


def run_recommend(args,result):
    config = load_config(args.config,capital=args.capital,max_loss_pct=args.max_loss_pct,
                         research=args.research,limit=args.limit)
    if args.market != 'KR':
        raise TmonError('unsupported-market','추천은 KR만 지원합니다.',2)
    with localcontext() as ctx:
        ctx.prec = 50
        return Engine(config,args.horizon,args.limit).run(result)
