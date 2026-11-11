# 대시보드 메뉴 감사 — 구조·완성도·정확성 (2026-11-01)

대상: 대시보드(`/`), 설정(`/settings`), 사용량(`/usage`), 블랙박스(`/blackbox`).
방법: `routeDashboard.py`의 페이지 HTML·JS 번들을 API 계약과 대조하고, 실행 중인 대시보드(포트 8787)의 실제 응답과 브라우저 렌더 결과(jsdom 스텁)를 검증한 뒤, 확인된 오표기를 수정하고 회귀 테스트를 추가했다. 비밀값은 읽거나 기록하지 않았다.

## 요약

| 메뉴 | 구조 | 완성도(모니터링 관점) | 잘못된 내용 |
| --- | --- | --- | --- |
| 대시보드 | 7개 카드 + 접이식 진단/활동 + 도움말 | Local·OpenRouter·CLI 동일 지표 | 6건 수정 |
| 설정 | 서비스 선택 + 3개 서비스 카드 + 안내 | 필드 3/6/4개 모두 API와 일치 | 3건 수정 |
| 사용량 | 기간 선택 + 차트 + 실행기 표 | 기간·버킷·집계 모두 원장과 일치 | 없음 |
| 블랙박스 | 상태 + 시작/표시/종료 + 상세 옵션 | 페이로드 8개 필드 중 6개만 표시 | 1건 수정 |

## 1. 대시보드

구조: 한눈에 보기(상태·모델·요청·실패율·속도·p95) → 요청 흐름(관측 토큰만) → 상태/처리량/크레딧/경로 비율 → Plan 우회 사유 → 스냅샷·이벤트 → 보조 지표(접이식) → 사용량·활동(접이식) → 도움말. 모든 카드가 동일한 `/api/snapshot` 응답을 쓰고, 프록시 카운터는 세션 시작 시각 기준이다.

완성도: 세 연결점(Local·OpenRouter·CLI)이 같은 실패율·지연·토큰 흐름·회로 차단 계산을 공유한다. 실패율은 `failed - aborted`로 계산해 취소를 제외하고, 시계열의 실패율도 `failed/(ok+failed)`로 같은 기준을 쓴다. 사용량 미보고 값은 0이 아니라 `미보고`/`—`로 표시된다.

확인 후 수정한 잘못된 내용:
1. 헤더 상태 문구가 `external`일 때 항상 "외부 서비스 사용 중"이었다 → 실제 제공자(Local/OpenRouter/CLI)를 이미 아는 경우에도 OpenRouter로 읽힐 수 있어 "외부 모델 사용 중"으로 바꾸고, 제공자별 문구(`routingStateText`)와 일치시켰다. (네 페이지 공통 번들)
2. 경로 비율 카드의 aria-label·범례가 "외부 서비스"였다 → "외부 모델". 지표 행 이름(`외부 p95`, `외부 / Plan 경로`, `외부 초기 진입`, `외부 실제 시도`)도 "외부 모델"로 통일했다.
3. 실패율 타일 설명이 "완료된 성공·실패 요청 기준"이라 취소 제외를 알 수 없었다 → "완료 응답 기준 · 취소 제외"로 수정(카드 행 라벨과 동일).
4. 연결점별 결정 값: `routing.providers`가 비면 "Local 0건 · OpenRouter 0건 · CLI 0건 · Plan 0건"을 실제 0처럼 출력했다 → 이벤트가 없으면 "최근 60분 이벤트 없음"을 표시.
5. 도움말 상태 표의 `external`이 "OpenRouter 사용 중"이었다 → "외부 모델 사용 중 (Local·OpenRouter·CLI)".
6. 도움말 API 목록이 6개뿐이라 UI가 실제로 쓰는 `/api/models`·`/api/cli`·`/api/usage-stats`·`/api/blackbox`·`POST /api/config`·`/api/test`·`/api/config-reset`이 빠져 있었다(`POST /api/default-provider` 선택지 설명도 `sonnet` 누락) → 13개 엔드포인트로 보완하고 "두 제공자" → "세 서비스"로 수정.

