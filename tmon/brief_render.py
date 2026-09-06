"""Korean text output for both terminal use and notification wrappers."""
from decimal import Decimal
from .recommend_render import clean

SESSIONS = {'premarket': '장전', 'intraday': '장중', 'close': '장마감', 'closed': '휴장일', 'unknown': '장 상태 미확인'}
FRESHNESS = {'current-session': '장중 자료', 'dated-snapshot': '기준 시점 자료', 'stale': '오래된 자료', 'unknown': '시각 미확인'}
UPDATES = {'first-brief': '첫 브리핑', 'previously-covered': '기존 출처', 'published-since-previous': '이전 브리핑 이후 게시',
           'undated-source': '게시 시각 미확인', 'newly-found-source': '추가 확인한 출처'}


def number(value, places=2, scale=1, signed=False):
    if value is None:
        return 'N/A'
    value = Decimal(value) / Decimal(scale)
    return format(value, ('+' if signed else '') + ',.' + str(places) + 'f')


def render_brief(data, meta, table):
    context = meta['context']
    print('국내 시황 브리핑 · ' + SESSIONS[context['session']] + ' · KST ' + meta['queriedAt'])
    print('실제 장 상태: %s · 시세 기준 거래일: %s · 다음 거래일: %s' %
          (SESSIONS[context['phase']], context['referenceDate'] or '미확인', context['nextBusinessDay'] or '미확인'))
    market, research = data['market'], data['research']
    print('\n핵심 요약')
    available = [r for r in market['indices'] if r['changePct'] is not None]
    if available:
        print('- ' + ' · '.join('%s %s%%' % (r['symbol'], number(r['changePct'], signed=True)) for r in available) + ' (각 시세 시점의 전 거래일 대비; 기준 시각은 아래 표)')
    for item in research['summary'][:2 if available else 3]:
        print('- ' + clean(item['text']) + ' [' + ', '.join(item['sourceIds']) + ']')
    if not available and not research['summary']:
        print('요약할 수 있는 자료가 부족합니다.')
    if market['indices']:
        print('\n국내 지수 · 단위 point · 전 거래일 종가 대비 % · 분봉 사용 시 시각은 봉 시작 기준')
        table(['지수', '조회값', '등락 %', '비교 종가일', '시세 시각 KST', '가격 기준', '자료 상태'],
              [[r['symbol'], number(r['lastPrice']), number(r['changePct'], signed=True), r['baseDate'], r['asOf'],
                '완료 1분봉 종가' if r['priceBasis'] == 'completed-minute-close' else '제공 현재가', FRESHNESS[r['freshness']]] for r in market['indices']])
    if market['investorTrading']:
        print('\nKRX 거래대금·순매수 · 억원 · 당일 기록은 잠정치')
        table(['시장', '거래대금', '외국인 순매수', '기관 순매수', '집계일', '갱신 시각 KST', '자료 상태'],
              [[r['symbol'], number(r['turnoverKrw'], scale=100000000), number(r['netBuyingKrw']['foreigner'], scale=100000000, signed=True), number(r['netBuyingKrw']['institution'], scale=100000000, signed=True),
                r['date'], r['asOf'], FRESHNESS[r['freshness']]] for r in market['investorTrading']])
    for group in market['rankings']:
        label = {'amount': '거래대금', 'gain': '상승률', 'loss': '하락률'}[group['by']]
        print('\n%s 상위 · 토스 제공 집계 · %s · %s' % (label, group['asOf'] or '시각 미확인', FRESHNESS[group['freshness']]))
        print('등락률 기준: ' + ('전일 대비' if group['changeBasis'] == 'previous-close' else '선택 기간 시작 대비 (1일)'))
        table(['종목', '이름', '현재가 원', '등락 %', '거래대금 억원'],
              [[r['symbol'], clean(r['name'] or ''), number(r['lastPrice'], 0), number(r['changePct'], signed=True), number(r['tradingAmount'], scale=100000000)] for r in group['rows']])
    if meta['profile'] is not None:
        print('\n관심종목 프로필: ' + clean(meta['profile']) + ' · %d/%d개' % (len(meta['selectedSymbols']), meta['profileCount']))
        table(['종목', '이름', '현재가', '통화', '시세 시각 KST', '자료 상태'],
              [[r['symbol'], clean(r['name'] or ''), number(r['lastPrice'], 0 if r['currency'] == 'KRW' else 2), r['currency'], r['timestamp'], FRESHNESS[r['freshness']]] for r in market['watchlist']])
    status = {'completed': '완료', 'no-relevant-source': '관련 자료 없음', 'disabled': '사용 안 함', 'unavailable': '조사 불가'}[research['status']]
    print('\n주요 뉴스 · ' + status + (' · 캐시 사용' if meta['research'].get('cacheHit') else ''))
    if meta['research'].get('asOf'):
        print('뉴스 조사 기준 KST: ' + meta['research']['asOf'])
    if meta['previousBriefAsOf']:
        print('이전 조사 기준 KST: ' + meta['previousBriefAsOf'])
    for i, item in enumerate(research['news'], 1):
        print('%d. %s [%s]' % (i, clean(item['headline']), UPDATES[item['updateStatus']]))
        print('   사실: ' + clean(item['summary']) + ' [' + ', '.join(item['sourceIds']) + ']')
        if item['impact']:
            print('   영향 해석: ' + clean(item['impact']))
        if item['eventAt']:
            print('   사건 시각 KST: ' + item['eventAt'])
    print('\n다음 확인 사항')
    for event in research['upcomingEvents']:
        print('- %s · %s [%s]' % (event['eventAt'] or '정확한 시각 미확인', clean(event['text']), ', '.join(event['sourceIds'])))
    if not research['upcomingEvents']:
        print('출처로 확인한 예정 일정이 없습니다.')
    for source in research['sources']:
        print('[%s] %s · 게시 KST %s\n  %s' % (source['id'], clean(source['title']), source['publishedAt'] or '미확인', clean(source['url'])))
    if meta.get('recordPath'):
        print('\n실행 기록: ' + meta['recordPath'])
