import contextlib
import io
import unittest

from tmon.recommend_summary import summarize
from tmon.recommend_render import render_recommend


class SummaryTests(unittest.TestCase):
    def result(self):
        excluded = []
        for reason, count, kind in [('no-volume-breakout', 11, 'condition'),
                                    ('not-common-stock', 15, 'condition'),
                                    ('low-liquidity', 2, 'condition'),
                                    ('entry-invalidated', 1, 'condition'),
                                    ('insufficient-history', 1, 'data')]:
            excluded.extend({'symbol': str(len(excluded) + i), 'stage': 'screen',
                             'reason': reason, 'kind': kind} for i in range(count))
        return {'status': 'partial', 'data': [], 'meta': {
            'horizon': 'day', 'strategyVersion': 'breakout-v1', 'universeCount': 114,
            'outcomeReason': 'insufficient-data', 'excluded': excluded,
            'notEvaluated': [{'symbol': str(i), 'reason': 'detail-limit'} for i in range(84)]}}

    def test_mixed_outcome_explains_coverage_and_preserves_status(self):
        result = self.result()
        summarize(result)
        meta = result['meta']
        screen = meta['evaluationSummary']['screening']
        self.assertEqual((screen['evaluatedCount'], screen['conditionExcludedCount'],
                          screen['dataUnavailableCount']), (30, 29, 1))
        self.assertEqual(meta['outcomeReason'], 'insufficient-data')
        self.assertEqual(result['status'], 'partial')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            render_recommend([], meta, lambda *args: None)
        self.assertIn('일봉 이력 부족 1개', output.getvalue())
        self.assertIn('상세평가 한도 84개', output.getvalue())
        self.assertIn('가격 돌파 또는 거래량 조건 미충족 11개', output.getvalue())

    def test_final_exclusions_do_not_count_as_screening_failures(self):
        result = self.result()
        result['meta'].update(quantitativePassCount=1, excluded=[
            {'symbol': 'x', 'stage': stage, 'kind': 'data', 'reason': 'expired-candidate'}
            for stage in ('expiry-retry', 'output')], notEvaluated=[])
        summarize(result)
        summary = result['meta']['evaluationSummary']
        self.assertEqual(summary['screening']['evaluatedCount'], 1)
        self.assertEqual(summary['screening']['dataUnavailableCount'], 0)
        self.assertIn('최종 검증', result['meta']['outcomeExplanation'])

    def test_closed_session_is_not_described_as_no_match(self):
        result = self.result()
        result['meta'].update(outcomeReason='market-closed', excluded=[], notEvaluated=[])
        summarize(result)
        self.assertIn('평가하지 않았습니다', result['meta']['outcomeExplanation'])
