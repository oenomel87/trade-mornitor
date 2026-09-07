# snapshot-v2 최종 구현 검증 기록 — 2026-09-08

대상: 실제 Git worktree `codex/recommend-snapshot-v2`, 기준 HEAD `167bc738abf86a62af929528bccbe181956e5795`.
구현 담당: gpt-5.6-luna / xhigh 서브에이전트. 설계·검토·독립 실행: Codex 주 에이전트.

## 단계별 독립 실행

| 단계 | 저장소 전체 unittest 결과 |
| --- | --- |
| PR2 인수 | 244 tests 통과 |
| PR3 인수 | 295 tests 통과 |
| PR4-A 인수 | 315 tests 통과 |
| PR4-B 및 최종 감사 | **329 tests 통과, unittest 2.647초, exit0** |

모든 단계에서 `python3 -m unittest discover -s tests -t .`를 실제 worktree에서 실행했다. 초기 단계의 수에는 엔진 통합 전 보조 함수 테스트가 포함된다. 각 단계의 `git diff --check`도 통과했다.

## 회귀 근거

| 계약 | 관련 테스트 |
| --- | --- |
| 최초 screened 보존, 관측·호출·오류 분류 | test_recommend_pr0.py |
| 실제 시각·완료 지연·최신성 분리 | test_recommend_pr1.py |
| 고정 지수 구간·캐시·후보 순서 불변성 | test_recommend_pr2.py, test_recommend_index_dates.py |
| 원 신호·600초 수명·현재 가격 재평가 | test_recommend_pr3.py, test_recommend_snapshot_signals.py |
| 개별 관측 TTL·저장 실패 보존·출력 경계 | test_recommend_snapshot_observations.py, test_recommend_pr3.py, test_recommend_final_audit.py |
| 사전 필터 후 상세 한도·집계 | test_recommend_precheck.py, test_recommend_pr4.py |
| 캘린더·일봉 결측·조정 기준 보조 로직 | test_recommend_daily.py, test_recommend_daily_adapter.py, test_recommend_pr4b.py |

## 검증 한계

- 실제 저장소 코드에 가짜 공급자와 진행하는 시계를 주입한 테스트다. 함수 발췌본만의 검증과 구분한다.
- 실제 토스 장중 API 응답, 봉 전달 지연, 기업행사 데이터, 전략 수익성은 검증하지 않았다.
- 거래일 자료를 확보하지 못하면 검증 완료로 표시하지 않는다. 정상 지수 일봉만으로 완전한 캘린더를 대신하지 않는다.
- 수정·비수정 창의 관측 일치는 공급자 전체의 거래량 조정 보증을 뜻하지 않는다. 초기 스크리닝에서 이미 탈락한 후보는 상위 최대 5개 추가 검증으로 복구되지 않는다.
- 신호 이후 가격 경로 감시, 자동 주문/손절, 비용·누적 호가·일봉 저장소·백테스트·breakout-v2 실험은 이번 범위 밖이다.
- 제품 코드의 원본 main 반영·커밋·푸시·배포는 수행하지 않았다.

## 추가 독립 프로브

- 원본과 worktree의 기존 DEFAULTS 값 대조: 변경0. 신규 day.maxSignalAgeSeconds=600, 제품 research model=gpt-5.6-sol 유지.
- SnapshotClient(swing, step=0.01)에서 종목·지수 양쪽의 동일 2026-08-28 일봉을 제거: 검증된 공통 캘린더는 유지되며 daily-gap 1건, exit5. 양쪽이 함께 누락되어도 정상 평가로 통과하지 않음.
- 원본59파일 SHA256 재대조: 이 작업 시작 후 work-log 외 차이 없음.

## 최종 인수와 산출물

- PR0·PR1의 기존 변경을 보존하고 잔여 PR2·PR3·PR4-A·PR4-B를 worktree에서 완료했다. PR 번호는 명세의 작업 구분이며 실제 GitHub PR 생성이나 병합을 뜻하지 않는다.
- 마지막 전체 실행과 `git diff --check`는 Codex 주 에이전트가 독립 실행했다. 원문은 `local-artifacts/opencode-snapshot-v2/luna-final-independent-tests.txt`에 보존한다.
- 통합 중 324개 전체 실행에서 기존 만료 테스트의 2개 기대값 실패를 확인했다. 이미 만료된 신호의 raw 일봉·현재가 조회를 생략하는 새 동작에 맞춰, 599초 유효 및 600·601초 제외/불필요 조회0 검증으로 수정했다. 최종329개에는 저장 정리 실패·CLI 강제 빈 결과 제외 기록·재시도 검증 근거 보존 회귀3개가 포함된다.
- `effectiveConfig`는 엔진·연구 입력과 복사를 분리하고 기존 configHash와 일치시킨다. 최종 재시도는 원 신호와 추가 일봉 검증 근거를 유지한다.
- 실제 장중 데이터와 성과를 확인하지 않았으므로 배포 후 관측은 별도로 남아 있다. 초기 600초 수명을 최적값이나 무영향 수정으로 주장하지 않는다.
- 이번 작업에서는 함수 발췌본만 실행한 별도 검증을 새로 수행하지 않았다. 위 결과는 실제 저장소의 전체 테스트와 실제 Engine/Store/CLI를 사용한 모의 프로브다.

[작업 명세](./recommend-snapshot-v2-work-spec.md) · [작업 로그](./recommend-snapshot-v2-work-log.md)

## 로컬 main 반영 — 2026-09-08

사용자 요청으로 검증본의 추천 기능 관련 파일을 main에 반영했다. 기존 브리핑·알림 관련 미커밋 파일은 별도로 보존하고, 중단된 OpenCode 설정은 반영하지 않았다. main 체크아웃에서 전체 **329 tests 통과(2.654초)**. 로컬 커밋으로 기록하며 원격 푸시·배포는 수행하지 않는다. 앞 절의 미병합 기록은 워크트리 인수 시점의 이력이다.

- 커밋 예정 Git 인덱스만 별도 임시 디렉터리에 추출한 전체 테스트: **327 tests 통과(2.693초), exit0**. 작업 체크아웃329개와의 차이는 별도로 보존한 미커밋 브리핑 테스트2개다. 신규 추적 문서의 기존 행끝 공백1건도 정리했다.
