# tmon — 주가 조회·분석 CLI

토스증권 Open API를 사용하는 조회·분석 CLI다. 종목 검색·랭킹·관심종목 관리, 현재가·일봉·기술적 지표와 국내 당일·2~5거래일 돌파 후보 추천, 시황·주요 뉴스 브리핑을 지원한다. 추천과 브리핑에는 선택적으로 Codex 웹 조사를 연결한다. 주문·계좌 API는 포함하지 않는다.

## 실행

Python 3.10 이상과 macOS 또는 Linux가 필요하다. 기본 조회와 정량 추천은 표준 라이브러리만 사용한다. `./bin/tmon`은 프로젝트 `.venv`가 있으면 자동으로 사용한다. Codex 웹 조사는 선택적 SDK 설치가 필요하다.

```sh
./bin/tmon --help
./bin/tmon doctor
./bin/tmon doctor --remote
./bin/tmon search 삼성전자
./bin/tmon search 현대자동차 --market KR
./bin/tmon rank --market KR --by amount
./bin/tmon watchlist add 005930 005380 AAPL
./bin/tmon watchlist quote
./bin/tmon quote 005930 AAPL
./bin/tmon history 005930 --count 20
./bin/tmon analyze AAPL
./bin/tmon analyze AAPL --json
./bin/tmon brief
```

`python3 -m tmon ...`도 동일하게 동작한다. 다른 디렉터리에서는 `/path/to/trade-mornitor/bin/tmon`처럼 실행 파일의 절대 경로를 사용할 수 있다. Python 패키지로 설치할 경우 `pyproject.toml`의 `tmon` 실행 진입점을 이용할 수 있다.

