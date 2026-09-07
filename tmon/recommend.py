"""Snapshot recommendation orchestration with final revalidation and audit records."""
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, localcontext
import hashlib
from pathlib import Path
import subprocess
import time
import uuid

from .auth import Auth
from .client import TossClient, Transport
from .errors import TmonError, invalid_data, is_expected_ineligible
from .intraday import fresh, session, signal_window_end, utcnow
from .market import history, timestamp, market_zone
from .ranking import rank
from .recommend_config import load_config, config_hash
from .recommend_data import (RecordingClient, daily_data, minute_data, current_data,
                             ensure_frozen_index, frozen_relative_for_candidate,
                             relative_return_window, request_counts,
                             signal_input_hash, stock_eligibility, VENUE_SCOPE,
                             window_basis_fields, benchmark_snapshot_hash)
from .recommend_daily import (ADJUSTMENT_BASIS_UNVERIFIED, common_trading_dates,
                              validate_daily_dates, verify_adjustment_basis)
from .recommend_precheck import precheck_universe
from .recommend_store import Store
from .recommend_summary import summarize
from .research import research
from .strategies import VERSION, NoMatch, signal, entry, sort_key


SIGNAL_POLICY = 'frozen-current-state-v2'
RECOMMENDATION_POLICY_VERSION = 'snapshot-v2'
FILL_MODEL = 'best-ask-visible-depth-v1'
SIGNAL_PRICE_ADJUSTMENT_POLICY = 'adjusted=true'


def code_revision():
    """Identify the source actually used, including an uncommitted tree."""
    package = Path(__file__).resolve().parent
    try:
        source_hash = hashlib.sha256()
        for path in sorted(package.glob('*.py')):
            source_hash.update(path.name.encode())
            source_hash.update(path.read_bytes())
        digest = source_hash.hexdigest()[:20]
        repo = package.parent
        head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                       stderr=subprocess.DEVNULL, text=True).strip()
        return 'git:%s+source:%s' % (head, digest)
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        try:
            source_hash = hashlib.sha256()
            for path in sorted(package.glob('*.py')):
                source_hash.update(path.name.encode())
                source_hash.update(path.read_bytes())
            return 'source:%s' % source_hash.hexdigest()[:20]
        except OSError:
            return 'source-unverified'


