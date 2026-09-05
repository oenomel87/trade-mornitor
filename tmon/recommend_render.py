"""Human output, with external text stripped of terminal control characters."""

def clean(value):
    return ''.join(c for c in str(value) if ord(c) >= 32 and ord(c) != 127)


def render_recommend(data, meta, table):
    reasons = {'market-closed':'휴장 또는 정규장 밖입니다.', 'outside-entry-window':'신규 진입 평가 시간 밖입니다.',
               'no-match':'현재 조건을 충족하는 후보가 없습니다.', 'insufficient-data':'판단에 필요한 데이터가 부족합니다.'}
    print(('당일매매' if meta['horizon'] == 'day' else '2~5거래일 매매') + ' · ' + meta['strategyVersion'])
    print('후보군 %d개 · 추천 %d개 · 토스 제공 시세 기준 (KRX·NXT 구분 없이 사용)' % (meta.get('universeCount',0),len(data)))
    if not data:
        print(reasons.get(meta.get('outcomeReason'),'추천 후보가 없습니다.'))
    else:
        table(['#','종목','이름','진입 기준','무효화','가격 위험 %','거래량 배수'],
              [[r['rank'],r['symbol'],clean(r['name']),r['entryReference'],r['invalidation'],r['riskPct'],r['volumeRatio']] for r in data])
    for r in data:
        print('\n%s %s' % (r['symbol'],clean(r['name'])))
        print('진입 범위: %s 초과 ~ %s 이하 · 목표: %s ~ %s (위험폭 배수 가정)' %
              (r['entryRange']['lowerExclusive'],r['entryRange']['upperInclusive'],*r['targetRange']))
        print('시세: %s · 신호: %s · 유효 기한: %s' % (r['quoteAsOf'],r['signalAsOf'],r['expiresAt']))
        print('점검일: %s · 보유 종료 기준: %s' % (r['reviewDate'] or 'N/A',r['exitBy'] or '5거래일 이내, 날짜 미확인'))
        if r['quantity'] is not None:
            print('가정 수량: %s주 · 가격상 손실 폭: %s원 (비용 제외)' % (r['quantity'],r['estimatedPriceRiskKrw']))
        for reason in r['quantitativeReasons']:
            print('근거: '+clean(reason))
        news = r['research']
        print('웹 조사: '+news['researchStatus']+(' (캐시)' if news.get('cacheHit') else ''))
        if news.get('summary'):
            print(clean(news['summary']))
        for key,label in (('catalysts','재료'),('counterEvidence','반대 근거'),('upcomingEvents','예정 일정')):
            for item in news.get(key,[]):
                print(label+': '+clean(item['text'])+' ['+', '.join(item['sourceIds'])+']')
        for s in news.get('sources',[]):
            print('출처 [%s] %s · %s · %s' % (clean(s['id']),clean(s['title']),s['publishedAt'] or '게시 시각 미확인',clean(s['url'])))
        for reason in r['limitations']:
            print('참고: '+clean(reason))
    if meta.get('excludedCounts'):
        print('제외: '+', '.join(clean(k)+' '+str(v) for k,v in meta['excludedCounts'].items()))
    if meta.get('recordPath'):
        print('실행 기록: '+meta['recordPath'])