환경 변수 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`은 이미 설정된 값을 사용한다. `.env` 파일은 자동으로 읽지 않는다. 실행 컴퓨터의 IP가 토스증권 WTS의 허용 IP에 등록되어 있어야 한다.

## 명령

| 명령 | 결과 |
| --- | --- |
| `doctor` | 환경 변수 존재 여부와 로컬 캐시 접근 점검. 네트워크 요청 없음 |
| `doctor --remote` | 인증 및 삼성전자 현재가 접근 확인 |
| `search QUERY` | 종목명·코드 검색. 국내·미국 전체 시장, 기본 20개 표시 |
| `rank` | 거래대금·거래량·상승률·하락률 랭킹. 기본 국내·1일·시장 전체·거래대금 20개 |
| `watchlist [list]` | 저장된 관심종목 목록 표시 |
| `watchlist add SYMBOL ...` | 관심종목 코드 추가, 중복 제외 |
| `watchlist remove SYMBOL ...` | 지정한 관심종목 코드 삭제 |
| `watchlist quote` | 관심종목 전체 현재가를 저장 순서대로 조회 |
| `profile [list]` | 프로필 목록·종목 수·현재 선택 표시 |
| `profile create NAME` / `profile use NAME` | 빈 프로필 생성 / 사용할 프로필 선택 |
| `profile rename OLD NEW` / `profile delete NAME` | 프로필 이름 변경 / 삭제 |
| `quote SYMBOL [SYMBOL ...]` | 최대 200종목의 현재가·통화·시세 시각 |
| `history SYMBOL --count N` | 완료된 수정주가 일봉 1~200개, 기본 20개 |
| `history SYMBOL --unadjusted` | 수정주가 미적용 일봉 |
| `analyze SYMBOL` | 최대 120봉으로 기본 기술적 지표 계산 |
| `brief` | KST 기준 장전·장중·장마감·휴장일 시황과 주요 뉴스 브리핑 |
| `recommend --horizon day` | 장중 5분봉 돌파 후보, 기본 최대 3개 |
| `recommend --horizon swing` | 완료 일봉의 20일 고가 돌파 후보, 2~5거래일 보유 가정 |

모든 명령에서 `--json`, `--no-color`를 지원한다. 기본 출력은 색상 없는 표다. JSON의 금액·수량·계산 결과는 십진 문자열이며 소수점 8자리까지 반올림한다. 변화율의 `Pct`는 퍼센트, `Ratio`는 배수다. `--help`는 항상 일반 도움말을 출력한다.

`analyze`의 `volumeRatio`는 최근 완료 일봉 거래량을 **그 직전 20봉의 평균 거래량**으로 나눈 값이다. 추천 day의 거래량 배수는 최신 완료 5분봉과 직전 5개 5분봉 평균의 비교다. RSI는 Wilder 방식이며 계산 기간과 봉 수가 결과에 포함된다. 지표값이 없는 경우 0으로 채우지 않고 `null`과 사유를 제공한다.

## 시황·주요 뉴스 브리핑

`tmon brief`는 국내 지수·KRX 거래대금과 투자자별 순매수·랭킹을 조회하고 Codex SDK로 국내외 주요 뉴스와 예정 일정을 조사한다. 한국시간과 시장 캘린더로 브리핑 유형을 자동 선택하며 휴장일에도 동작한다. 뉴스 사실과 영향 해석, 원문 출처·게시 시각을 구분한다.

```sh
./bin/tmon brief
./bin/tmon brief --session premarket
./bin/tmon brief --session close --json
./bin/tmon brief --profile 국내주식
./bin/tmon brief --research off
./bin/tmon brief --refresh --timeout 300
```

뉴스 캐시는 장중 10분·그 외 30분이고 시세는 매번 조회한다. 기본 실행 예산은 240초다. 기존 추천의 ChatGPT 로그인·SDK·모델 설정을 사용한다. `--profile`은 선택한 프로필만 읽으며 기본 앞 10개를 조회·조사한다. 브리핑 커맨드는 한 번 실행 후 종료하며 예약·알림 전송은 별도로 연결한다. [상세 사용법과 데이터 기준](docs/brief.md)을 참고한다.

## 단기매매 후보 추천

실행 예제, 추천 조건, 결과 해석과 문제 해결은 [추천 기능 사용 가이드](docs/recommend.md)를 참고한다.

```sh
./bin/tmon recommend --horizon day
./bin/tmon recommend --horizon swing --json
./bin/tmon recommend --horizon day --research off
./bin/tmon recommend --horizon swing --capital 1000000 --max-loss-pct 4
./bin/tmon recommend --horizon day --config docs/examples/recommend.json
```

랭킹으로 후보를 모으고 완료 봉·거래량·호가·거래 상태를 평가한다. 정량 상위 후보의 뉴스·공시를 조사한 뒤 최신 시세와 조건을 재검증한다. 결과에는 진입 기준·범위, 무효화 가격, 위험폭의 1.5~2배로 정한 목표 구간, 시세 시각·유효 기한, 출처를 표시한다. 수치 기준은 [설정 예제](docs/examples/recommend.json)를 복사해 조정할 수 있다.

**현재 지원 범위:** KRX 정규장 시간의 코스피·코스닥 보통주이며 NXT 지원 종목도 포함한다. 현재가·호가·분봉·일봉·랭킹은 거래소를 분리하거나 직접 합산하지 않고 **토스 제공 시세 기준**으로 사용한다. JSON에는 `venueScope=TOSS_PROVIDED`, `venueBasis=provider-default`를 기록한다. 첫 버전은 랭킹에서 최대 30개를 상세 평가하며 전 종목 스크리닝은 아니다.

일봉은 토스가 제공하는 완료 일봉을 그대로 사용하므로 정규장만의 OHLCV라고 가정하지 않는다. 거래소별 합산 방식과 과거 집계 기준은 운영 관측 항목이며, 범위 미확인만으로 종목을 제외하지 않는다. KRX 또는 지원되는 NXT의 거래정지가 명시되거나 거래소 구분 없이 VI·종목 경고가 있으면 해당 후보를 제외한다.

day는 개장 30분 후부터 정규장 종료 30분 전까지, swing은 개장 30분 후부터 종가단일가 시작 전까지 신규 진입을 평가한다. 휴장·장외에는 빈 결과로 정상 종료한다. 단순 조건 미충족(`no-match`)과 데이터 부족(`insufficient-data`)을 구분한다. 필수 시세나 분봉이 오래됐으면 추천하지 않는다.

웹 조사 설정은 기본 `auto`이며 모델은 `gpt-5.6-sol`, 추론 강도는 `high`로 고정한다. 아래처럼 SDK를 설치하고 Codex CLI에 ChatGPT로 로그인해 사용한다.

```sh
uv venv --python python3.11 .venv
uv pip install --python .venv/bin/python -e '.[ai]'
codex login status
# 로그인이 없는 환경에서만 실행
codex login
```

SDK 0.147.0과 포함 런타임을 사용한다. API 키 로그인으로 자동 전환하지 않는다. SDK 미설치·인증 불가·검색 시간 초과 시 정량 결과를 보존하고 웹 조사 불가를 표시한다. `--research off`는 SDK를 실행하지 않는다. 웹 해석은 정량 순위를 바꾸지 않으며 확인된 재료와 반대 근거를 덧붙인다. 조사에는 종목 식별 정보만 보내고 토스 인증값과 투입 금액은 전달하지 않는다.

실행 기록은 macOS의 `~/Library/Application Support/tmon/recommend/runs/`, Linux의 `$XDG_DATA_HOME/tmon/recommend/runs/`(기본 `~/.local/share/tmon/recommend/runs/`)에 저장한다. `result.json`, 사용 데이터 `inputs.json`, 출처·요약 `research.json`을 보존한다. 뉴스 캐시는 day 15분, swing 60분이다. 자동 삭제는 하지 않는다.

추천 전체 예산은 기본 180초이며 일부 종목의 데이터가 없거나 예산에 걸리면 부분 결과와 종료 코드 5다. 웹 조사만 불가하면 `status=partial`, 종료 코드 0이다. [상세 조건과 한계](docs/design/recommend.md)를 참고한다.

## 종목 검색

PATH를 등록한 터미널에서는 아래처럼 사용할 수 있다.

```sh
tmon search 삼성전자
tmon search 현대자동차 --market KR
tmon search 애플 --market US
tmon search AAPL --market NASDAQ --json
tmon search 반도체 --limit 50
tmon search 현대차 --refresh
```

검색 결과에는 코드·이름·시장·종목 유형이 표시된다. 결과의 코드를 이용해 `tmon analyze 005380`처럼 분석한다. 한글명·코드의 부분 일치를 지원하며 공백·대소문자 차이는 무시한다. 완전 일치가 먼저 나온다.

`--market`은 `ALL`(기본), `KR`, `US`, `KOSPI`, `KOSDAQ`, `KR_ETC`, `NYSE`, `NASDAQ`, `AMEX`, `US_ETC` 중 선택한다. `--limit`는 1~200개이며 출력 제한과 전체 일치 개수를 구분한다. 검색 0건은 정상 결과다.

종목 목록은 토스증권 거래 가능 `ACTIVE` 종목 기준이며 ETF·ETN 등도 포함한다. API 목록에는 한글명만 제공되므로 전체 영문 회사명 검색은 지원하지 않는다. 초기 별칭으로 `apple`, `현대자동차`, `hyundai motor`, `samsung electronics`를 지원한다.

시장별 목록은 사용자 캐시 폴더 아래 `stocks-v1/`에 **24시간 캐시**한다. 처음 조회할 때 선택한 시장의 목록을 받아오며, 캐시가 유효하면 인증·네트워크 없이 재검색한다. `--refresh`로 강제 갱신할 수 있다. 만료 캐시 갱신이 네트워크 장애로 실패한 일반 검색은 과거 목록을 사용하면서 시각·경고·종료 코드 5를 표시한다. 강제 갱신 실패나 캐시가 없는 시장의 실패는 검색 오류로 처리한다.

## 랭킹 조회

```sh
tmon rank --market KR --by amount
tmon rank --market US --by gain --limit 20
tmon rank --market KR --by loss --duration 1w
tmon rank --market US --by volume --source toss --duration realtime
tmon rank --exclude-caution --json
```

- `--by`: `amount` 거래대금, `volume` 거래량, `gain` 상승률, `loss` 하락률.
- `--duration`: `realtime`, `1d`(기본), `1w`, `1mo`, `3mo`, `6mo`, `1y`.
- `--source`: `market`(기본, 시장 전체) 또는 `toss`(토스증권 체결).
- `--limit`: 1~100, 기본 20. `--exclude-caution`은 투자 유의 종목을 제외한다.

상승률·하락률은 `realtime`과 `--source toss`를 지원하지 않는다. 이 조합은 호출 전에 오류로 안내한다.

**등락률 기준을 구분해야 한다.** 상승률·하락률 랭킹은 선택 기간 시작 대비이며, 거래대금·거래량 랭킹의 등락률은 기간과 무관하게 전일 대비다. 거래량·거래대금 자체는 선택 기간 누적값이다. 결과에 집계 시각과 집계 범위를 표시한다.

현재가는 가격 통화와 함께 표시한다. 미국 거래대금은 실제 응답과 명세만으로 통화를 확정하지 못해 **API 원본 값, 거래대금 통화 `N/A`**로 표시한다. JSON에서는 `tradingAmountCurrency=null`이다. 달러 금액으로 간주하거나 환산하지 않는다.

집계되지 않은 조합은 0건으로 정상 종료한다. API가 요청보다 적은 종목을 반환할 수도 있으며 공급자 순위와 값을 그대로 유지한다. 랭킹의 코드를 `tmon search CODE`로 확인하거나 `tmon analyze CODE`로 분석할 수 있다.

## 관심종목 관리

```sh
tmon profile create 국내주식
tmon profile use 국내주식
tmon watchlist add 005930 005380
tmon watchlist
tmon watchlist quote