남은 사항(의도/한계): 요약 카드의 "최근 외부 요청 기준", 흐름 카드의 "성공한 외부 응답"처럼 카드 문맥에서 "외부 = Plan이 아닌 경로"임이 분명한 문구는 그대로 두었다. Plan 우회 사유의 `native_feature:plan_model` 안내문 "Opus·Haiku 등은 설계상 Plan으로 갑니다"는 실행기가 Haiku 모델명을 Plan으로 보내는 현재 동작과 일치한다.

## 2. 설정

구조: 사용할 서비스 선택 → 선택한 서비스 카드(상태·필드·연결 검사·저장/삭제) → CLI 카드(감지·모델 목록·검사) → 기본 실행 서비스 → 저장 안내 → 세션 영향 → 고급(접이식). 모든 저장은 새 세션부터 적용된다는 안내가 카드와 하단에 중복되지 않게 배치돼 있다.

완성도: `/api/config` 응답과 대조해 OpenRouter 3개(`OPENROUTER_API_KEY`·`OPENROUTER_MODEL`·`OPENROUTER_UPSTREAM_PROVIDER`), Local 6개, CLI 4개 필드가 모두 바인딩됨을 확인했다. 비밀값은 `preview`(예: `sk-or-…ccd0`)만 표시되고 저장 후 재조회해도 원문이 브라우저로 오지 않는다. 기본 실행 서비스 선택지는 `auto`·`sonnet`·3개 제공자로 백엔드 허용 목록과 일치한다.

확인 후 수정한 잘못된 내용:
1. `ERROR_TEXT`에 `rate_limited`가 없어 저장 요청이 429로 거절되면 "알 수 없는 오류"로 보였다 → "저장 요청이 많습니다. 잠시 후 다시 시도하세요." 추가.
2. `TEST_TEXT`에 CLI 백엔드의 실제 응답 코드가 대부분 없었다. 특히 Grok에서 가장 흔한 `backend_version_unverified`가 원문 코드로 노출됐다 → `backend_*`·`cli_*`·취소·프로토콜·도구·용량 계열 21개 코드를 한국어 문구로 추가.
3. CLI 종류 선택지에 `grok`이 감지되면 선택 가능하게 보였지만 `VERIFIED_VERSIONS["grok"] = set()`이라 실제 실행은 항상 거부된다(카드 안내문은 이미 "사용할 수 없습니다"라고 표시). 선택지 자체에 "grok (미검증 · 실행 불가)"를 붙여 안내문과 일치시켰다.

남은 사항: CLI 설정 초기화 버튼은 없다. 백엔드 `/api/config-reset`이 `cli`를 `invalid_reset`으로 거부하므로 UI 미노출이 계약과 일치한다(문서화만 필요). 같은 이름의 `loadConfig()`가 공유 번들에 두 번 정의되어 앞의 정의(토큰 없음)가 뒤 정의에 의해 덮이는 문제는 2026-11-11 확인 시점에 이미 사라져 `loadConfig()` 정의가 단일하며(`routeDashboard.py`의 `loadConfig()` · `loadConfigHistory()` 구분), 해결됨으로 종결했다.

## 3. 사용량

구조: 조회 단위(일 31일·주 12주·월 13개월) → 새로고침 → 입력/출력 토큰 추이 → 실행기(제공자)별 표 → 수집 한계 안내. 표는 `bucket`·`provider`·`requests`·`ok`·`failed`·`aborted`·토큰/샘플·평균/최대 지연·첫/마지막 시각을 표시한다.

