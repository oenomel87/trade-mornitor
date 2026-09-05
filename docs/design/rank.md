# 랭킹 조회

2단계의 두 번째 기능이다. 순위는 토스증권 API가 제공한 값을 유지하고, 선택한 코드를 기존 조회·분석 명령에 사용할 수 있게 한다.

```sh
tmon rank
tmon rank --market KR --by amount
tmon rank --market US --by gain --limit 20
tmon rank --market KR --by loss --duration 1w
tmon rank --market US --by volume --source toss --duration realtime
tmon rank --exclude-caution --json
```

## 입력

| 옵션 | 값 | 기본값 |
| --- | --- | --- |
| `--market` | `KR`, `US` | `KR` |
| `--by` | `amount`, `volume`, `gain`, `loss` | `amount` |
| `--duration` | `realtime`, `1d`, `1w`, `1mo`, `3mo`, `6mo`, `1y` | `1d` |
| `--source` | `market`(시장 전체), `toss`(토스증권 체결) | `market` |
| `--limit` | 1~100 | 20 |
| `--exclude-caution` | 투자 유의 종목 제외 | 미적용 |

`gain/loss + realtime`, `gain/loss + source=toss`는 지원하지 않으므로 인증 전에 종료 코드 2로 거부한다. `--json`, `--no-color`, `--help`는 기존 명령과 같다.

## API와 출력

- `GET /api/v1/rankings`를 한 번 조회한다. 시세 데이터 캐시와 반복 감시는 추가하지 않는다.
- `amount/volume`는 source에 따라 `MARKET_TRADING_*` 또는 `TOSS_SECURITIES_TRADING_*`로 변환한다. `gain/loss`는 `TOP_GAINERS/TOP_LOSERS`다.
- 결과: `rank`, `symbol`, `currency`(가격 통화), `lastPrice`, `basePrice`, `changePct`, `tradingVolume`, `tradingAmount`, `tradingAmountCurrency`.
- `changeRate`의 소수비율을 `changePct`의 퍼센트 단위로 변환한다. 음수도 보존한다. 누락·null은 `N/A`로 표시하며, 0으로 대체하거나 재계산하지 않는다.
- 거래량과 거래대금은 선택 기간 누적값이다. `gain/loss`의 기준가·등락률은 기간 시작 대비, `amount/volume`는 기간과 무관하게 전일 대비다. 이 차이를 표와 JSON 메타데이터에 표시한다.
- 공급자 순위를 보존하고 순위 오름차순으로 출력한다. 누락된 순위를 임의로 채우거나 재번호를 붙이지 않는다.
- 시장·기간·집계 범위·투자 유의 종목 필터·랭킹 집계 시각·조회 시각을 표시한다. 가격 통화는 원화·달러를 구분하며 환산하지 않는다.
- 미국 실응답의 거래대금은 USD 가격·거래량과 비교해 단위가 불분명한 값이었다. 문서는 거래대금 통화를 별도로 정의하지 않으므로 원본 값을 유지하고 `tradingAmountCurrency=null`과 경고를 표시한다. 국내 거래대금 통화는 `KRW`다. USD 값을 추측하거나 환율로 임의 변환하지 않는다.
- 종목명 보강을 위한 별도 API 호출은 하지 않는다. 필요한 이름은 `search CODE`로 확인한다.

## 빈 결과와 오류

- 빈 `rankings`는 정상 결과(`data=[]`, 종료 코드 0)다. 집계 데이터 없음으로 안내한다.
- 요청보다 적게 반환되는 것은 API의 정상 동작이다. 실제 개수를 표시하고 경고하되 종료 코드 0을 유지한다. 부족 사유는 응답만으로 단정하지 않는다.
- `rankedAt`이 없으면 미제공으로 표시한다. 데이터가 있는데 시각을 알 수 없으면 경고한다.
- 잘못된 필드·중복 심볼·시장과 다른 통화·요청 한도를 넘은 응답은 데이터 오류로 처리한다.
- 429·네트워크·서버 장애는 기존 클라이언트의 제한된 재시도·종료 코드를 사용한다.

근거: [랭킹 명세](https://developers.tossinvest.com/docs/ranking), [OpenAPI JSON](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json).