class Engine:
    def __init__(self, config, horizon, limit, client=None, store=None, now=utcnow, clock=time.monotonic,
                 researcher=research):
        # Keep the caller's configuration and the audit snapshot independent
        # from researcher callbacks and downstream mutation.
        self.config, self.horizon, self.limit = deepcopy(config), horizon, limit
        self.now, self.clock, self.researcher = now, clock, researcher
        self.started = clock()
        self.deadline = self.started + self.config['budget']['totalSeconds']
        self.transport = None
        if client is None:
            self.transport = Transport(self.config['budget']['totalSeconds'], clock=clock, now=now)
            client = TossClient(Auth(self.transport), self.transport)
        inner = client.client if isinstance(client, RecordingClient) else client
        if isinstance(client, RecordingClient):
            self.client = client
            self.client.now = now
            self.client._clock = clock
        else:
            self.client = RecordingClient(inner, now, clock)
        self.store = store if store is not None else Store()
        self.inputs = {'requests':self.client.records, 'candidates':[], 'excluded':[],
                       'preExcluded':[], 'precheckBatches':[],
                       'notEvaluated':[], 'screened':[], 'dailyChecks': {},
                       'adjustmentChecks': []}
        self.partial = False
        self.phase_times = {}
        self.daily = {}
        self.index_cache = {}
        self.index_window = None
        self.screen_relative = {}
        self.warnings = []
        self.eligible = set()
        self.stock_raw = {}
        self.stock_checked_at = {}
        self.detail_selected_count = 0
        # Authoritative snapshots are never handed to the research callback.
        # The callback receives a separate deep copy of the display candidate.
        self.signal_snapshots = {}
        self.trading_date_reference = None
        self.final_verifications = {}

    def phase(self, end):
        self.phase_end = min(end, self.deadline)
        if self.transport is not None:
            self.transport.deadline = self.phase_end

    def remaining(self):
        return self.phase_end - self.clock()

    def check_budget(self):
        if self.remaining() <= 0:
            raise TmonError('recommend-budget', '추천 단계의 시간 예산을 초과했습니다.', 4)

    def _prepare_trading_dates(self, calendar, result):
        """Prepare the one shared completed-session reference for this run."""
        required = 20 if self.horizon == 'day' else 65
        reference = common_trading_dates(
            self.client, calendar, self.now(), required,
            check_budget=self.check_budget)
        self.trading_date_reference = deepcopy(reference)
        self.inputs['tradingDateReference'] = deepcopy(reference)
        result['meta']['tradingDateReference'] = deepcopy(reference)
        return reference

    def calendar(self, refresh=False):
        today = self.now().astimezone(market_zone('KRW')).date().isoformat()
        if refresh:
            self.client.calendar_cache.clear()
        return self.client.get('/api/v1/market-calendar/KR', date=today)

    def _recorded_at(self, path):
        """Read a logical request receipt without exposing response content."""
        try:
            for record in reversed(self.client.records):
                if record.get('path') == path and record.get('success'):
                    return timestamp(record['receivedAt'])
        except (TmonError, KeyError, TypeError, ValueError):
            pass
        return self.now()

    @staticmethod
    def _same_session(snapshot, sess):
        expected = snapshot.get('sessionIdentity') if isinstance(snapshot, dict) else None
        if not isinstance(expected, dict) or not isinstance(sess, dict):
            return False
        try:
            return all(expected.get(key) == sess[key].isoformat()
                       for key in ('start', 'end', 'auction', 'entryEnd'))
        except (KeyError, AttributeError):
            return False

    def reject(self, symbol, error, stage):
        is_match = isinstance(error, NoMatch)
        expected = is_expected_ineligible(error)
        details = getattr(error, 'details', {})
        if not isinstance(details, dict):
            details = {}
        kind = 'condition' if is_match else 'data'
        record = {'symbol': symbol, 'reason': error.code, 'stage': stage,
                  'kind': kind, 'expectedIneligible': expected,
                  'message': error.message, **deepcopy(details),
                  'details': deepcopy(details)}
        # Error details are evidence, never authority over classification.
        record.update(symbol=symbol, reason=error.code, stage=stage,
                      kind=kind, expectedIneligible=expected,
                      message=error.message)
        self.inputs['excluded'].append(record)
        if not is_match:
            self.partial = True
        if error.exit_code == 3:
            raise error

    def universe(self, result):
        specs = [('amount','1d'),('volume','1d'),('gain','1d')] if self.horizon == 'day' else [('amount','1d'),('gain','1w')]
        merged, succeeded = {}, 0
        previous_phase = self.client.phase
        self.client.phase = 'ranking'
        try:
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
        finally:
            self.client.phase = previous_phase
        if not succeeded:
            raise TmonError('rankings-unavailable','후보군 랭킹을 조회할 수 없습니다.',4)
        ordered = sorted(merged.values(),key=lambda x:(-x['preRank'],x['symbol']))
        self.inputs['candidates'] = ordered
        return ordered

    def _make_signal_snapshot(self, sym, daily, five, sess, sig):
        """Freeze signal inputs and values before any mutable downstream work."""
        signal_at = timestamp(sig['signalAsOf'])
        bar_end = (signal_at + timedelta(minutes=5)).isoformat() \
            if self.horizon == 'day' else None
        daily_tail = daily[-20:] if self.horizon == 'day' else daily[-65:]
        five_tail = five[-6:] if self.horizon == 'day' else []
        signal_hash = signal_input_hash(
            sym, self.horizon, daily, five, self.config[self.horizon],
            SIGNAL_PRICE_ADJUSTMENT_POLICY)
        snapshot = {
            'symbol': sym, 'horizon': self.horizon,
            'strategyVersion': self.config['strategyVersion'],
            'signalId': signal_hash, 'signalInputHash': signal_hash,
            'signalPolicy': self.config['signalPolicy'],
            'signalAsOf': sig['signalAsOf'],
            'signalBarStartAt': sig['signalAsOf'],
            'signalBarEndAt': bar_end,
            'signalSessionDate': sess.get('date'),
            'signalClose': (five[-1]['closePrice'] if self.horizon == 'day'
                            else daily[-1]['closePrice']),
            'priceAdjustmentPolicy': SIGNAL_PRICE_ADJUSTMENT_POLICY,
            'signalSettings': {k: self.config[self.horizon][k] for k in
                               ('minEstimatedAvgDailyAmount', 'minVolumeRatio')},
            'signalInput': {'daily': deepcopy(daily_tail),
                            'five': deepcopy(five_tail)},
            'signal': deepcopy(sig),
            'sessionIdentity': {key: sess[key].isoformat() for key in
                                ('start', 'end', 'auction', 'entryEnd')},
            # ``signalAsOf`` is the start of the signal bar.  The next bar
            # starts five minutes after its end and is complete after D.
            'nextBarCompletionAt': (signal_at + timedelta(minutes=10) + timedelta(
                seconds=self.config['freshness']['barCompletionDelaySeconds'])).isoformat()
                if self.horizon == 'day' else None,
            'signalValidUntil': min(
                signal_at + timedelta(minutes=5) + timedelta(
                    seconds=self.config['day']['maxSignalAgeSeconds'])
                if self.horizon == 'day' else sess['entryEnd'],
                sess['entryEnd']).isoformat(),
        }
        return snapshot

    def _row_from_snapshot(self, snapshot, stock, price, book, upper, sess,
                           observations, checked_at):
        """Recompute entry fields from a frozen signal and current observations."""
        sig = deepcopy(snapshot['signal'])
        row = {**stock, **entry(sig, book, price['lastPrice'], upper,
                                self.horizon, self.config),
               'horizon': self.horizon, 'strategyVersion': VERSION,
               'currency': 'KRW', 'quoteAsOf': price['timestamp'],
               'orderbookAsOf': book['timestamp'],
               # stateAsOf remains the legacy state-check meaning.
               'stateAsOf': observations['stateAsOf'].isoformat(),
               'stockCheckedAt': observations['stockCheckedAt'].isoformat(),
               'warningsCheckedAt': observations['warningsCheckedAt'].isoformat(),
               'quoteCheckedAt': observations['quoteCheckedAt'].isoformat(),
               'orderbookCheckedAt': observations['orderbookCheckedAt'].isoformat(),
               'limitsCheckedAt': observations['limitsCheckedAt'].isoformat(),
               'finalCheckedAt': checked_at.isoformat(),
               'universePolicy': self.config['universePolicy'],
               'reviewDate': None,
               'exitBy': sess['exitBy'].isoformat() if self.horizon == 'day' else None,
               'limitations': ['목표는 위험폭의 1.5~2배 가정이며 예상 수익률이 아닙니다.',
                              '평균 일거래대금은 종가×거래량 추정값입니다.']}
        for field in ('signalId', 'signalInputHash', 'signalPolicy',
                      'signalBarStartAt', 'signalBarEndAt', 'signalSessionDate',
                      'signalClose', 'priceAdjustmentPolicy', 'signalValidUntil',
                      'nextBarCompletionAt', 'sessionIdentity'):
            row[field] = deepcopy(snapshot.get(field))
        row['signalSnapshot'] = deepcopy(snapshot)
        row['relativeReturnPct'] = snapshot.get('relativeReturnPct')
        row['relativeReturnReason'] = snapshot.get('relativeReturnReason')
        row['relativeReturnWindow'] = deepcopy(snapshot.get('relativeReturnWindow'))
        row['benchmarkSnapshotHash'] = snapshot.get('benchmarkSnapshotHash')
        row['relativeReturnBenchmarkHash'] = snapshot.get('benchmarkSnapshotHash')
        if row['relativeReturnPct'] is None:
            row['limitations'].append('같은 구간의 시장 상대수익률을 확인하지 못했습니다.')
        if self.config['capital'] is None:
            row['limitations'].append('투입 금액 미설정: 주문 수량과 호가잔량 적합성은 평가하지 않았습니다.')
        valid_until = timestamp(snapshot['signalValidUntil'])
        # Equality is expiration: a day signal at age 600 is not usable.
        if self.horizon == 'day' and checked_at >= valid_until:
            raise NoMatch('signal-stale', '원 신호의 유효 시간이 지났습니다.')
        age = ((checked_at - timestamp(snapshot['signalBarEndAt'])).total_seconds()
               if snapshot.get('signalBarEndAt') else None)
        row['signalAgeSeconds'] = int(age) if age is not None and age.is_integer() else age
        row['signalPathCheck'] = 'not-performed'
        row['signalPathLimitation'] = '신호 발생 이후 가격 경로는 확인하지 않고 현재 밴드만 점검했습니다.'
        f = self.config['freshness']
        expiry = min(
            checked_at + timedelta(seconds=f['resultSeconds']),
            timestamp(price['timestamp']) + timedelta(seconds=f['quoteSeconds']),
            timestamp(book['timestamp']) + timedelta(seconds=f['quoteSeconds']),
            observations['stockCheckedAt'] + timedelta(seconds=f['stateSeconds']),
            observations['warningsCheckedAt'] + timedelta(seconds=f['stateSeconds']),
            valid_until, sess['entryEnd'])
        row['expiresAt'] = expiry.isoformat()
        if expiry <= checked_at:
            raise TmonError('expired-candidate', '재검증 전에 후보 시세가 만료되었습니다.')
        return row

    def _verify_raw_daily(self, symbol):
        """Verify the final candidate's unadjusted daily window once."""
        required = 20 if self.horizon == 'day' else 65
        check = {'symbol': symbol, 'horizon': self.horizon,
                 'requiredDailyBars': required, 'status': 'unavailable'}
        try:
            raw_rows, raw_meta, _, _ = history(
                self.client, symbol, required, adjusted=False, now=self.now(),
                check_budget=self.check_budget)
        except TmonError as error:
            if error.exit_code == 3:
                raise
            check.update({'reason': ADJUSTMENT_BASIS_UNVERIFIED,
                          'sourceError': error.code,
                          'sourceDetails': deepcopy(getattr(error, 'details', {}))})
            self.inputs['adjustmentChecks'].append(check)
            basis_error = TmonError(
                ADJUSTMENT_BASIS_UNVERIFIED,
                '비수정 일봉을 확인하지 못해 가격 조정 기준을 검증할 수 없습니다.')
            basis_error.details = deepcopy(check)
            raise basis_error from None
        try:
            verification = verify_adjustment_basis(
                self.daily[symbol], raw_rows, self.horizon,
                self.trading_date_reference,
                self.config[self.horizon]['minEstimatedAvgDailyAmount'])
        except NoMatch as error:
            details = deepcopy(getattr(error, 'details', {}))
            check.update({'status': 'condition', **details,
                          'rawMeta': deepcopy(raw_meta), 'reason': error.code,
                          'symbol': symbol})
            self.inputs['adjustmentChecks'].append(check)
            raise
        except TmonError as error:
            details = deepcopy(getattr(error, 'details', {}))
            check.update({**details, 'rawMeta': deepcopy(raw_meta),
                          'reason': error.code, 'symbol': symbol})
            self.inputs['adjustmentChecks'].append(check)
            raise
        check.update({'status': 'verified', 'reason': None,
                      **deepcopy(verification), 'rawMeta': deepcopy(raw_meta)})
        self.inputs['adjustmentChecks'].append(check)
        self.final_verifications[symbol] = deepcopy(verification)
        return verification

    def evaluate(self, sym, calendar, sess, final=False, phase_started_at=None,
                 signal_window_end_at=None, signal_snapshot=None):
        self.check_budget()
        if final:
            # Final evaluation intentionally has no chart or signal path. The
            # authoritative snapshot is selected independently of research's
            # mutable candidate copy.
            snapshot = signal_snapshot or self.signal_snapshots.get(sym)
            if snapshot is None:
                raise TmonError('missing-signal-snapshot', '원 신호 스냅샷이 없습니다.')
            preloaded = None
            preloaded_checked_at = None
            current_phase = self.client.phase
            try:
                self.client.phase = 'final'
                stock, price, book, upper, state_at, observations = current_data(
                    self.client, sym, self.config['freshness'], self.now,
                    preloaded_stock=preloaded,
                    preloaded_stock_checked_at=preloaded_checked_at,
                    return_observations=True)
            finally:
                self.client.phase = current_phase
            checked_at = self.now()
            return self._row_from_snapshot(snapshot, stock, price, book, upper,
                                           sess, observations, checked_at)

        # Screening is the only phase that reads calculation bars and invokes
        # signal().
        if sym not in self.daily:
            if sym not in self.eligible:
                raw = self.client.get('/api/v1/stocks', symbols=sym)
                self.stock_raw[sym] = deepcopy(raw)
                self.stock_checked_at[sym] = self._recorded_at('/api/v1/stocks')
                reference_dates = (self.trading_date_reference or {}).get('dates') \
                    if (self.trading_date_reference or {}).get('status') == 'verified' else None
                stock_eligibility(raw, sym, self.horizon, self.now(),
                                  trading_dates=reference_dates)
            try:
                self.daily[sym] = daily_data(
                    self.client, sym, calendar, self.now(),
                    horizon=self.horizon, reference=self.trading_date_reference,
                    check_budget=self.check_budget)
            except TmonError as error:
                self.inputs['dailyChecks'][sym] = {
                    'status': 'unavailable', 'verified': False,
                    'horizon': self.horizon,
                    'requiredDailyBars': 20 if self.horizon == 'day' else 65,
                    **deepcopy(getattr(error, 'details', {})),
                    'reason': error.code}
                raise
            validation = validate_daily_dates(
                self.daily[sym], self.trading_date_reference,
                20 if self.horizon == 'day' else 65)
            self.inputs['dailyChecks'][sym] = deepcopy(validation)
        daily = self.daily[sym]
        phase_instant = phase_started_at if phase_started_at is not None else self.now()
        mins, five = ([], [])
        if self.horizon == 'day':
            mins, five = minute_data(self.client, sym, sess, phase_instant,
                                     self.config['freshness'], self.warnings,
                                     phase_started_at=phase_instant,
                                     signal_window_end_at=signal_window_end_at)
        sig = signal(daily, five, self.horizon, self.config[self.horizon], phase_instant)
        snapshot = self._make_signal_snapshot(sym, daily, five, sess, sig)
        if self.horizon == 'swing':
            # A swing signal is the previous completed business-day candle,
            # while ``sess.date`` is the execution date.
            snapshot['signalSessionDate'] = calendar['previousBusinessDay']['date']
        preloaded = self.stock_raw.get(sym)
        stock, price, book, upper, state_at, observations = current_data(
            self.client, sym, self.config['freshness'], self.now,
            preloaded_stock=preloaded,
            preloaded_stock_checked_at=self.stock_checked_at.get(sym),
            return_observations=True)
        frozen_index = self._frozen_index_for(stock['market'])
        pct, reason, window = frozen_relative_for_candidate(
            self.horizon, daily, mins, self.index_window, frozen_index)
        benchmark_hash = benchmark_snapshot_hash(frozen_index)
        if isinstance(window, dict) and benchmark_hash:
            window = {**window, 'benchmarkSnapshotHash': benchmark_hash}
        snapshot.update(relativeReturnPct=pct, relativeReturnReason=reason,
                        relativeReturnWindow=deepcopy(window),
                        benchmarkSnapshotHash=benchmark_hash,
                        relativeReturnBenchmarkHash=benchmark_hash)
        # Keep the same frozen values in the compatibility cache and publish
        # an independent authoritative copy before research starts.
        self.screen_relative[sym] = {'pct': pct, 'reason': reason,
                                     'window': deepcopy(window)}
        self.signal_snapshots[sym] = deepcopy(snapshot)
        row = self._row_from_snapshot(snapshot, stock, price, book, upper,
                                      sess, observations, self.now())
        return row

    @staticmethod
    def _business_session_ok(day):
        """Coherent business-day session data: regularMarket with a valid
        same-date [start, end) range. Never inferred from weekday/index."""
        try:
            if not isinstance(day, dict) or not isinstance(day.get('date'), str):
                return False
            from datetime import date as _date
            _date.fromisoformat(day['date'])
            integrated = day.get('integrated')
            regular = integrated.get('regularMarket') if isinstance(integrated, dict) else None
            if not isinstance(regular, dict):
                return False
            start, end = timestamp(regular['startTime']), timestamp(regular['endTime'])
            if not (start < end and (end - start) <= timedelta(days=1)):
                return False
            if start.astimezone(market_zone('KRW')).date().isoformat() != day['date']:
                return False
            return True
        except (KeyError, TypeError, AttributeError):
            return False

    @staticmethod
    def _session_ended_before(day, now):
        """Signal date really completed relative to the execution calendar:
        its regular session already ended as of now."""
        try:
            return timestamp(day['integrated']['regularMarket']['endTime']) <= now
        except (KeyError, TypeError, AttributeError):
            return False

    def _swing_trading_dates(self, calendar):
        """Return the last six dates from the initial common reference.

        The full 65-date chain is prepared once before ranking. Swing index
        comparison and signal data must share its final six dates; when that
        basis is unavailable there is no weaker weekday or index fallback.
        ``calendar`` remains an accepted argument for older direct callers.
        """
        reference = self.trading_date_reference
        if not isinstance(reference, dict) or reference.get('status') != 'verified' or \
                reference.get('verified') is not True:
            return None
        dates = reference.get('dates')
        if not isinstance(dates, list) or len(dates) != 65:
            return None
        return list(dates[-6:])

    def _frozen_comparison_window(self, calendar, sess, phase_started_at,
                                  signal_window_end_at):
        if self.horizon == 'day':
            if signal_window_end_at is None:
                return None
            start = signal_window_end_at - timedelta(minutes=30)
            return {'interval': '1m', 'comparisonStartAt': start.isoformat(),
                    'comparisonEndAt': signal_window_end_at.isoformat(),
                    **window_basis_fields('day')}
        dates = self._swing_trading_dates(calendar)
        if dates is None:
            return None
        return {'interval': '1d', 'comparisonStartAt': dates[0],
                'comparisonEndAt': dates[-1], **window_basis_fields('swing'),
                'tradingDates': dates}

    def _ensure_index_snapshots(self, sess, phase_started_at):
        """Fetch per-market frozen index snapshots BEFORE candidates (PR2)."""
        if self.index_window is None:
            return
        seen = set()
        for sym in self.eligible:
            raw = self.stock_raw.get(sym)
            try:
                market = raw[0].get('market') if isinstance(raw, list) and raw else None
            except (IndexError, AttributeError):
                market = None
            if market in ('KOSPI', 'KOSDAQ'):
                seen.add(market)
        markets = sorted(seen)
        delay = self.config['freshness']['barCompletionDelaySeconds']
        for market in markets:
            self.check_budget()
            previous = self.client.phase
            self.client.phase = 'screen'
            try:
                if self.horizon == 'day':
                    cstart = timestamp(self.index_window['comparisonStartAt'])
                    cend = timestamp(self.index_window['comparisonEndAt'])
                else:
                    cstart = self.index_window['comparisonStartAt']
                    cend = self.index_window['comparisonEndAt']
                ensure_frozen_index(
                    self.client, market, self.horizon, cstart, cend,
                    session_start=sess['start'] if self.horizon == 'day' else None,
                    phase_started_at=phase_started_at, delay=delay,
                    now=self.now(),
                    budget_remaining=lambda: self.remaining() > 0,
                    cache=self.index_cache,
                    trading_dates=(self.trading_date_reference.get('dates', [])[-6:]
                                   if self.horizon == 'swing' and
                                   isinstance(self.trading_date_reference, dict) and
                                   self.trading_date_reference.get('status') == 'verified'
                                   else None))
            finally:
                self.client.phase = previous

    def _frozen_index_for(self, market):
        for result in self.index_cache.values():
            if isinstance(result, dict) and result.get('market') == market and \
                    result.get('comparisonStartAt') == (self.index_window or {}).get('comparisonStartAt') and \
                    result.get('comparisonEndAt') == (self.index_window or {}).get('comparisonEndAt'):
                return result
        return None

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
        self.warnings = result['warnings']
        meta = result['meta']
        meta.update(runId=uuid.uuid4().hex, recommendSchemaVersion=1, horizon=self.horizon,
                    strategyVersion=self.config['strategyVersion'], effectiveConfig=deepcopy(self.config),
                    configHash=config_hash(self.config),
                    startedAt=self.now().isoformat(), recordPath=None,
                    venuePolicy=VENUE_SCOPE, outcomeReason=None,
                    codeRevision=code_revision(),
                    recommendationPolicyVersion=self.config['recommendationPolicyVersion'],
                    signalPolicy=self.config['signalPolicy'], fillModel=self.config['fillModel'],
                    relativeReturnWindowPolicy=self.config['relativeReturnWindowPolicy'],
                    relativeReturnBasis=window_basis_fields(self.horizon),
                    relativeReturnComparisonPolicy=self.config['relativeReturnWindowPolicy'],
                    universePolicy=self.config['universePolicy'],
                    preExcluded=[], preExcludedCount=0,
                    precheckDataUnavailable=0, detailSelectedCount=0,
                    precheckBatches=[], tradingDateReference=None,
                    dailyChecks={}, adjustmentChecks=[])
        result['data'] = []
        code = 0
        self.phase(self.started + self.config['budget']['dataSeconds'])
        try:
            code = self._run(result)
        except TmonError as e:
            if 'quantitativePassCount' not in result['meta']:
                result['meta']['quantitativePassCount'] = len(self.inputs['screened'])
            result.update(status='error',data=None,error=e.as_dict())
            code = e.exit_code
        except KeyboardInterrupt:
            if 'quantitativePassCount' not in result['meta']:
                result['meta']['quantitativePassCount'] = len(self.inputs['screened'])
            result.update(status='error',data=None,error=TmonError('interrupted','사용자가 중단했습니다.',130).as_dict())
            code = 130
        counts = request_counts(self.client.records)
        meta.update(completedAt=self.now().isoformat(), elapsedSeconds=round(self.clock()-self.started,3),
                    timings=self.phase_times, universeCount=len(self.inputs['candidates']),
                    excluded=self.inputs['excluded'], notEvaluated=self.inputs['notEvaluated'],
                    preExcluded=self.inputs['preExcluded'],
                    preExcludedCount=self.preexcluded_condition_count(),
                    precheckDataUnavailable=self.precheck_data_count(),
                    detailSelectedCount=self.detail_selected_count,
                    precheckBatches=self.inputs['precheckBatches'],
                    tradingDateReference=deepcopy(self.inputs.get('tradingDateReference')),
                    dailyChecks=deepcopy(self.inputs.get('dailyChecks', {})),
                    adjustmentChecks=deepcopy(self.inputs.get('adjustmentChecks', [])),
                    excludedCounts=dict(Counter(r['reason'] for r in self.inputs['excluded'])),
                    screenedCount=len(self.inputs['screened']),
                    endpointCounts=counts['byEndpoint'],
                    endpointGroupCounts=counts['byGroup'],
                    requestPhaseCounts=counts['byPhase'],
                    actualAttemptsByEndpoint=counts['actualAttemptsByEndpoint'],
                    actualAttemptsByGroup=counts['actualAttemptsByGroup'],
                    expectedIneligibleCount=self.expected_count(),
                    candidateDataUnavailableCount=self.unavailable_count())
        meta['executionStatus'] = 'failed' if result.get('status') == 'error' else 'completed'
        meta['coverageStatus'] = self.coverage_status(result, meta)
        # Keep the established diagnostic summary on fatal/auth/interrupt
        # results too; persistence and CLI error paths must expose the same
        # evidence as successful runs.
        if result.get('status') == 'error':
            summarize(result)
        # Persistence and serialization are part of the visible expiry
        # boundary. A delayed store may cross that boundary after the first
        # write, so trim and overwrite this run once when necessary.
        save_attempts = 0
        while save_attempts < 2:
            if result.get('status') != 'error':
                self.finalize_output(result, result.get('data') or [])
                if self.partial and code == 0:
                    code = 5
                self.refresh_meta_counts(result)
                summarize(result)
            try:
                self.store.save(result,self.inputs)
            except OSError:
                meta['recordPath'] = None
                result['warnings'].append({'code':'record-write-failed','message':'실행 기록을 저장하지 못했습니다.'})
                if result['status'] == 'ok':
                    result['status'] = 'partial'
                if result.get('status') != 'error':
                    self.refresh_meta_counts(result)
                    summarize(result)
                break
            save_attempts += 1
            saved_checked_at = self.now()
            if all(timestamp(row['expiresAt']) > saved_checked_at
                   for row in (result.get('data') or [])):
                break
        # If a pathological second write still crossed an expiry boundary,
        # persist a safe empty result so the file cannot disagree with the
        # object returned to the CLI. This is the final bounded fallback.
        output_rows = result.get('data') or []
        output_checked_at = self.now()
        if (result.get('status') != 'error' and output_rows and
                min(timestamp(row['expiresAt']) for row in output_rows) <= output_checked_at):
            for row in output_rows:
                self.reject(row['symbol'], TmonError(
                    'expired-candidate', '표시 전에 후보 시세가 만료되었습니다.'), 'output')
            result['data'] = []
            self.partial = True
            result['status'] = 'partial'
            if code == 0:
                code = 5
            result['meta']['outcomeReason'] = 'insufficient-data'
            self.refresh_meta_counts(result)
            summarize(result)
            try:
                self.store.save(result, self.inputs)
            except OSError:
                meta['recordPath'] = None
                result['warnings'].append({'code':'record-write-failed','message':'실행 기록을 저장하지 못했습니다.'})
                self.refresh_meta_counts(result)
                summarize(result)
        return code

    def expected_count(self):
        rows = list(self.inputs['excluded']) + list(self.inputs['preExcluded'])
        return sum(1 for r in rows if r.get('expectedIneligible'))

    def refresh_meta_counts(self, result):
        """Refresh aggregates after output-boundary exclusion or retry."""
        counts = request_counts(self.client.records)
        meta = result['meta']
        meta.update(excluded=self.inputs['excluded'],
                    notEvaluated=self.inputs['notEvaluated'],
                    preExcluded=self.inputs['preExcluded'],
                    preExcludedCount=self.preexcluded_condition_count(),
                    precheckDataUnavailable=self.precheck_data_count(),
                    detailSelectedCount=self.detail_selected_count,
                    precheckBatches=self.inputs['precheckBatches'],
                    tradingDateReference=deepcopy(self.inputs.get('tradingDateReference')),
                    dailyChecks=deepcopy(self.inputs.get('dailyChecks', {})),
                    adjustmentChecks=deepcopy(self.inputs.get('adjustmentChecks', [])),
                    excludedCounts=dict(Counter(r['reason'] for r in self.inputs['excluded'])),
                    screenedCount=len(self.inputs['screened']),
                    endpointCounts=counts['byEndpoint'], endpointGroupCounts=counts['byGroup'],
                    requestPhaseCounts=counts['byPhase'],
                    actualAttemptsByEndpoint=counts['actualAttemptsByEndpoint'],
                    actualAttemptsByGroup=counts['actualAttemptsByGroup'],
                    expectedIneligibleCount=self.expected_count(),
                    candidateDataUnavailableCount=self.unavailable_count())
        meta['executionStatus'] = 'failed' if result.get('status') == 'error' else 'completed'
        meta['coverageStatus'] = self.coverage_status(result, meta)

    def unavailable_count(self):
        # Distinct candidate symbols with required-data failures. Repeated
        # failure events for one symbol (e.g. expiry-retry then output
        # rejection) count once here; meta.excludedCounts keeps event counts.
        rows = list(self.inputs['excluded']) + list(self.inputs['preExcluded'])
        return len({r['symbol'] for r in rows if r.get('kind') == 'data'})

    def preexcluded_condition_count(self):
        """Count confirmed precheck conditions, excluding data failures."""
        return sum(1 for row in self.inputs['preExcluded']
                   if row.get('kind') == 'condition')

    def precheck_data_count(self):
        """Count precheck data failures without folding in later exclusions."""
        return sum(1 for row in self.inputs['preExcluded']
                   if row.get('kind') == 'data')

    def finalize_output(self, result, final_rows):
        """Apply the last expiry boundary and assign deterministic ranks."""
        valid = []
        checked_at = self.now()
        for row in final_rows:
            if timestamp(row['expiresAt']) > checked_at:
                valid.append(row)
            else:
                self.reject(row['symbol'], TmonError(
                    'expired-candidate', '표시 전에 시세가 만료되었습니다.'), 'output')
        valid.sort(key=sort_key)
        result['data'] = [dict(row, rank=i + 1)
                          for i, row in enumerate(valid[:self.limit])]
        previous_reason = result['meta'].get('outcomeReason')
        if result['data'] or previous_reason not in ('market-closed', 'outside-entry-window'):
            result['meta']['outcomeReason'] = (
                'recommended' if result['data'] else
                'insufficient-data' if self.partial else 'no-match')
        if self.partial:
            result['status'] = 'partial'
        return valid

    def coverage_status(self, result, meta):
        reason = meta.get('outcomeReason')
        if reason in ('market-closed', 'outside-entry-window'):
            return 'not-evaluated'
        screen_excluded = [r for r in self.inputs['excluded']
                           if r.get('stage') in ('eligibility', 'screen')]
        evaluated = (len(screen_excluded) + len(self.inputs['preExcluded']) +
                     int(meta.get('quantitativePassCount') or 0))
        if result.get('status') == 'error' and evaluated == 0:
            return 'not-evaluated'
        # self.partial tracks data-phase incompleteness only (ranking gaps,
        # data exclusions, budgets). Research-only unavailability flips result
        # status without touching self.partial, so quantitative coverage stays
        # complete in that case.
        if self.partial or meta.get('notEvaluated'):
            return 'partial'
        return 'complete'

    def _run(self, result):
        self.client.phase = 'calendar'
        cal = self.calendar()
        sess, reason = session(cal,self.now(),self.horizon)
        result['meta']['session'] = {k:v.isoformat() if hasattr(v,'isoformat') else v for k,v in (sess or {}).items()}
        if reason:
            result['meta']['outcomeReason'] = reason
            return 0
        # Build the exact completed trading-day basis before ranking and
        # precheck. The calendar requests must not age stockCheckedAt or be
        # silently rediscovered per candidate.
        reference = self._prepare_trading_dates(cal, result)
        selected = self.universe(result)
        # PR4-A: precheck the complete ranked union in bounded provider
        # batches.  detailLimit is applied only to rows that pass this phase;
        # conditions and unavailable data remain separate from detailed-screen
        # exclusions and are never counted twice.
        self.client.phase = 'prefetch'
        prechecked = precheck_universe(
            self.client, selected, self.horizon,
            self.config['universe']['detailLimit'], now=self.now,
            budget_check=self.check_budget,
            trading_dates=reference.get('dates') if reference.get('verified') else None)
        self.inputs['preExcluded'].extend(prechecked['preExcluded'])
        self.inputs['preExcluded'].extend(prechecked['dataUnavailable'])
        self.inputs['precheckBatches'] = deepcopy(prechecked['batches'])
        self.inputs['notEvaluated'].extend(prechecked['notEvaluated'])
        self.stock_raw.update(deepcopy(prechecked['stockRaw']))
        for sym, received_at in prechecked['receivedAtBySymbol'].items():
            try:
                self.stock_checked_at[sym] = timestamp(received_at)
            except (TmonError, TypeError, ValueError):
                # A malformed observation cannot safely be used as a state
                # timestamp; current_data will retain its normal fallback.
                continue
        # Keep all successful raw rows for audit/reuse, but only selected rows
        # drive chart/index work.  A valid candidate beyond detailLimit must
        # not cause an unnecessary market-index fetch.
        self.eligible.update(row['symbol'] for row in prechecked['selectedRows'])
        if (prechecked['dataUnavailable'] or
                any(row.get('reason') == 'not-evaluated-budget'
                    for row in prechecked['notEvaluated'])):
            self.partial = True
        detailed_targets = [row['symbol'] for row in prechecked['selectedRows']]
        self.detail_selected_count = len(detailed_targets)
        # PR1: one actual phaseStartedAt after detailed selection, shared by
        # the whole screening phase. F = S + n*300, n=floor((P-S-D)/300).
        # Stored for both day and swing (swing: signalWindowEndAt None).
        phase_started_at = self.now()
        signal_window_end_at = None
        screen_n = None
        if self.horizon == 'day':
            delay = self.config['freshness']['barCompletionDelaySeconds']
            signal_window_end_at = signal_window_end(sess['start'], phase_started_at, delay)
            if signal_window_end_at is None:
                screen_n = 0
            else:
                screen_n = int((signal_window_end_at - sess['start']).total_seconds() // 300)
            result['meta']['phaseStartedAt'] = phase_started_at.isoformat()
            result['meta']['signalWindowEndAt'] = signal_window_end_at.isoformat() if signal_window_end_at else None
            result['meta']['barCompletionDelaySeconds'] = delay
        else:
            result['meta']['phaseStartedAt'] = phase_started_at.isoformat()
            result['meta']['signalWindowEndAt'] = None
        # PR2: one frozen comparison window + per-market completed index
        # snapshots fetched BEFORE candidates (max one bounded retry, cached
        # success/unavailable alike under the full key).
        self.client.phase = 'screen'
        self.index_window = self._frozen_comparison_window(
            cal, sess, phase_started_at, signal_window_end_at)
        result['meta']['comparisonWindow'] = deepcopy(self.index_window) if self.index_window else None
        # Keep the policy identifier's type identical in config and result.
        # The detailed PR2 basis is additive under its own explicit field.
        result['meta']['relativeReturnWindowPolicy'] = self.config['relativeReturnWindowPolicy']
        result['meta']['relativeReturnBasis'] = window_basis_fields(self.horizon)
        result['meta']['relativeReturnComparisonPolicy'] = self.config['relativeReturnWindowPolicy']
        if self.remaining() <= 0:
            # A successful precheck can itself consume the remaining data
            # budget. Preserve its evidence, but do not start index/chart
            # work after the deadline; the selected rows become budget skips.
            self.partial = True
            self.inputs['notEvaluated'].extend(
                {'symbol': row['symbol'], 'reason': 'not-evaluated-budget'}
                for row in prechecked['selectedRows'])
            detailed_targets = []
        elif not (self.horizon == 'day' and screen_n is not None and screen_n < 6):
            self._ensure_index_snapshots(sess, phase_started_at)
        candidates = []
        if self.horizon == 'day' and screen_n is not None and screen_n < 6:
            # Expected session-not-ready: session exists but the required 6
            # completed 5-minute bars cannot exist yet. Expected evaluation
            # unavailability with evidence (not a normal condition failure
            # nor a data defect).
            for sym in detailed_targets:
                err = NoMatch('session-not-ready', '장 시작 후 완료 5분봉 6개가 아직 생성될 수 없습니다.')
                err.details = {'sessionStart': sess['start'].isoformat(),
                               'phaseStartedAt': phase_started_at.isoformat(),
                               'requiredCompletedBars': 6, 'completedBars': screen_n}
                self.reject(sym, err, 'screen')
        else:
            for sym in detailed_targets:
                try:
                    self.client.phase = 'screen'
                    row = self.evaluate(sym,cal,sess, phase_started_at=phase_started_at,
                                        signal_window_end_at=signal_window_end_at)
                    candidates.append(row)
                    # PR0: freeze each initial quantitative pass immediately, so an
                    # auth failure or interrupt on a later candidate preserves the
                    # earlier passes. Deep copy: later research mutations or final
                    # price changes cannot alter the original snapshot.
                    self.inputs['screened'].append(deepcopy(row))
                except TmonError as e:
                    self.reject(sym,e,'screen')
        result['meta']['quantitativePassCount'] = len(candidates)
        # PR2 coverage: relative-return availability by reason over screened rows.
        try:
            from collections import Counter as _Counter
            _reasons = _Counter(r.get('relativeReturnReason') or 'ok' for r in candidates)
            result['meta']['relativeReturnCoverage'] = {
                'ok': int(_reasons.get('ok', 0)),
                'unavailable': int(len(candidates) - _reasons.get('ok', 0)),
                'byReason': dict(_reasons)}
        except Exception:
            pass
        self.client.phase = 'calendar'
        review_date, exit_by = self.holding_dates(cal) if candidates else (None,None)
        self.phase_times['dataSeconds'] = round(self.clock()-self.started,3)
        if not candidates:
            if self.partial:
                data_errors = [r for r in self.inputs['excluded'] if r['kind']=='data']
                precheck_data = [r for r in self.inputs['preExcluded']
                                 if r.get('kind') == 'data']
                if ((precheck_data and not detailed_targets) or
                        (detailed_targets and
                         len(data_errors) >= len(detailed_targets))):
                    raise TmonError('insufficient-data','필수 데이터를 확인할 수 있는 후보가 없습니다.')
                result['status'] = 'partial'
            result['meta']['outcomeReason'] = 'insufficient-data' if self.partial else 'no-match'
            return 5 if self.partial else 0
        candidates.sort(key=sort_key)
        shortlist = candidates[:self.config['universe']['researchLimit']]
        research_start = self.clock()
        budget = self.config['budget']
        available = max(0,min(budget['researchSeconds'], self.deadline-budget['finalSeconds']-self.clock()))
        # Research is allowed to annotate its own mutable copy. In
        # particular, nested signalSnapshot/signalInput values must not be
        # able to change the authoritative snapshot used below.
        research_input = deepcopy(shortlist)
        investigated, research_meta = self.researcher(research_input,self.horizon,deepcopy(self.config),available,
                                                       store=self.store,asof=self.now())
        result['meta']['research'] = research_meta
        self.phase_times['researchSeconds'] = round(self.clock()-research_start,3)
        self.phase(self.deadline)
        final = []
        for row in shortlist:
            sym = row['symbol']
            if self.remaining() <= 0:
                self.partial = True
                self.inputs['notEvaluated'].append({'symbol':sym,'reason':'final-budget'})
                continue
            try:
                self.client.phase = 'final'
                latest_cal = self.calendar(refresh=True)
                latest_session, reason = session(latest_cal,self.now(),self.horizon)
                snapshot = self.signal_snapshots.get(sym)
                if reason:
                    result['meta']['outcomeReason'] = reason
                if (reason or latest_cal['today']['date'] != cal['today']['date'] or
                        snapshot is None or
                        (self.horizon == 'day' and
                         snapshot.get('signalSessionDate') != latest_session.get('date')) or
                        (snapshot is not None and
                         not self._same_session(snapshot, latest_session))):
                    raise NoMatch('session-ended')
                if (self.horizon == 'swing' and
                        latest_cal.get('previousBusinessDay', {}).get('date') !=
                        snapshot.get('signalSessionDate')):
                    raise NoMatch('session-ended')
                if self.horizon == 'day' and self.now() >= timestamp(snapshot['signalValidUntil']):
                    raise NoMatch('signal-stale', '원 신호의 유효 시간이 지났습니다.')
                # Recheck the original adjusted signal window against the
                # provider's unadjusted window immediately before quote
                # validation. The original shortlist is fixed; failures do
                # not refill it with later screened rows.
                adjustment = self._verify_raw_daily(sym)
                verified = self.evaluate(sym,latest_cal,latest_session,final=True,
                                         signal_snapshot=snapshot)
                verified['research'] = investigated[sym]
                verified['finalVerification'] = {
                    'status': 'verified',
                    'basis': 'observed-equal-adjusted-unadjusted-window',
                    'horizon': self.horizon,
                    'requiredDailyBars': adjustment['requiredDailyBars'],
                    'rawEstimatedAvgDailyAmount': adjustment['rawEstimatedAvgDailyAmount'],
                    'minimumEstimatedAvgDailyAmount': adjustment['minimumEstimatedAvgDailyAmount'],
                    'explanation': adjustment['explanation'],
                }
                verified['rawEstimatedAvgDailyAmount'] = adjustment['rawEstimatedAvgDailyAmount']
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
                    self.client.phase = 'expiry-retry'
                    retry_cal = self.calendar(refresh=True)
                    latest, reason = session(retry_cal,self.now(),self.horizon)
                    if reason:
                        result['meta']['outcomeReason'] = reason
                    if (reason or retry_cal.get('today', {}).get('date') !=
                            cal.get('today', {}).get('date')):
                        raise NoMatch('session-ended')
                    snapshot = self.signal_snapshots.get(row['symbol'])
                    if snapshot is None:
                        raise TmonError('missing-signal-snapshot', '원 신호 스냅샷이 없습니다.')
                    if not self._same_session(snapshot, latest):
                        raise NoMatch('session-ended')
                    if (self.horizon == 'swing' and
                            retry_cal.get('previousBusinessDay', {}).get('date') !=
                            snapshot.get('signalSessionDate')):
                        raise NoMatch('session-ended')
                    fresh_row = self.evaluate(row['symbol'],retry_cal,latest,final=True,
                                              signal_snapshot=snapshot)
                    fresh_row.update(research=row['research'],reviewDate=row['reviewDate'],exitBy=row['exitBy'])
                    verification = self.final_verifications.get(row['symbol'])
                    if verification is not None:
                        fresh_row['finalVerification'] = {
                            'status': 'verified',
                            'basis': 'observed-equal-adjusted-unadjusted-window',
                            'horizon': self.horizon,
                            'requiredDailyBars': verification['requiredDailyBars'],
                            'rawEstimatedAvgDailyAmount': verification['rawEstimatedAvgDailyAmount'],
                            'minimumEstimatedAvgDailyAmount': verification['minimumEstimatedAvgDailyAmount'],
                            'explanation': verification['explanation'],
                        }
                        fresh_row['rawEstimatedAvgDailyAmount'] = verification['rawEstimatedAvgDailyAmount']
                    final[i] = fresh_row
                except TmonError as e:
                    self.reject(row['symbol'],e,'expiry-retry')
        valid = []
        output_checked_at = self.now()
        for r in final:
            if timestamp(r['expiresAt']) > output_checked_at:
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


def run_recommend(args,result, *, engine_holder=None):
    config = load_config(args.config,capital=args.capital,max_loss_pct=args.max_loss_pct,
                         research=args.research,limit=args.limit)
    if args.market != 'KR':
        raise TmonError('unsupported-market','추천은 KR만 지원합니다.',2)
    with localcontext() as ctx:
        ctx.prec = 50
        engine = Engine(config,args.horizon,args.limit)
        code = engine.run(result)
        # Keep the runtime object outside the JSON result.  The CLI passes a
        # short-lived holder for its pre-emit expiry check; direct callers do
        # not leave a process-global engine registry behind.
        if engine_holder is not None:
            engine_holder['engine'] = engine
        return code


def finalize_cli_result(result, engine=None, *, force_empty=False):
    """Recheck recommendation rows after CLI rendering and before emission."""
    if engine is None or result.get('status') == 'error':
        return False, 0
    rows = result.get('data') or []
    checked_at = engine.now()
    if not force_empty and (not rows or
                            min(timestamp(row['expiresAt']) for row in rows) > checked_at):
        return False, 0
    if force_empty:
        for row in rows:
            engine.reject(row['symbol'], TmonError(
                'expired-candidate', '표시 전에 후보 시세가 만료되었습니다.'), 'output')
        result['data'] = []
        engine.partial = True
        result['status'] = 'partial'
        result['meta']['outcomeReason'] = 'insufficient-data'
    else:
        engine.finalize_output(result, rows)
    engine.refresh_meta_counts(result)
    summarize(result)
    code = 5 if engine.partial else 0
    try:
        engine.store.save(result, engine.inputs)
    except OSError:
        result['meta']['recordPath'] = None
        result['warnings'].append({'code':'record-write-failed','message':'실행 기록을 저장하지 못했습니다.'})
        if result.get('status') == 'ok':
            result['status'] = 'partial'
        engine.refresh_meta_counts(result)
        summarize(result)
        code = max(code, 5)
    return True, code


def discard_cli_engine(engine_holder):
    if engine_holder is not None:
        engine_holder.clear()
