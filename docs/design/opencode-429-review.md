# OpenCode 지정 모델 호출 상태와 429 검토

검토일: 2026-09-08 (Asia/Seoul)
모델: `opencode/muse-spark-1.3-contributor-free`

## 현재 확인 결과

- OpenCode auto의 새 세션에서 짧은 확인 프롬프트로 실행 1회. 도구 사용·파일 조회·제품 코드 수정은 요청하지 않았으며 해당 도구 실행 이벤트도 없음.
- 05:44:39 KST에 HTTP 429, `FreeUsageLimitError`, `Error from provider (Console): Rate limit exceeded. Please try again later.` 응답. 프로세스 종료 코드 1. 정상 모델 응답 없음.
- 이번 확인도 실패했으므로 구현 작업은 재개하지 않음. 여러 HTTP 전송을 할 수 있는 CLI 내부 재시도 횟수는 이 이벤트만으로 확정하지 않음.

## 기존 오류 본문 대조

| KST 시각 | 실행 | 응답 본문 오류 유형 | 관측 |
| --- | --- | --- | --- |
| 02:43:33 | pr2-review | rate_limit_error / rate_limit_exceeded | Console이 상위 제공자 호출 제한을 전달 |
| 02:51:19 | pr2-retry1 | FreeUsageLimitError | 재시도 중 일부 작업 후 제한 |
| 02:55:59 | pr2-retry2 | FreeUsageLimitError | 성공한 모델 단계 없음 |
| 03:01:42 | pr2-finish | FreeUsageLimitError | 짧은 새 세션도 실패 |
| 05:44:39 | health check | FreeUsageLimitError | 현재의 짧은 새 세션도 실패 |

각 오류는 HTTP 429 및 재시도 가능 표시를 포함한다. 클라이언트가 받은 응답에는 `Retry-After`, 남은 한도·리셋 시각 헤더가 없었다. 이 시각들 사이에 계속 제한됐는지는 연속 관측하지 않았다.

## 원인 판단

1. **확인됨:** 모델 제공 경로가 사용량/호출 제한을 반환했다. 뒤쪽 오류는 무료 이용 제한 계열인 `FreeUsageLimitError`로 구체화할 수 있다. 토스 API나 저장소 테스트 실패, 자동 승인 거부가 발생시킨 429가 아니다.
2. **원인 후보:** 무료 이용량 누적 또는 무료 모델 제공 경로의 제한. 현재 공개 OpenCode 코드에는 IP 기반 일일 요청 제한과 키 기반 분당 요청 제한이 따로 존재한다. 그러나 이번 응답은 상위 Console 제공 경로에서 전달됐으므로 공개 코드만으로 실제 적용 범위나 한도를 확정할 수 없다.
3. **기여 가능 요인:** 자동 구현의 도구 왕복마다 모델 요청이 누적됐다. pr2-review는 성공 단계 23개, 다음 재시도는 22개였고, 각각 60초 구간에서 최대 8개·10개의 단계 완료가 관측됐다. 이는 전송 시도 수/RPM 측정값 자체가 아니며 내부 재시도는 별도다. 불필요한 반복 조회와 짧은 단계가 호출 부담을 높였을 가능성이 있다.
4. **확정할 수 없음:** 개인 계정/IP 한도인지 공유 제공자 한도인지, 일일/분당/토큰 한도인지, 언제 해제되는지. 최신 무료 모델 정책과 실제 백엔드 계측 없이는 결정할 수 없다. 특정 시각 자동 해제를 약속하지 않는다.
5. **긴 문맥만으로 설명 불가:** 오류 직전 성공 단계의 토큰 총계는 약 8.2만/10.9만이며 대부분 캐시 읽기였다. 그러나 대화가 없는 새 세션도 실패했다. 문맥 증가가 부담 요인일 수 있어도 직접 원인으로 단정하거나 이 수치를 청구량/제한 소비량으로 환산하면 안 된다.

## 공개 코드에서 확인한 해석상의 주의점

- IP 제한 구현은 `FreeUsageLimitError`를 사용하며 일일 요청 수를 센다. 키 제한 구현은 `RateLimitError`를 사용한다. 이는 가능한 제한 구조의 근거이지 이번 사용자에게 동일한 경로가 적용됐다는 증거는 아니다.
- 게이트웨이의 상위 제공자 응답 처리에는 일부 응답 헤더만 전달하는 경로가 있다. 따라서 클라이언트에 `Retry-After`가 없었다고 상위 제공자도 이를 보내지 않았다고 단정할 수 없다.
- 공식 Zen 문서는 해당 모델을 Free로 표시하지만 이 모델의 실제 수치 한도와 리셋 조건은 확인한 문서에 명시돼 있지 않다.

출처: [공식 Zen 문서](https://opencode.ai/docs/zen), [IP 제한 구현](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/util/ipRateLimiter.ts), [키 제한 구현](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/util/keyRateLimiter.ts), [제공자 오류·응답 헤더 처리](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/util/handler.ts). 현재 공개 dev 코드이며 당시 배포 버전과의 동일성은 확인하지 않았다.

## 후속 운영 방향

호출이 회복되면 짧은 작업 단위·파일 읽기 중복 제거·연속 실패 시 중단과 충분한 대기 간격을 적용한다. 반복 429 상태에서 즉시 재시도를 쌓지 않는다. 계정 콘솔의 실제 사용량/제한 정보가 있으면 오류 시각과 대조하되, 다른 모델이나 네트워크/계정으로 우회하지 않는다. 이번 확인에서는 설정 변경·자동 재시도 예약·코드 구현을 수행하지 않았다.
