# 정기 주식 시황·추천 알림

## 설정 예시

Hermes Cron으로 한국시간(`Asia/Seoul`) 평일에 `tmon brief`와 `tmon recommend`를 실행하고 결과를 원하는 알림 채널에 전달하는 예시다. 아래 일정과 작업명은 예시이며, 실제 채널·작업 ID·운영 설정은 저장소 밖에서 관리한다. 시장 전반을 조회하려면 관심종목 프로필을 지정하지 않는다.

| 한국시간 | 알림 | 실행 |
|---|---|---|
| 08:30 | 장전 브리핑 | `tmon brief --session premarket` |
| 09:40 | 장중 브리핑 + 당일매매 추천 | `tmon brief --session intraday` + `tmon recommend --horizon day` |
| 11:00 | 장중 브리핑 + 당일매매 추천 | `tmon brief --session intraday` + `tmon recommend --horizon day` |
| 14:00 | 장중 브리핑 + 스윙 추천 | `tmon brief --session intraday` + `tmon recommend --horizon swing` |
| 15:40 | 장마감 브리핑 | `tmon brief --session close` |

이 예시의 기본 추천 출력은 최대 3개이며, 후보가 반환될 때 선정 근거·진입 범위·무효화 가격·목표 구간·시세 기준 시각·유효 시각을 표시한다. 후보가 없거나 데이터가 부분적으로 실패하면 실제 상태와 원인을 표시한다.

## 예약에 적용할 휴장일·중복·출처 정책

아래는 예약 프롬프트와 전달 계층에 설정할 정책이다. 이 문서를 추가하거나 CLI를 실행하는 것만으로 예약·전달·휴장일 생략이 자동 설정되지는 않는다.

- `tmon brief`가 시장 캘린더를 해석한 `meta.context.phase`가 `closed`이면 예약에서 알림 전달을 생략하도록 한다. 평일 일정으로 예약해 주말 실행을 제외한다.
- 캘린더를 확인하지 못한 경우에는 휴장으로 단정하지 않고 조회 실패를 표시한다.
- 뉴스와 일정은 확인된 출처 URL·게시 시각만 사용한다. 조사 실패·오래된 시세·오래된 추천은 명시한다.
- `tmon brief`의 실행 잠금과 이전 출처 비교를 사용하며, 예약 프롬프트도 동일 결과·출처를 반복하지 않도록 한다.
- 추천은 투자 조언이나 주문이 아니며, `expiresAt`이 지난 후보는 시세를 재확인해야 한다.

## 일시정지·재개·삭제

예약 목록을 확인한다.

```sh
hermes cron list
hermes cron doctor
```

이 문서의 작업명 또는 `job_id`를 사용한다.

```sh
# 개별 작업
hermes cron pause <job_id_or_name>
hermes cron resume <job_id_or_name>

# 전체 5건을 일시정지하려면 각 작업에 반복
hermes cron pause 주식알림-장전
hermes cron pause 주식알림-09시40
hermes cron pause 주식알림-11시
hermes cron pause 주식알림-14시
hermes cron pause 주식알림-장마감

# 삭제는 복구되지 않으므로 목록 확인 후 실행
hermes cron remove <job_id_or_name>
```

평일 반복 일정으로 설정한 작업을 재개한 뒤에는 표시된 다음 실행 시각을 확인한다. 수동 점검은 다음처럼 실행할 수 있지만, 휴장일이면 프롬프트 정책에 따라 알림이 생략된다.

```sh
hermes cron run <job_id_or_name>
```

## 운영 한계

`tmon brief --research auto --timeout 300`으로 웹 조사까지 포함해 최대 300초를 허용한다. 환경·뉴스량에 따라 시간이 걸릴 수 있으며, 조사에 실패하면 확인된 시장 데이터만 `partial`로 전달하고 확인되지 않은 뉴스는 넣지 않는다. 토스 API 장애나 오래된 시세도 결과에 경고로 남긴다. 자동 주문·계좌 조회·주문 감시는 하지 않는다.

문서의 관리 명령은 Hermes CLI 도움말과 작업 ID/이름 해석 코드를 확인했다. 실제 예약 생성·변경·알림 발송은 이 문서 검토 과정에서 수행하지 않았다.
