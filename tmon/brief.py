"""One-shot market briefing. Scheduling and delivery stay outside the CLI."""
from contextlib import contextmanager
import time
import uuid

from .auth import Auth
from .client import TossClient, Transport
from .errors import TmonError
from .intraday import utcnow
from .market import timestamp, market_zone
from .watchlist import Watchlist
from .recommend_data import RecordingClient
from .brief_data import calendar_context, collect, kst
from .brief_research import research, empty, canonical_url
from .brief_store import BriefStore, scope_key


def classify_news(news, previous, asof):
    """Known URLs do not prove a story is unchanged; label only what is measurable."""
    try:
        baseline = timestamp(previous['asOf'])
        urls = previous['urls']
        if baseline > asof or not isinstance(urls, list) or len(urls) > 500 or any(not isinstance(u, str) for u in urls):
            raise ValueError()
        seen = set(urls)
    except (TypeError, KeyError, ValueError, TmonError):
        baseline, seen = None, set()
    sources = {s['id']: s for s in news['sources']}
    for item in news['news']:
        linked = [sources[s] for s in item['sourceIds']]
        if baseline is None:
            state = 'first-brief'
        elif all(canonical_url(s['url']) in seen for s in linked):
            state = 'previously-covered'
        elif any(s['publishedAt'] and timestamp(s['publishedAt']) > baseline for s in linked):
            state = 'published-since-previous'
        elif any(s['publishedAt'] is None for s in linked):
            state = 'undated-source'
        else:
            state = 'newly-found-source'
        item['updateStatus'] = state
    current_urls = [canonical_url(s['url']) for s in news['sources']]
    return (kst(baseline.isoformat()) if baseline else None,
            list(dict.fromkeys(current_urls + sorted(seen)))[:500])


@contextmanager
def storage_lock(store, scope, warnings):
    # A read-only filesystem must not prevent an otherwise usable briefing.
    manager = store.locked(scope)
    try:
        manager.__enter__()
    except OSError:
        warnings.append({'code': 'brief-storage-unavailable', 'message': '기록 저장소에 접근할 수 없어 중복 실행 방지·기록 저장이 제한됩니다.'})
        yield
    else:
        try:
            yield
        finally:
            manager.__exit__(None, None, None)


def run_brief(args, result, *, client=None, store=None, watchlist=None, researcher=research, now=utcnow, clock=time.monotonic):
    if not 30 <= args.timeout <= 600:
        raise TmonError('invalid-brief-timeout', '--timeout은 30~600초여야 합니다.', 2)
    if not 1 <= args.profile_limit <= 20:
        raise TmonError('invalid-profile-limit', '--profile-limit는 1~20이어야 합니다.', 2)
    started, instant = clock(), now()
    asof = instant.astimezone(market_zone('KRW'))
    rows, profile = [], None
    if args.profile is not None:
        rows, profile_meta, _, _ = (watchlist or Watchlist()).apply('list', profile=args.profile)
        profile = profile_meta['profile']
    selected = [r['symbol'] for r in rows[:args.profile_limit]]
    warnings = result['warnings']
    if len(rows) > len(selected):
        warnings.append({'code': 'brief-profile-limit', 'message': '관심종목 %d개 중 저장 순서의 앞 %d개를 조회·조사합니다.' % (len(rows), len(selected))})
    store = store if store is not None else BriefStore()
    scope = scope_key(profile, selected)
    result['meta'].update(queriedAt=asof.isoformat(), source='Toss Securities Open API + Codex web search',
        timezone='Asia/Seoul', runId=uuid.uuid4().hex, briefSchemaVersion=1, profile=profile,
        profileCount=len(rows), selectedSymbols=selected, omittedSymbols=[r['symbol'] for r in rows[args.profile_limit:]],
        totalBudgetSeconds=args.timeout, model=None)
    with storage_lock(store, scope, warnings):
        return generate(args, result, client, store, researcher, now, clock, started, asof, selected, scope)


