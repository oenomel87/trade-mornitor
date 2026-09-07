"""Explain screening coverage separately from later verification outcomes."""
from collections import Counter


LABELS = {
    'not-common-stock': '대상 제외(보통주 아님)',
    'no-volume-breakout': '가격 돌파 또는 거래량 조건 미충족',
    'low-liquidity': '유동성 기준 미충족',
    'entry-invalidated': '진입 가격 조건 미충족',
    'insufficient-history': '일봉 이력 부족',
    'insufficient-minute-bars': '완료 분봉 부족',
    'incomplete-bars': '분봉 누락 또는 최신 완료 봉 부족',
    'stale-data': '데이터 최신성 기준 미충족',
    'future-minute-bar': '허용 범위를 넘는 미래 분봉',
    'detail-limit': '상세평가 한도',
    'not-evaluated-budget': '평가 시간 예산 초과',
    'final-budget': '최종 검증 시간 예산 초과',
}


def grouped(rows):
    counts = Counter(r['reason'] for r in rows)
    return [{'reason': code, 'label': LABELS.get(code, code), 'count': count}
            for code, count in counts.items()]


def summarize(result):
    meta = result['meta']
    screen = [r for r in meta['excluded'] if r['stage'] == 'screen']
    conditions = [r for r in screen if r['kind'] == 'condition']
    errors = [r for r in screen if r['kind'] == 'data']
    skipped = [r for r in meta['notEvaluated'] if r['reason'] != 'final-budget']
    passed = meta.get('quantitativePassCount', 0)
    summary = {
        'screening': {'evaluatedCount': len(screen) + passed,
                      'passedCount': passed, 'conditionExcludedCount': len(conditions),
                      'dataUnavailableCount': len(errors),
                      'conditionReasons': grouped(conditions), 'dataReasons': grouped(errors)},
        'notEvaluatedCount': len(skipped), 'notEvaluatedReasons': grouped(skipped),
        'laterExclusions': [r for r in meta['excluded'] if r['stage'] != 'screen'],
        'finalNotEvaluated': [r for r in meta['notEvaluated'] if r['reason'] == 'final-budget'],
    }
    reason = meta.get('outcomeReason')
    if reason in ('market-closed', 'outside-entry-window'):
        explanation = ('휴장 또는 정규장 밖이므로 평가하지 않았습니다.' if reason == 'market-closed'
                       else '신규 진입 평가 시간 밖이므로 평가하지 않았습니다.')
    elif result.get('status') == 'error':
        explanation = '실행 오류: ' + result['error']['message']
    elif result.get('data'):
        explanation = '최종 검증을 통과한 추천 후보 %d개입니다.' % len(result['data'])
    elif passed:
        explanation = '초기 정량 조건을 통과한 %d개 중 최종 검증·유효기간 확인까지 통과한 후보가 없습니다.' % passed
    elif errors:
        explanation = ('초기 평가에서 조건·대상 기준 미충족 %d개, 데이터 부족·오류로 판단 불가 %d개이며, '
                       '통과 후보는 없습니다.') % (len(conditions), len(errors))
    else:
        explanation = '이번 초기 평가에서 조건을 충족한 후보가 없습니다.'
    if skipped:
        explanation += ' 추가 %d개는 미평가이므로 시장 전체에 후보가 없다는 의미는 아닙니다.' % len(skipped)
    meta['evaluationSummary'] = summary
    meta['outcomeExplanation'] = explanation
