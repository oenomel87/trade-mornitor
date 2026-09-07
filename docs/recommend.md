# 단기매매 후보 추천 사용 가이드

`tmon recommend`는 국내 주식 랭킹에서 후보를 모아 가격 돌파·거래량·호가·거래 상태를 확인하고, 선택적으로 뉴스와 공시를 조사한다. 당일매매(`day`)와 2~5거래일 보유를 가정하는 매매(`swing`)를 지원한다. 한 번 실행하면 당시 데이터를 조회하고 결과를 출력한 뒤 종료한다.

이 문서는 구현된 `breakout-v1` 기준이다. 세부 계산과 내부 구조는 [추천 설계](design/recommend.md), 실제 검증 범위는 [검증 기록](implementation-verification.md)을 참고한다.

## 1. 바로 실행하기

프로젝트 루트에서 실행한다. 기본 조회와 같은 토스 인증 환경을 사용한다. 처음 사용하는 환경은 [설치·인증 안내](../README.md#실행)를 먼저 확인한다.

```sh
# 로컬 설정 확인
./bin/tmon doctor

# 웹 조사 없이 당일 정량 후보 확인
./bin/tmon recommend --horizon day --research off

# 뉴스·공시 조사를 포함한 당일 후보, 기본 최대 3개
./bin/tmon recommend --horizon day

# 2~5거래일 후보를 JSON으로 출력
./bin/tmon recommend --horizon swing --json

# 종목당 100만원 투입 가정, 무효화 가격까지 거리가 4% 이하인 후보
./bin/tmon recommend --horizon swing --capital 1000000 --max-loss-pct 4

# 최대 5개 표시
./bin/tmon recommend --horizon day --limit 5
```

`--research auto`가 기본값이다. 웹 조사를 사용하려면 프로젝트의 선택적 SDK 설치와 기존 ChatGPT 로그인이 필요하다. 준비 방법은 [README의 추천 기능 안내](../README.md#단기매매-후보-추천)에 있다. `--research off`는 SDK 없이 정량 분석만 실행한다.

## 2. 분석 대상과 실행 시간

코스피·코스닥 보통주를 대상으로 하며 **NXT 지원 종목도 포함**한다. 현재가·호가·분봉·일봉·랭킹은 **토스 제공 시세 기준**으로 사용한다. 거래소별 데이터를 직접 분리하거나 합산하지 않는다.

추천 시간은 시장 캘린더의 정규장 기준으로 정한다. 아래 시각은 캘린더가 09:00~15:30 정규장, 15:20 종가단일가 시작을 반환하는 경우의 예시다. 종료 시각은 포함하지 않는다.

| 구분 | 진입 평가 시간 | 보유 종료 참고 기준 |
| --- | --- | --- |
| `day` | 개장 30분 후부터 정규장 종료 30분 전까지, 예: 09:30 이상~15:00 미만 | 정규장 종료 10분 전, 예: 15:20 |
| `swing` | 개장 30분 후부터 종가단일가 시작 전까지, 예: 09:30 이상~15:20 미만 | 실행일을 1거래일째로 센 5번째 거래일의 종가단일가 시작 |

휴장일·장외에는 빈 결과로 정상 종료한다. 정규장 안이더라도 진입 평가 시간 밖이면 후보를 만들지 않는다. NXT 프리·애프터마켓에서 추천을 실행하는 기능은 없다.

분봉은 정규장 분석 시간으로 제한하지만, 같은 시간의 NXT 거래를 제거하는 것은 아니다. 일봉은 토스가 반환한 완료 일봉을 사용하며, 정규장만의 OHLCV라고 가정하지 않는다. 실행 시작 때 시장 캘린더의 `previousBusinessDay` 연결로 day 20개·swing 65개의 공통 완료 거래일을 한 번 확인하고, 모든 후보 일봉·swing 지수 창은 그 날짜 집합과 정확히 대조한다. 주말이나 휴장일을 평일 계산으로 채우지 않으며, 기준이 미확인되면 일봉 후보를 검증하지 않는다. swing 신호에는 당일 진행 중인 일봉을 사용하지 않는다.

## 3. 후보를 고르는 과정

1. **공통 기준과 랭킹 수집:** 세션 확인 직후 day 20개 또는 swing 65개의 완료 거래일을 한 번 조회해 기준을 고정한다. 이 조회가 끝난 뒤 day는 1일 거래대금·거래량·상승률, swing은 1일 거래대금·1주 상승률 랭킹을 각각 기본 50개 조회한다. 토스 내 체결 랭킹이 아닌 시장 전체 랭킹을 사용하고, 공급자의 투자 유의 종목 제외 옵션을 적용한다.
2. **상세 평가 대상 선정:** 중복 종목을 합친 뒤 각 랭킹의 `1 / (60 + 순위)` 합계가 높은 순서로 기본정보를 확인한다. 보통주 여부·거래 상태·상장 초기 이력 요건을 통과한 종목에 기본 최대 30개의 상세평가 한도를 사용한다. 사전 제외된 상품 대신 다음 순위 종목을 확인하며 후보군 범위와 데이터 단계 시간 예산은 유지한다. 전 종목 스크리닝은 아니다.
3. **정량 조건 확인:** 아래 돌파·거래량·유동성·진입 범위 조건을 평가한다.
4. **웹 조사:** 정량 상위 최대 5개에 뉴스·공시·예정 일정과 반대 근거를 붙인다. 웹 해석이 정량 가격이나 순위를 바꾸지는 않는다.
5. **최종 재검증:** 원 신호를 고정한 채 최신 가격·호가·상한가·거래 상태·경고와 세션만 다시 확인해 진입·위험·목표·수량을 계산한다. 조사 중 가격이 진입 범위를 벗어나거나 VI·거래정지가 생긴 후보는 제외한다.
6. **출력·기록:** 최종 유효 후보 중 기본 최대 3개를 출력하고 실행에 사용한 데이터를 저장한다.

정량 순서는 시장 상대수익률을 확인할 수 있는 후보 우선, 상대수익률 내림차순, 거래량 배수 내림차순, 스프레드 오름차순, 종목코드 순이다. 웹 조사 후 재검증에서 제외된 후보를 대신해 새로운 후보를 계속 충원하는 방식은 아니다. 상대수익률은 후보 평가 전에 시장별로 한 번 확보한 고정 스냅샷으로 계산한다.

## 4. 기본 추천 조건

| 조건 | 당일매매 `day` | 2~5거래일 `swing` |
| --- | --- | --- |
| 신호 봉 | 최신 완료 5분봉 | 직전 완료 거래일의 일봉 |
| 돌파 | 신호 봉 종가가 직전 5개 5분봉의 최고가 초과 | 신호 봉 종가가 직전 20개 일봉의 최고가 초과 |
| 거래량 | 신호 봉 거래량 ÷ 직전 5개 5분봉 평균 ≥ 1.5배 | 신호 봉 거래량 ÷ 직전 20개 일봉 평균 ≥ 1.5배 |
| 추세 | 별도 이동평균 조건 없음 | 신호 종가 > SMA20 > SMA60, SMA20이 5거래일 전보다 높음 |
| 유동성 | 최근 20개 완료 일봉의 평균 추정 거래대금 ≥ 100억원 | 동일 |
| 스프레드 | 최우선 매도·매수 호가 차이 ÷ 두 가격의 평균 ≤ 0.30% | ≤ 0.50% |
| 진입 범위 | 돌파선 초과~돌파선의 101% 이하 | 돌파선 초과~돌파선의 103% 이하 |
| 무효화 가격 | 신호 봉과 직전 봉의 저가 중 낮은 값 | 신호 일봉과 직전 일봉의 저가 중 낮은 값 |

평균 추정 거래대금은 각 일봉의 **종가 × 거래량**을 평균한 값이다. 실제 체결 거래대금 합계와는 다르다. 최신 체결가와 최우선 매도호가 모두 진입 범위 안에 있어야 한다.

상대수익률 비교 구간은 후보마다 다시 잡지 않는다. `day`는 `signalWindowEndAt` 직전 30개 연속 완료 1분봉(`[F-30분, F)`)의 첫 시가부터 마지막 종가까지, `swing`은 시장 캘린더가 확인한 신호 거래일과 직전 5거래일의 종가까지를 종목·지수에 같은 방식으로 적용한다. 지수 봉은 통화 필드가 없는 `MarketIndicatorCandle` 형식을 전용으로 정규화한 뒤 종목과 같은 완료·미래 시각 필터를 거친다. 진행 중 봉, 종점 누락, 조회 실패 또는 캘린더 근거 부족은 `relativeReturnPct: null`과 `relativeReturnReason`으로 남긴다.

`relativeReturnWindow`에는 `comparisonStartAt`, `comparisonEndAt`, `priceBasis: "provider-index-points"`, `stockPriceBasis: "adjusted"`, `returnCalculation`(`day`는 `first-open-to-last-close`, `swing`은 `close-to-close`)과 완료 정책 버전이 기록된다. 값과 미확인 사유는 스크리닝 단계에서 고정하며 최종 재검증에서 현재 지수를 다시 조회해 구간을 바꾸지 않는다. 상대수익률 미확인은 필수 탈락이 아니며 정렬에서 확인 가능한 값보다 뒤로 배치한다.

최종 후보는 원래 신호의 수정주가 일봉 창과 같은 day 20개·swing 65개의 비수정(`adjusted=false`) 창을 대조한다. 사용 창의 날짜·시각·OHLC·거래량이 모두 관찰상 같을 때만 `finalVerification`을 기록하며, 가격이 다르고 거래량만 같아도 기업행사 전후 거래량 단위가 일관되었다고 보지 않아 `adjustment-basis-unverified`로 제외한다. 일치해도 이 창에서의 관측 일관성만 뜻하며 공급자의 기업행사 전역 보증은 아니다. 비수정 종가×거래량 평균이 기준보다 낮으면 `low-liquidity`로 조건 제외하고, 대조 실패 때도 계산 가능한 평균과 기준을 기록한다. 최종 검증 대상은 조사 전 정량 shortlist 최대 5개로 고정하며 탈락 후보를 뒤 순위로 보충하지 않는다.

이 보수적 대조는 기업행사 이후 두 가격 기준이 정상적으로 연결된 후보를 조기에 제외할 수 있는 false negative 한계를 가진다. 초기 정량 스크리닝에서 탈락한 종목은 원래 shortlist를 고정하기 때문에 이후 비수정 일봉 검증으로 복구되지 않는다. 공급자가 가격·거래량 단위의 조정 계약을 제공하지 않는 동안에는 이 한계를 완화하지 않는다.

공통 캘린더 기준이 확인되면 상장일 이후 실제 기준 창에 남는 거래일 수로 `listing-history-too-short`를 사전 판정한다. 이 근거가 있는 제외는 `partial`이 되지 않으며, `listDate`·요건·실제 가능 봉 수·기준 거래일을 기록한다. 기준이 미확인일 때는 이 판정을 만들지 않고 실제 일봉의 `daily-calendar-unverified` 또는 `daily-gap`으로 남긴다. 휴장일을 추정해 요건을 완화하지 않는다.

5분봉은 연속된 완료 1분봉 5개로 만든다. 완료 판단은 상세평가 대상 확정 뒤 고정한 실제 스크리닝 단계 시작 시각(`phaseStartedAt`, day·swing 공통; swing의 `signalWindowEndAt`은 null) 기준이며, 분봉 종료 후 기본 5초(`barCompletionDelaySeconds`)가 지나야 완료로 취급한다(`barEnd+D<=phaseStartedAt`). 고정 신호 구간은 `n=floor((P-S-D)/300)`, `F=S+n×300`(`S`=정규장 시작, `P`=단계 시작)으로 정하고 `barEnd<=F`로 자르며 지연을 중복 차감하지 않는다. 원천 최신성은 구간 절단 전 최신 사용 가능 봉을 실제 응답 수신 시각으로 검사하므로, 고정 신호가 오래되었다는 이유만으로 정상 도착한 최신 분봉을 공급 지연으로 판정하지 않는다. `n<=0`이면 완료 5분봉이 없고 `n<6`이면 예상된 평가 불가(`session-not-ready`)이며, 필수 최신 `F` 봉이 없으면 이전 봉으로 대체하지 않는다. 누락된 봉을 거래량 0으로 채우지 않는다. 따라서 진입 평가 시작 시각 직후에는 필요한 완료 봉이 아직 부족할 수 있다. 예를 들어 10:00:10 평가(`D=5`)는 09:59 시작 1분봉을 포함한다.

조회 응답에 평가 시각의 바로 다음 분 경계 봉이 포함되면 계산에서 제외하고 `future-minute-bar-skipped` 경고를 남긴다. 미래 판단과 원천 최신성 판단 모두 실제 응답 수신 시각(`receivedAt`) 기준이다(미래만 아님). 예를 들어 09:42:36에 받은 09:43 봉은 사용하지 않는다. 다음 분 경계보다 먼 미래 봉은 `future-minute-bar` 오류로 종목을 제외한다. 이는 공급자 응답에 대응하는 제한적인 처리 정책이며, 봉 시각을 1분 당기거나 종료 시각으로 재해석하지 않는다. 남은 봉의 개수·연속성·최신성 기준은 그대로 적용한다. 경고에는 종목, 평가·수신 시각, 문제 봉 시각을 기록하며, 제외 기록에는 구체적인 오류 메시지도 저장한다.

KRX 거래정지·정리매매, 지원되는 NXT의 명시적 거래정지, 거래소 구분 없는 VI·종목 경고를 제외 조건으로 적용한다. NXT 지원 여부 자체나 REST 거래소 집계 범위의 미확인은 제외 사유가 아니다.

## 5. 결과 읽기

| 출력 / JSON 필드 | 의미 |
| --- | --- |
| 진입 기준 / `entryReference` | 최종 평가 때의 최우선 매도호가 |
| 진입 범위 / `entryRange` | 하한 초과, 상한 이하. 범위를 벗어나면 현재 추천 조건에서 이탈 |
| 무효화 / `invalidation` | 전략의 가격 조건이 무효가 되는 기준 |
| 가격 위험 % / `riskPct` | `(진입 기준 − 무효화 가격) / 진입 기준 × 100` |
| 목표 / `targetRange` | 진입 기준에서 가격 위험폭의 1.5~2배 위에 둔 참고 구간 |
| 거래량 배수 / `volumeRatio` | 위 표의 비교 구간 대비 거래량 비율 |
| `relativeReturnPct` | 고정된 같은 구간의 종목 수익률에서 코스피·코스닥 지수 수익률을 뺀 값, 단위는 %포인트. 미확인 시 `null` |
| `relativeReturnReason` | 상대수익률이 `null`인 이유. 예: `index-fetch-failed`, `index-bar-missing`, `index-data-invalid`, `stock-bars-noncontiguous`, `comparison-basis-unverified` |
| `relativeReturnWindow` | 종목·지수에 공통 적용한 고정 비교 구간, 완료 정책, 실제 지수·종목 가격 기준과 수익률 계산법 |
| `signalAsOf` | 신호에 사용한 봉의 시각. 추천 생성 시각과 다름 |
| `quoteAsOf`, `orderbookAsOf` | 최종 현재가·호가의 원천 시각 |
| `expiresAt` | 이 시세 스냅샷의 유효 기한. 보유 종료 시각이 아님 |
| `reviewDate`, `exitBy` | swing의 다음 거래일 점검일과 보유 종료 참고 시각. 일정 조회 실패 시 null일 수 있음 |
| `venueScope`, `venueBasis` | 각각 `TOSS_PROVIDED`, `provider-default`. 토스 기본 시세 사용 정책 |
| `nxtSupported` | 해당 종목의 NXT 지원 여부를 나타내는 참고 정보 |
| `limitations` | 해당 결과의 계산 가정·확인하지 못한 항목 |

목표 구간은 예상 수익률이나 승률이 아니다. day에서는 상한가를 넘는 목표 상단을 상한가로 낮추며, 상한가가 1.5배 위험폭 목표보다 낮으면 후보를 제외한다.

`--capital`은 **종목당 가정 투입 금액**이다. 후보들 사이에 나눠 배분하는 총예산이 아니다. 수량은 `floor(capital / entryReference)`로 계산하며, 0주이거나 최우선 매도호가 잔량보다 많으면 제외한다. 금액을 지정하지 않으면 수량·호가잔량 적합성을 평가하지 않는다.

`--max-loss-pct 4`는 무효화 가격까지의 거리가 진입 기준의 4%를 넘는 후보를 제외한다는 뜻이다. 자동 손절 주문이나 실제 손실 상한을 설정하지 않는다. 수량·가격상 손실 폭 계산에는 수수료·세금·슬리피지를 포함하지 않는다.

예를 들어 진입 기준 10,000원, 무효화 가격 9,800원이라면 주당 위험폭은 200원, `riskPct`는 2%, 목표 구간은 10,300~10,400원이다. 100만원 투입 가정에서는 100주, 가격상 손실 폭은 20,000원이다. 이는 설명용 숫자이며 실제 추천 결과가 아니다.

## 6. 옵션과 설정 조정

| 옵션 | 기본값 / 사용법 |
| --- | --- |
| `--horizon` | 필수. `day` 또는 `swing` |
| `--market` | `KR`만 지원 |
| `--limit` | 3. 1~5이면서 설정의 `researchLimit` 이하 |
| `--capital` | 미설정. 종목당 가정 투입 원화 금액, 양수 |
| `--max-loss-pct` | 미설정. 가격 위험 % 상한, 0 초과 100 미만 |
| `--research` | `auto` 또는 `off`, 기본 `auto` |
| `--config` | JSON 설정 파일 경로 |
| `--json` | 표 대신 JSON 객체 하나를 stdout에 출력 |
| `--no-color` | 색상 없는 출력, 현재 기본 동작 |

설정 우선순위는 **내장 기본값 → JSON 파일 → 명시한 CLI 옵션**이다. 파일에는 바꿀 항목만 적어도 된다. 전체 항목은 [설정 예제](examples/recommend.json)를 참고한다.

예를 들어 `recommend-local.json`을 다음처럼 작성할 수 있다.

```json
{
  "research": "off",
  "capital": "1000000",
  "maxLossPct": "4",
  "day": {
    "minVolumeRatio": "2.0"
  }
}
```

```sh
./bin/tmon recommend --horizon day --config recommend-local.json

# 이 실행에서만 웹 조사와 종목당 200만원 가정으로 변경
./bin/tmon recommend --horizon day --config recommend-local.json --research auto --capital 2000000
```

자주 조정할 값은 `day`·`swing`의 `minVolumeRatio`, `maxBreakoutGapPct`, `maxSpreadPct`, `minEstimatedAvgDailyAmount`다. 비율의 `Pct`는 퍼센트, `Ratio`는 배수다. 거래대금은 원 단위이며 `10000000000`이 100억원이다. decimal 값은 문자열로 쓰면 소수 표현이 명확하다.

`universe`의 `rankCount`·`detailLimit`은 각각 1~100, `researchLimit`은 1~5이면서 `detailLimit` 이하로 설정한다. `--limit` 기본값 3도 이 제한을 적용받으므로 `researchLimit=2`로 바꿨다면 `--limit 2`를 함께 지정해야 한다.

`detailLimit`은 사전 대상 확인을 통과한 종목의 상세평가 횟수다. `evaluationSummary.screening`의 `evaluatedCount`는 사전 제외까지 포함한 초기 확인 수이며, `detailEvaluatedCount`와 `eligibilityExcludedCount`로 상세평가와 사전 제외를 구분한다. 시간 예산이나 상세평가 한도 이후 후보는 `notEvaluated`에 기록한다.

`freshness`·`budget` 값은 1~3600의 정수 초다. 단계 예산인 `dataSeconds + researchSeconds + finalSeconds`는 `totalSeconds` 이하여야 한다. 알 수 없는 키·중복 키·잘못된 자료형이나 범위는 설정 오류로 처리한다.

`effectiveConfig`와 결과의 `relativeReturnWindowPolicy`에는 실행 정책 버전도 고정한다: `strategyVersion=breakout-v1`, `recommendationPolicyVersion=snapshot-v2`, `signalPolicy=frozen-current-state-v2`, `fillModel=best-ask-visible-depth-v1`, `relativeReturnWindowPolicy=signal-aligned-v2`, `universePolicy=ranking-union-batch-prefilter-detail-limit-v1`. 이 값들은 현재 구현된 정책과 일치해야 하며 임의의 설정 파일 값으로 바꿀 수 없다. 가격 기준 세부 정보는 `meta.relativeReturnBasis`와 각 행의 `relativeReturnWindow`에 남기고, 같은 정책의 명시적 별칭은 `relativeReturnComparisonPolicy`로 함께 기록한다.

## 7. 웹 조사와 대기 시간

웹 조사 모델은 **`gpt-5.6-sol`**, 추론 강도는 **`high`**로 고정한다. SDK 기본 모델이 바뀌어도 이 선택은 유지된다. 설정 파일의 `model`은 생략하거나 `"gpt-5.6-sol"`로 지정한다. 기존 `null`도 sol로 해석하며 다른 모델 값은 설정 오류다. 이전 low 강도 조사 캐시는 재사용하지 않는다.

웹 조사는 정량 후보의 재료·반대 근거·예정 일정과 출처를 덧붙인다. 금액·수량·진입가·목표가를 모델이 결정하지 않는다.

| `researchStatus` | 의미 |
| --- | --- |
| `verified` | 출처와 연결된 조사 결과가 형식 검증을 통과함. 향후 주가나 내용 전체의 사실성을 보장하는 표시는 아님 |
| `no-relevant-source` | 조사에서 관련 근거를 확보하지 못함 |
| `unavailable` | SDK·로그인·시간 초과·출력 검증 등의 이유로 조사를 완료하지 못함 |
| `disabled` | `--research off`로 생략 |

캐시는 day 15분, swing 60분이다. 캐시를 사용해도 가격·호가·거래 상태의 최종 재검증은 수행한다. 조사만 실패하면 유효한 정량 후보를 남기고 `status=partial`, 종료 코드 0으로 반환한다.

기본 전체 실행 예산은 180초이며 데이터 수집 80초, 웹 조사 70초, 최종 검증 여유 30초다. 완료 시간을 보장하는 약속이 아니라 각 단계의 요청을 제한하는 설정이다. 웹 조사가 필요 없는 실행은 `--research off`를 사용한다.

## 8. 빈 결과와 문제 해결

JSON을 처리하는 프로그램은 **종료 코드뿐 아니라 `status`, `error`, `warnings`, `meta.outcomeReason`도 확인**해야 한다. 정상적인 빈 결과는 `data: []`이고, 전체 오류 응답의 `data`는 null일 수 있다.

`meta.executionStatus`(`completed`/`failed`)와 `meta.coverageStatus`(`complete`/`partial`/`not-evaluated`)는 기존 `status`·종료 코드를 바꾸지 않고 실행 성패와 정량 데이터 커버리지를 구분한다. `coverageStatus`는 데이터 단계의 부분 진행(랭킹 일부 실패·데이터 제외·예산 미평가)을 `partial`로 표시하며, 웹 조사만의 실패는 정량 커버리지를 오염시키지 않는다. `meta.expectedIneligibleCount`는 상장일 근거가 있는 예상 제외(`listing-history-too-short` + 상장일·요건·상한 세부 정보) 수, `meta.candidateDataUnavailableCount`는 원인 미확인 데이터 부족이 발생한 후보 심볼 수다(같은 후보의 반복 실패 사건은 1개로 셈; 단계별 사건 수는 기존 `excludedCounts`에 유지). 일반적인 `insufficient-history`·`stale-daily`·`incomplete-bars`·`zero-volume-baseline`은 근거 없이 정상 후보 특성으로 재분류하지 않으며 데이터 판단 불가로 남는다. 모든 후보의 필수 자료가 미확인이면 정상 후보 부재(`no-match`)로 표시하지 않는다.

`meta.outcomeExplanation`은 이번 결과의 의미를 문장으로 설명한다. `meta.evaluationSummary`는 초기 평가의 통과·조건/대상 미충족·데이터 판단 불가, 미평가 사유, 후속 검증 제외를 나누어 제공한다. 예를 들어 30개 중 조건/대상 미충족 29개와 일봉 부족 1개가 있으면 기존 `partial / insufficient-data`를 유지하면서 각각의 개수를 표시한다. 상세평가 한도로 남은 종목은 조건 미충족으로 집계하지 않는다. 후속 제외는 단계별 사건 목록이므로 같은 종목이 여러 단계에서 나타날 수 있다. `no-volume-breakout`은 가격 돌파 또는 거래량 조건 미충족을 뜻하며 거래량만의 실패로 단정하지 않는다.

| 사유 / 코드 | 의미와 확인 방법 |
| --- | --- |
| `market-closed` | 휴장 또는 정규장 밖. 정상 빈 결과이며 종료 코드 0 |
| `outside-entry-window` | 장중이지만 신규 진입 평가 시간 밖. 정상 빈 결과이며 종료 코드 0 |
| `no-match` | 평가된 후보가 조건을 충족하지 않음. 종료 코드 0, `meta.excludedCounts` 확인 |
| `insufficient-data` | 필요한 데이터가 부족함. 부분 결과 또는 전체 오류, 종료 코드 5 |
| `stale-data`, `stale-daily` | 시세나 직전 완료 거래일 데이터가 신선도 기준을 충족하지 않음 |
| `insufficient-minute-bars`, `incomplete-bars` | 완료 분봉 부족 또는 신호 구간 누락. 필수 최신 `F` 봉이 없으면 대체 없이 제외 |
| `session-not-ready` | 장 시작 후 완료 5분봉 6개가 아직 생성될 수 없는 예상된 평가 불가. 데이터 미확인으로 집계하지 않음 |
| `no-volume-breakout`, `trend-not-confirmed` | 돌파·거래량 또는 swing 추세 조건 미충족 |
| `wide-spread`, `entry-invalidated` | 호가 차이가 크거나 가격이 진입 범위 밖 |
| `trading-suspended`, `stock-warning-*` | 거래정지 또는 VI 등 종목 경고 |
| `invalid-recommend-config` | 설정 키·숫자·한도 확인. 종료 코드 2 |
| `research-unavailable` | 정량 후보는 볼 수 있지만 웹 조사는 완료되지 않음 |

최종 시세·호가는 기본 15초, 랭킹은 120초 이내여야 한다. 최종 단계는 신호용 분봉을 다시 조회하지 않는다. 출력 유효 기한은 시세·호가·상태의 남은 허용 시간과 원 신호 수명·진입 세션 종료를 함께 고려하므로 30초보다 짧을 수 있다. 오래된 결과를 현재 추천으로 사용하려면 명령을 다시 실행한다.

인증·연결 문제는 `./bin/tmon doctor`로 로컬 설정을 먼저 확인한다. 원격 연결까지 확인하려면 `./bin/tmon doctor --remote`를 사용한다. 인증·권한 오류는 종료 코드 3, 네트워크·호출 제한·공급자 장애는 4이며 일부 종목에만 실패하면 부분 결과와 5가 반환될 수 있다.

`meta.preExcluded`에는 랭킹 합집합을 최대 200개 심볼 단위로 조회한 사전 조건 제외와 데이터 미확인을 각각 `kind=condition`/`kind=data`로 기록한다. `meta.excluded`에는 상세평가 이후 후보별 제외 사유와 단계, `meta.notEvaluated`에는 상세 평가 한도·시간 예산으로 평가하지 못한 종목이 기록된다. 이 셋을 구분하면 조건이 엄격한지, 데이터나 시간 예산이 부족한지 판단하기 쉽다.

## 9. 실행 기록과 운영 점검

출력의 `meta.recordPath` 또는 화면의 ‘실행 기록’에서 해당 실행의 `result.json`을 찾을 수 있다. `meta.tradingDateReference`에는 공통 거래일 창의 날짜·조회 경로·미확인 사유를, `meta.dailyChecks`와 `meta.adjustmentChecks`에는 후보별 검증 결과를 남긴다.

| 환경 | 기본 기록 위치 |
| --- | --- |
| macOS | `~/Library/Application Support/tmon/recommend/runs/<runId>/` |
| Linux | `$XDG_DATA_HOME/tmon/recommend/runs/<runId>/`, 미설정 시 `~/.local/share/tmon/recommend/runs/<runId>/` |

각 폴더에는 다음 파일을 저장한다.

- `result.json`: 표시 결과, 적용 설정·해시, 단계별 시간, 후보·제외·미평가 정보. PR1부터 `meta.phaseStartedAt`(day·swing 공통)·`meta.signalWindowEndAt`(swing은 null)·`meta.barCompletionDelaySeconds`(day)를 기록한다.
- `inputs.json`: 사용한 시세 응답·조회 시각·후보 선정 입력. `tradingDateReference`에 실행 전에 확인한 day 20개·swing 65개 날짜와 출처를 보존하며, `dailyChecks`에는 종목별 날짜 대조 결과, `adjustmentChecks`에는 최종 raw 일봉 대조와 비수정 종가×거래량 평균을 보존한다. `screened`에 조사 전 정량 통과 행 전체의 독립 복사본을 보존한다. 각 행은 통과 직후 즉시 추가되므로 이후 후보의 인증 실패·중단 시에도 앞선 통과분이 남고, `quantitativePassCount`는 보존된 행에서 복원된다. 각 행은 `signalId`/`signalInputHash`(입력 OHLCV 정규화 SHA-256), 돌파선·무효화 가격·거래량 배수, `relativeReturnWindow`(원 비교 구간), 최초 진입가·위험폭·목표·수량을 포함하며 이후 조사·최종 가격 변경으로 수정되지 않는다.
- `research.json`: 최종 출력 후보의 조사 요약과 출처

`meta.endpointCounts`·`meta.endpointGroupCounts`·`meta.requestPhaseCounts`는 논리 조회 호출 수(레코드 1개당 1호출, 호환 유지)이며, `meta.actualAttemptsByEndpoint`·`meta.actualAttemptsByGroup`은 각 호출 안에 중첩 persisted된 실제 전송 시도 합계로 인증 토큰 발급 작업까지 포함한다. 스크리닝은 사전 종목 정보를 재사용하므로 종목 상세(`/api/v1/stocks`) 호출이 후보당 2회에서 1회로 줄고, STOCK 그룹은 3회에서 2회가 된다. 최종 검증은 종목 정보·경고를 항상 새로 조회한다. 각 조회 기록은 `requestStartedAt`·`receivedAt`(기존 `retrievedAt`과 동일)·`endpointGroup`·`attemptCount`·`rateLimitWaitSeconds`·`retryWaitSeconds`·`elapsedSeconds`·성공 여부·안전한 오류 코드를 포함하며, 토큰·인증 헤더·요청 본문·원문 오류 메시지는 저장하지 않는다. 대기 시간은 계획값이 아니라 일시정지 전후 단조 시계로 잰 실제 대기이며, 예산 거부·중단은 실제 0초로 기록된다. 인증 토큰 발급 호출은 대상 조회 기록에 합산되지 않고 전송 관측에만 별도로 남는다. 오류 코드는 기존 안전 패턴에 맞게 정규화되며 부적합한 원문 코드는 저장하지 않는다. 시간 예산·경과에는 단조 시계, 관측 시각에는 시간대가 명시된 실제 시각을 사용한다.

기록과 뉴스 캐시는 별도이며 자동 삭제하지 않는다. 저장 실패 시 가능한 추천 결과를 유지하고 `record-write-failed` 경고를 표시한다.

초기 운영에서는 같은 설정으로 여러 실행의 후보 수·제외 사유·소요 시간을 비교하고, 설정을 바꿀 때는 기록된 `effectiveConfig`와 `configHash`로 구분한다. 실제 장중 지연·봉 확정과 성과 검증은 [검증 기록](implementation-verification.md)에 남은 항목이다. 현재 명령은 자동 주문·보유 감시·후속 수익률 추적을 수행하지 않는다.

## 10. snapshot-v2 단계별 구현 범위

PR0(기록·호출 중복 제거·관측·오류 분류), PR1(실제 시각·완료 봉 정합성), PR2(고정 상대수익률·지수 캐시), PR3(원 신호 고정·최종 진입 재평가·수명), PR4-A(사전 필터 후 상세평가 한도), PR4-B(공통 거래일·일봉 결측·조정 기준 검증)가 구현되었다. PR4-A는 랭킹 합집합을 먼저 사전 조회하고, 통과한 후보에만 상세평가 한도를 적용한다. PR4-B의 공통 달력은 day 19회, swing 64회의 연결 조회가 추가될 수 있으며, 이는 종목마다 반복하지 않는다.

## 14. 원 신호와 최종 확인 (snapshot-v2)

스크리닝이 통과한 각 후보는 계산에 사용한 OHLCV 구간과 가격 기준, 신호 설정을 독립 복사한 `signalSnapshot`으로 고정한다. `signalId`/`signalInputHash`는 종목·horizon·실제 입력 봉·신호 설정·가격 기준으로 만들며 현재 호가, 자본금, 조사 결과, 관찰 시각을 포함하지 않는다. `benchmarkSnapshotHash`는 고정 상대수익률 지수 자료를 별도로 식별한다.

뉴스 조사는 이 스냅샷의 복사본을 받는다. 조사 중 후보 설명이나 중첩 신호 자료가 바뀌어도 최초 `inputs.screened`와 최종 정량 계산에는 영향을 주지 않는다. 최종 단계는 원 신호를 다시 계산하지 않고 시장 캘린더·세션, 종목 상태·경고, 현재가·호가·상한가만 새로 확인한 뒤 진입가·스프레드·위험폭·목표·수량·잔량 적합성을 계산한다.

`day` 신호는 봉 종료 시각부터 `maxSignalAgeSeconds`(기본 600초)까지 유효하며 만료 시각 이상은 제외한다. `swing` 신호는 실행 거래일의 `previousBusinessDay` 일봉인지 확인하고 해당 거래일의 진입 평가 종료까지 유효하다. `nextBarCompletionAt`은 안내용 다음 봉 완료 시각이고 신호 만료 판단에 사용하지 않는다. `stateAsOf`의 기존 의미는 유지하면서 종목·경고별 확인 시각을 따로 기록하고 각 시각의 TTL을 출력 만료 계산에 반영한다.

메타데이터에는 `recommendationPolicyVersion=snapshot-v2`, `signalPolicy=frozen-current-state-v2`, `fillModel=best-ask-visible-depth-v1`, `relativeReturnWindowPolicy=signal-aligned-v2`, `relativeReturnComparisonPolicy=signal-aligned-v2`, 실제 적용한 `universePolicy`, `effectiveConfig`·`configHash`, 소스 변경을 포함한 `codeRevision`을 기록한다. 가격 기준 세부 정보는 `relativeReturnBasis`와 각 행의 `relativeReturnWindow`에 보존한다.

저장과 JSON 직렬화도 유효성 경계에 포함한다. 저장 직전과 저장 직후 만료를 확인해 같은 신호로 한 번 재검증하고, 지연으로 만료되면 만료 후보를 제외한 결과를 같은 실행 기록에 저장한다. CLI와 `result.json`은 같은 십진 직렬화 경로를 사용하므로 화면과 저장된 출력이 같은 스냅샷을 가리킨다.
