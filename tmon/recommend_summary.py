"""Explain screening coverage separately from later verification outcomes."""
from collections import Counter


LABELS = {
    'not-common-stock': '대상 제외(보통주 아님)',
    'no-volume-breakout': '가격 돌파 또는 거래량 조건 미충족',
    'low-liquidity': '유동성 기준 미충족',
    'entry-invalidated': '진입 가격 조건 미충족',
    'insufficient-history': '일봉 이력 부족',
    'listing-history-too-short': '상장 초기로 최소 일봉 요건 미충족',
    'insufficient-minute-bars': '완료 분봉 부족',
    'incomplete-bars': '분봉 누락 또는 최신 완료 봉 부족',
    'session-not-ready': '장 시작 후 완료 5분봉 부족(예상된 평가 불가)',
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
    # PR4-A keeps the batch precheck ledger separate from quantitative and
    # final exclusions.  The two kinds still contribute to screening coverage
    # exactly once.
    pre = list(meta.get('preExcluded') or [])
    pre_conditions = [r for r in pre if r.get('kind') == 'condition']
    pre_errors = [r for r in pre if r.get('kind') == 'data']
    screen = [r for r in meta['excluded'] if r['stage'] in ('eligibility', 'screen')]
    eligibility = [r for r in screen if r['stage'] == 'eligibility']
    # session-not-ready is expected evaluation unavailability (session exists
    # but required 6 completed 5m bars cannot exist yet), not a normal
    # quantitative condition failure nor a data defect.
    session_unready = [r for r in screen if r.get('reason') == 'session-not-ready']
    conditions = [r for r in screen if r['kind'] == 'condition' and r.get('reason') != 'session-not-ready']
    errors = [r for r in screen if r['kind'] == 'data']
    skipped = [r for r in meta['notEvaluated'] if r['reason'] != 'final-budget']
    passed = meta.get('quantitativePassCount', 0)
    # PR0 additive classification (old status/exit codes unchanged):
    # expected = listing-history-too-short WITH listDate/requiredDailyBars/
    # maxPossibleDailyBars evidence; generic history/staleness/contiguity
    # failures stay candidate-data-unavailable.
    expected = sum(1 for r in meta['excluded'] + pre
                   if r.get('expectedIneligible') or
                   (r.get('reason') == 'listing-history-too-short' and
                    all(k in r for k in ('listDate', 'requiredDailyBars', 'maxPossibleDailyBars'))))
    # Distinct symbols (repeated failure events for one symbol count once).
    unavailable = len({r['symbol'] for r in meta['excluded'] + pre
                       if r.get('kind') == 'data'})
    ranking_gap = any(w.get('code') == 'ranking-unavailable' for w in result.get('warnings', []))
    meta.setdefault('expectedIneligibleCount', expected)
    meta.setdefault('candidateDataUnavailableCount', unavailable)
    meta.setdefault('executionStatus', 'failed' if result.get('status') == 'error' else 'completed')
    if 'coverageStatus' not in meta:
        reason = meta.get('outcomeReason')
        if reason in ('market-closed', 'outside-entry-window'):
            meta['coverageStatus'] = 'not-evaluated'
        elif result.get('status') == 'error' and len(screen) + passed == 0:
            meta['coverageStatus'] = 'not-evaluated'
        elif unavailable > 0 or meta.get('notEvaluated') or ranking_gap:
            meta['coverageStatus'] = 'partial'
        else:
            meta['coverageStatus'] = 'complete'
    summary = {
        'screening': {'evaluatedCount': len(pre) + len(screen) + passed,
                      'detailEvaluatedCount': len(screen) - len(eligibility) + passed,
                      # Keep the legacy field's meaning: all candidates
                      # removed before detailed quantitative evaluation,
                      # including precheck data failures.  The new additive
                      # preExcludedCount below remains condition-only.
                      'eligibilityExcludedCount': len(pre) + len(eligibility),
                      'passedCount': passed,
                      'conditionExcludedCount': len(pre_conditions) + len(conditions),
                      'dataUnavailableCount': len(pre_errors) + len(errors),
                      'preExcludedCount': len(pre_conditions),
                      'precheckDataUnavailable': len(pre_errors),
                      'detailSelectedCount': meta.get('detailSelectedCount', 0),
                      'expectedUnavailabilityCount': len(session_unready),
                      'conditionReasons': grouped(pre_conditions + conditions),
                      'dataReasons': grouped(pre_errors + errors),
                      'expectedUnavailabilityReasons': grouped(session_unready)},
        'notEvaluatedCount': len(skipped), 'notEvaluatedReasons': grouped(skipped),
        'laterExclusions': [r for r in meta['excluded'] if r['stage'] not in ('eligibility', 'screen')],
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
    elif pre_errors or errors:
        explanation = ('초기 평가에서 조건·대상 기준 미충족 %d개, 데이터 부족·오류로 판단 불가 %d개이며, '
                       '통과 후보는 없습니다.') % (len(pre_conditions) + len(conditions),
                                                  len(pre_errors) + len(errors))
    elif session_unready:
        explanation = ('장 시작 후 완료 5분봉 6개가 아직 생성될 수 없어 평가하지 않았습니다 '
                       '(예상된 평가 불가 %d개).') % len(session_unready)
    else:
        explanation = '이번 초기 평가에서 조건을 충족한 후보가 없습니다.'
    if skipped:
        explanation += ' 추가 %d개는 미평가이므로 시장 전체에 후보가 없다는 의미는 아닙니다.' % len(skipped)
    meta['evaluationSummary'] = summary
    meta['outcomeExplanation'] = explanation