완성도·정확성: `/api/usage-stats` 응답과 JS 필드가 일치하고, 주 버킷은 월요일 시작(`week_start=monday`), 날짜는 시스템 시간대(`timezone=system_local`)다. 실패 열은 상태가 `failed`인 요청만 세므로 취소(`aborted`)가 섞이지 않는다. 토큰 미보고는 0으로 그리지 않고 "미보고"로 표시하며, 차트는 샘플이 없는 구간을 선으로 잇지 않는다. 이번 감사에서 잘못된 문구·수치는 발견되지 않았다.

## 4. 블랙박스

구조: 현재 상태 한 줄 → 시작(진단/상세) · 문제 표시 · 종료 → 상세 기록 옵션 체크박스 → 저장 범위 안내. 대화 내용·도구 출력은 페이지에 노출되지 않는다.

완성도: `/api/blackbox`는 `active`·`mode`·`session_id`·`started_at`·`incidents`·`details_available`·`detail_status`·`discarded`를 준다. 이 중 `started_at`과 `discarded`가 화면에 없어 "언제 시작했는지", "손실된 기록이 있는지"를 알 수 없었다 → 상태 문구에 "· 시작 …"과 (0보다 클 때) "· 폐기 N건"을 추가했다. `session_id`는 개인정보/노출 최소화를 위해 계속 표시하지 않는다.

## 공통 점검

- 네 페이지 모두 같은 내비게이션(4개)과 현재 페이지 `aria-current="page"`를 사용한다.
- 페이지 HTML·CSS·JS는 인증 없이 제공되고, 데이터 API(`/api/config`, `/api/models`, `/api/cli`, `/api/usage-stats`, `/api/blackbox`, `/api/default-provider`, `/api/test`, `/api/config-reset`)는 `x-dashboard-token`을 요구한다. `/api/status`·`/api/diagnostics`·`/api/snapshot`은 읽기 전용 공개다.
- 헤더가 없거나 토큰이 틀리면 403 `token_required`, POST 반복은 429 `rate_limited`, 입력 오류는 400 `invalid_*`로 응답함을 실제 요청으로 확인했다.
- 렌더 결과에 중복 DOM id가 없다(대시보드의 `chart-details`는 CLI 토큰 카드와 타임라인 카드 중 하나만 렌더되어 상호배타).
- 사소한 미비: 파비콘·본문 건너뛰기 링크 없음(접근성 개선 후보, 기능 영향 없음).

## 변경 파일

- `routeDashboard.py`: `HELP_SECTION`(상태·엔드포인트) + 공유 번들/대시보드/설정/블랙박스 JS 패치 추가.
- `tests/test_route_dashboard.py`: 도움말 커버리지·연결점 빈 상태·번들 문구 계약 테스트 2개 추가.
- `tests/dashboard_dom.test.mjs`: 바뀐 범례/지표 라벨 기대값 갱신, 블랙박스 시작·폐기 표시, 빈 연결점 문구, 설정 오류/검사 문구 테스트 추가.

검증: `python -m unittest discover -s tests -p 'test_*.py'` 661개 통과(11 skip), `node --test tests/*.test.mjs` 700개 통과, 대시보드 DOM 테스트 84개, 대시보드 전용 Python 테스트 237개 통과. 대시보드를 재시작(PID 159540, 포트 8787)한 뒤 `/api/status`·`/api/snapshot`·`/api/config`·`/api/models`·`/api/cli`·`/api/usage-stats?period=day|week|month`·`/api/blackbox`가 200, 토큰 없는 요청이 403임을 확인했고, 제공 페이지에서 새 문구("외부 모델 사용 중", "grok (미검증 · 실행 불가)", "backend_version_unverified", "최근 60분 이벤트 없음")와 `/api/snapshot`의 13개 도움말 엔드포인트를 확인했다. `/api/status.ok`가 `false`인 것은 사용자가 실행 중인 프록시 세션이 이전 코드라 `proxy_stale` 경고가 있기 때문이며(대시보드 자체는 정상), 기존 세션은 건드리지 않았다.