def generate(args, result, client, store, researcher, now, clock, started, asof, selected, scope):
    warnings = result['warnings']
    initial_warnings = list(warnings)
    supplied_client = client is not None
    meta = result['meta']
    data = {'indices': [], 'investorTrading': [], 'rankings': [], 'watchlist': []}
    context = {'phase': 'unknown', 'session': args.session if args.session != 'auto' else 'unknown',
               'date': asof.date().isoformat(), 'referenceDate': None, 'previousBusinessDay': None,
               'nextBusinessDay': None, 'regularStart': None, 'regularEnd': None}
    identities = [{'symbol': s, 'name': None, 'market': None} for s in selected]
    recording = None
    try:
        if client is None:
            transport = Transport(min(60, max(1, args.timeout - 15)))
            client = TossClient(Auth(transport), transport)
        recording = RecordingClient(client, now)
        raw = recording.get('/api/v1/market-calendar/KR', date=asof.date().isoformat())
        context = calendar_context(raw, asof, args.session)
        data, identities = collect(recording, context, asof, selected, warnings)
    except TmonError as e:
        warnings.append({'code': e.code, 'message': '시장 데이터: ' + e.message})
    meta.update(context=context, dataSeconds=round(clock() - started, 3))
    payload = {'asOf': asof.isoformat(), 'session': context['session'], 'context': context,
               'lookbackHours': 72, 'watchlist': identities}
    if args.research == 'off':
        narrative, research_meta = empty('research-off', 'disabled'), {'cacheHit': False}
    else:
        narrative, research_meta = researcher(payload, max(0, args.timeout - (clock() - started) - 25), store, refresh=args.refresh)
    # News research can take minutes. Refresh the independently collected market
    # data within a reserved final budget rather than labeling initial data live.
    meta['marketRefreshed'] = False
    if clock() - started - meta['dataSeconds'] > 30:
        try:
            remaining = args.timeout - (clock() - started) - 2
            if remaining <= 0:
                raise TmonError('brief-refresh-timeout', '최종 시세 갱신 예산이 부족합니다.', 4)
            if supplied_client:
                final_client = client
            else:
                transport = Transport(min(20, remaining))
                final_client = TossClient(Auth(transport), transport)
            final = RecordingClient(final_client, now)
            final_now = now().astimezone(market_zone('KRW'))
            fresh_cal = final.get('/api/v1/market-calendar/KR', date=final_now.date().isoformat())
            final_context = calendar_context(fresh_cal, final_now, args.session)
            final_warnings = []
            final_data, _ = collect(final, final_context, final_now, selected, final_warnings)
            # A failed refresh must not erase the last successfully observed data.
            if not final_data['indices'] and not final_data['investorTrading'] and not any(g['rows'] for g in final_data['rankings']):
                raise TmonError('brief-refresh-unavailable', '최종 시장 자료를 확보하지 못했습니다.', 5)
            data, context = final_data, final_context
            warnings[:] = initial_warnings + final_warnings
            meta.update(context=context, marketRefreshed=True)
        except TmonError as e:
            warnings.append({'code': e.code, 'message': '최종 갱신: ' + e.message})
        finally:
            if 'final' in locals():
                if recording is None:
                    recording = final
                else:
                    recording.records.extend(final.records)
    # Recheck ages at output, including cached and failed-news runs.
    from .brief_data import freshness
    for row in data['indices'] + data['investorTrading'] + data['rankings'] + data['watchlist']:
        if row.get('currency') == 'USD':
            continue
        try:
            row['freshness'] = freshness(row.get('asOf', row.get('timestamp')), context, now())
            if row.get('date') and row['date'] != context['referenceDate']:
                row['freshness'] = 'stale'
        except TmonError:
            row['freshness'] = 'unknown'
        if row['freshness'] in ('unknown', 'stale') and not any(w['code'] == 'brief-stale-data' for w in warnings):
            warnings.append({'code': 'brief-stale-data', 'message': '출력 시점에 오래되거나 시각을 확인할 수 없는 시장 자료가 있습니다.'})
    market_failed = bool([w for w in warnings if w['code'] not in ('brief-profile-limit', 'fewer-ranking-results', 'brief-storage-unavailable')])
    if args.session != 'auto' and args.session != context['phase']:
        warnings.append({'code': 'brief-session-mismatch', 'message': '요청한 브리핑 유형과 실제 장 상태가 다릅니다. 자료 기준은 실제 장 상태를 따릅니다.'})
    if narrative['status'] == 'unavailable':
        warnings.append({'code': narrative['reason'], 'message': '웹 조사를 완료하지 못했습니다. 확인된 시장 데이터만 표시합니다.'})
    previous = store.cache_read('previous-' + scope)
    previous_asof, urls = classify_news(narrative, previous, asof)
    meta.update(research=research_meta, model=research_meta.get('model'), previousBriefAsOf=previous_asof,
                completedAt=kst(now().isoformat()), elapsedSeconds=round(clock() - started, 3))
    result['data'] = {'market': data, 'research': narrative}
    result['status'] = 'partial' if market_failed or narrative['status'] == 'unavailable' else 'ok'
    inputs = {'requests': recording.records if recording else [], 'researchInput': payload}
    saved = False
    try:
        store.save(result, inputs)
        saved = True
    except OSError:
        warnings.append({'code': 'record-write-failed', 'message': '브리핑 결과는 유지했지만 실행 기록을 저장하지 못했습니다.'})
    # Disabled, failed and cached runs must not consume the news comparison watermark.
    if saved and narrative['status'] == 'completed' and not research_meta.get('cacheHit'):
        try:
            store.cache_write('previous-' + scope, {'asOf': asof.isoformat(), 'urls': urls})
        except OSError:
            warnings.append({'code': 'brief-history-write-failed', 'message': '다음 브리핑의 비교 기준을 저장하지 못했습니다.'})
    useful = bool(data['indices'] or data['investorTrading'] or data['watchlist'] or any(g['rows'] for g in data['rankings']) or narrative['news'])
    return 5 if market_failed or not useful else 0