# 현재 선택을 바꾸지 않고 다른 프로필 사용
tmon profile create 미국주식
tmon watchlist add AAPL MSFT --profile 미국주식
tmon watchlist quote --profile 미국주식 --json
tmon watchlist remove MSFT --profile 미국주식
tmon profile list
```

프로필마다 최대 200개 코드를 저장하며 같은 종목을 여러 프로필에 등록할 수 있다. 추가한 순서를 유지하며 영문 코드는 대문자로 통일한다. 이미 등록된 코드의 추가와 없는 코드의 삭제는 목록을 바꾸지 않고 결과에 표시한다. 한도를 넘는 추가 요청은 일부만 저장하지 않고 전체 거부한다.

처음에는 빈 `default` 프로필을 사용한다. `profile create`는 빈 프로필을 만들고 현재 선택을 유지한다. `profile use`는 이후 사용할 프로필을 저장하며 **모든 터미널에 공유**한다. 자동화에서는 `--profile NAME`을 지정해 대상을 고정한다. 이 옵션은 `watchlist` 하위 명령 앞뒤에 사용할 수 있으며 저장된 선택을 바꾸지 않는다. 없는 프로필은 자동 생성하지 않는다.

이름은 한글·영문·숫자·하이픈·밑줄 1~32자로 입력한다. 한글은 NFC 정규화하며 영문 대소문자는 구분한다. `profile rename OLD NEW`로 이름을 바꾸고, `profile delete NAME`으로 비어 있는 비선택 프로필을 삭제한다. 종목이 있으면 `--force`가 필요하다. 현재 선택한 프로필은 먼저 다른 프로필로 전환해야 삭제할 수 있고, `default`는 이름 변경·삭제할 수 없다.

추가·삭제·목록 확인은 인증과 네트워크 없이 동작한다. 추가 시 코드 형식만 확인하므로 이름을 모르면 `tmon search 현대자동차`로 먼저 찾는다. 실제 종목 존재 여부는 현재가 조회에서 확인하며, 누락된 종목을 자동 삭제하지 않는다. 목록이 비어 있으면 `quote`도 네트워크 없이 종료한다.

저장 위치는 다음과 같으며 토큰·종목 목록 캐시와 별개다.

- macOS: `~/Library/Application Support/tmon/watchlist.json`
- Linux: `$XDG_CONFIG_HOME/tmon/watchlist.json` 또는 `~/.config/tmon/watchlist.json`

OS 사용자 단위로 공유하며 다른 디렉터리에서 실행해도 같은 프로필을 사용한다. 여러 프로필과 현재 선택은 v2 파일 하나에 저장하며 파일 한도는 1 MiB다. 폴더 권한 `0700`, 파일 권한 `0600`과 동시 수정 잠금을 적용한다. 파일이 손상되면 오류로 알리고 원본을 덮어쓰지 않는다. 실제 변경이 없는 명령은 파일·갱신 시각을 유지한다.

기존 v1 목록이 있으면 `default`로 표시하고, 첫 실제 변경 때 같은 폴더에 원본 `.bak`을 남긴 뒤 v2로 이전한다. 조회만으로 파일을 생성하거나 이전하지 않는다. 구버전 CLI는 v2를 지원하지 않는다.

묶음 조회는 한 프로필의 현재가만 제공한다. 각 종목의 기술적 분석은 `tmon analyze CODE`를 사용한다. 프로필 복제·병합·메모·묶음 분석은 이번 버전에 포함하지 않는다.

## 일봉 기준과 한계

일봉 확정 시점과 시간외 거래 포함 범위가 공식 명세에 명확하지 않아 **시장 현지 당일 봉은 장 마감 후에도 제외**한다. 시장 캘린더의 이전 영업일 세션이 종료됐는지도 확인한다. 최신 완료 일봉은 다음 현지 날짜부터 분석에 포함되는 보수적 정책이다.

- 국내 거래일은 `Asia/Seoul`, 미국은 `America/New_York`로 해석한다.
- 결과에 사용 기간·봉 수·수정주가·제외한 날짜·확정 판단 정책을 표시한다.
- 오래된 현재가에는 시세 시각을 표시하며 자동으로 실시간 가격이라 부르지 않는다.
- 과거 조회는 최대 3페이지로 제한한다. 확보한 이력이 부족하면 경고한다.
- 가격 변화와 이동평균 위치 등 관측 사실을 제공하며 매수·매도 판단은 출력하지 않는다.

## 인증 캐시

- macOS: `~/Library/Caches/tmon/`
- Linux: `$XDG_CACHE_HOME/tmon/` 또는 `~/.cache/tmon/`

캐시 폴더 권한은 `0700`, 토큰 파일은 `0600`이다. 파일명은 client ID의 SHA-256 해시이며 원문 자격 증명은 저장하지 않는다. 토큰은 이 캐시에만 저장하고 출력하지 않는다. 같은 컴퓨터의 동시 실행은 파일 잠금으로 발급을 조정한다.

새 토큰 발급은 같은 클라이언트의 기존 토큰을 무효화한다. 다른 컴퓨터나 앱이 동일 자격 증명으로 발급하는 경우 이 CLI의 잠금으로 조정할 수 없다. `doctor --remote`도 유효한 캐시가 있으면 이를 재사용하므로 매번 시크릿을 새로 검증하는 명령은 아니다.

캐시를 초기화하려면 실행 중인 `tmon`을 종료하고 위 폴더의 `.json` 토큰 파일을 삭제한다. `.lock` 파일은 동시 실행 조정을 위해 유지한다. 다음 온라인 실행에서 새 토큰을 발급한다.

## 오류와 자동화

JSON 모드는 stdout에 JSON 객체 하나를 출력한다. 일반 모드의 오류·경고는 stderr로 출력한다. HTTP 응답 원문이나 인증 헤더는 출력하지 않는다.

| 종료 코드 | 의미 |
| --- | --- |
| 0 | 성공. 분석 지표 일부가 없으면 JSON `status=partial`일 수 있음 |
| 1 | 내부 오류 |
| 2 | 입력·환경 변수·캐시 설정 오류 |
| 3 | 인증·접근 권한 오류 |
| 4 | 네트워크·호출 제한·공급자 장애 |
| 5 | 데이터 없음·오류, 조회 요청 일부 누락 |
| 130 | 사용자 중단 |

`history`는 요청 봉 수가 부족하면 종료 코드 5를 반환한다. `analyze`는 120봉에 못 미치더라도 지표를 하나 이상 계산할 수 있으면 종료 코드 0과 `status=partial`을 반환한다.

`search`는 0건이나 출력 제한만으로 실패하지 않는다. 장애로 만료 목록을 사용한 경우 `status=partial`, 종료 코드 5를 반환한다.

관심종목 파일 형식·권한·동시 수정 잠금 오류는 종료 코드 2다. `watchlist quote`는 일부 종목의 현재가가 누락되면 종료 코드 5와 `status=partial`을 반환한다. 빈 목록은 정상 결과다.

## 검증과 설계

```sh
python3 -m unittest discover -v
```

테스트는 가짜 자격 증명과 임시 디렉터리를 사용한다. 실제 API나 사용자의 토큰 캐시에 접근하지 않는다.

- [기능 설계](docs/design/README.md)
- [CLI 출력·지표 계약](docs/design/cli-spec.md)
- [종목 검색 설계](docs/design/search.md)
- [랭킹 설계](docs/design/rank.md)
- [관심종목 설계](docs/design/watchlist.md)
- [관심종목 프로필 설계](docs/design/watchlist-profiles.md)
- [국내 단기매매 후보 추천 설계](docs/design/recommend.md) — 당일·2~5거래일 후보와 Codex 웹 조사
- [구조·확장 계획](docs/design/architecture-and-roadmap.md)
- [구현 검증 기록](docs/implementation-verification.md)
