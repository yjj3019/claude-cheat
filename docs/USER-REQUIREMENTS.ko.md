# 사용자 요구사항 원장

이 문서는 사용자가 명시적으로 요구한 동작을 기록하는 **원장(ledger)** 입니다. 아래 요구사항을 바꾸려면 사용자의 명시적 승인이 필요하며, 코드·테스트·문서가 이 원장과 어긋나면 **그 변경을 되돌리는 것을 원칙**으로 합니다.

각 항목에는 수용 기준과 회귀 감시 테스트를 함께 적습니다.

## R1. 옵션 없이 지정 모델(OpenRouter)로 라우팅

**요구**: `OpenClaude`를 옵션 없이 실행해 저장된 기본 서비스가 OpenRouter이면, **별도의 `-OpenRouterCompat` 없이** 설정한 OpenRouter 모델로 라우팅되어야 합니다. Plan은 실험적 기능·미지원 요청처럼 외부로 보낼 수 없는 경우에만 폴백으로 사용합니다.

**수용 기준**

- 일반 OpenRouter 세션의 자식 환경에 `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`과 `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1`이 설정된다.
- OpenRouter를 대체 provider로 쓰는 dual 세션도 같은 설정을 사용한다.
- `-OpenRouterCompat`은 "Plan 폴백까지 완전히 차단"하는 추가 옵션으로 남고, 기본 라우팅의 전제 조건이 아니다.
- 대시보드는 외부 서비스가 설정됐는데 이 세션의 외부 시도가 0이고 전부 Plan이면 `session_plan_only` 경고와 재시작 안내를 표시한다.

**회귀 감시**

- `tests/test_user_requirements.py` — R1 전용 테스트(일반·dual·compat)
- `tests/test_openrouter_config.py::test_openrouter_sessions_pin_the_provider_stable_protocol_by_default`
- `tests/test_dual_provider.py::test_ordinary_dual_session_pins_the_stable_protocol_for_an_openrouter_alternate`
- `openrouterAdvisor.py`의 `routed_session` 조건부 블록(회귀 방지 주석 포함)

**회귀 이력**

| 커밋 | 내용 |
| --- | --- |
| `15298f0` (2026-10-24) | 일반 OpenRouter 세션에서 실험 beta·advisor 도구를 끄고 안정 프로토콜로 시작 → 옵션 없이 외부 라우팅 |
| `e075542` (2026-10-28) | CLI 백엔드 통합 중 조건이 `openrouter_compat`로 좁혀져 일반 세션의 모든 요청이 Plan으로 우회 |
| `0e2143b` (2026-11-01) | 기본 동작 복원 + 회귀 방지 테스트 고정 |

**위반 시 조치**: 요구사항을 약화하는 커밋·테스트 변경은 `git revert`로 되돌립니다. 요구 자체를 바꾸려면 사용자의 명시적 승인을 먼저 받습니다.

## 그 밖에 사용자가 요구해 확정된 동작

아래 항목도 임의로 약화하지 않습니다. 상세 동작과 테스트는 연결된 문서를 기준으로 합니다.

| ID | 요구 | 기준 |
| --- | --- | --- |
| R2 | 대시보드 쓰기 토큰은 재시작해도 유지되어 열어 둔 탭이 계속 동작해야 한다 | `.dashboard.token`, `tests/test_route_dashboard.py::TokenPersistenceTests` |
| R3 | CLI 자동 검색으로 감지된 CLI 종류·실행 파일 경로를 채울 수 있어야 한다 | `routeDashboard.py`의 `autoDetectCli`, `tests/dashboard_dom.test.mjs` |
| R4 | 설치 후 어느 폴더에서나 `OpenClaude`·`OpenClaudeDashboard`·`installLauncher`가 동작해야 한다 | `.cmd` 포워더, 사용자 PATH 자동 등록, `tests/test_install_launcher.py` |
| R5 | PowerShell 실행 정책이 서명을 요구하는 터미널에서도 실행되어야 한다 | `.cmd` 진입점(`-ExecutionPolicy Bypass`), README 실행 정책 절 |
| R6 | 문제를 기다리지 않고 먼저 알리는 능동형 모니터링(배지·알림·복구·경고)을 유지해야 한다 | `proactive_alerts`, 탭 제목 배지, `tests/dashboard_dom.test.mjs`·`ProactiveReactionTests` |
| R7 | 프록시 성능·모니터링 개선을 지속적으로 연구하고 기록해야 한다 | `docs/PROXY-PERFORMANCE-MONITORING-RESEARCH.ko.md`, `tests/metrics.test.mjs` |
| R8 | 위 동작은 README에 자세히 기록되어야 한다 | `README.md` 관련 절 |

## R9. 코드 변경은 수동 재시작 없이 자동 반영

**요구**: 커밋·배포 후 실행 중인 대시보드와 프록시가 사용자의 수동 재시작 없이 새 코드를 반영해야 합니다.

**수용 기준**

- 대시보드 소스가 바뀌면 실행 중인 대시보드가 스스로 재시작해 새 페이지/API를 제공한다. 쓰기 토큰은 유지된다.
- 실행 중 프록시가 이전 코드(`proxy_hash` 불일치)이고 진행 중 요청이 없으면, 대시보드가 **같은 포트·토큰으로 자동 takeover**를 요청해 Claude Code 세션을 끊지 않고 새 프록시 코드를 적용한다.
- 자동 takeover는 60초 간격으로만 시도하고, 활성 턴 중이면 대기하며, 모든 시도·실패는 `.auto-update.log`에 기록한다.
- 대시보드가 실행 중이지 않거나 세션 모드가 takeover를 지원하지 않으면 기존처럼 다음 세션 시작 시 적용한다.

**회귀 감시**

- `tests/test_route_dashboard.py::AutoUpdateTests` — 유휴·stale·스로틀·최신 상태 판정과 소스 지문
- `routeDashboard.py`의 `auto_update_action`, `_auto_takeover_loop`, `_auto_reload_loop`

**위반 시 조치**: 자동 반영을 제거하거나 수동 재시작을 기본으로 되돌리는 변경은 되돌립니다.

## R10. 여러 세션을 동시에 실행해도 모니터링이 초기화되지 않음

**요구**: Claude Code 세션을 2개 이상 동시에 실행한 상태에서 그중 하나를 재시작해도, 대시보드는 표시 중이던 다른 세션의 모니터링을 잃지 않아야 합니다.

**수용 기준**

- 대시보드는 `.usage-stats/instances/*/handoff.json`을 스캔해 **살아 있는 모든 세션**을 찾고 `/api/metrics`로 각각의 요청·경로·모델을 읽는다.
- 별도 선택이 없으면 가장 요청이 많은 세션을 표시하고, 표시 중인 세션이 살아 있는 동안 다른 세션이 새로 시작/재시작되어도 표시 세션을 바꾸지 않는다.
- 표시 세션이 사라지면 새로 생긴 빈 세션이 아니라 **남은 세션 중 가장 활발한 세션**으로 전환한다.
- 2개 이상이면 헤더의 **세션 선택 드롭다운**으로 전환할 수 있고, 선택은 `.dashboard.session`에 저장되어 대시보드가 자동 재시작해도 유지된다.
- 재시작된 프록시 자체의 시계열은 새 프록시 기준으로 시작되지만, 다른 세션의 모니터링은 그대로 유지된다.

**회귀 감시**

- `tests/test_route_dashboard.py::MultiSessionTests` — 세션 발견·안정 선택·선택 저장·거부·선택 UI
- `routeDashboard.py`의 `live_sessions`, `_display_session`, `select_live_session`, `/api/select-session`

**위반 시 조치**: 단일 세션만 추적하도록 되돌리는 변경은 되돌립니다.

## R11. 텍스트·결과와 무관하게 대시보드 레이아웃 고정

**요구**: 모델명·상태 문장·토큰 수·인사이트 문구가 바뀌어도 카드 위치와 크기가 흔들리지 않아야 합니다.

**수용 기준**

- 폭 1400px 이상에서 상단 **한눈에 보기**와 **요청 흐름** 카드는 높이가 고정(560px)되고, 내용은 잘림/스크롤로 처리되어 아래 카드가 밀리지 않는다.
- 긴 모델명·상태·세션 요약은 한 줄 말줄임(`text-overflow:ellipsis`), 지표 값은 말줄임, 인사이트 본문은 3줄 클램프로 고정 높이(128px)를 유지한다.
- 좁은 화면(1400px 미만)에서는 가독성을 위해 자동 높이를 허용한다.
- 값이 없거나 길어져도 위 카드의 높이가 바뀌어 차트·진단 카드가 이동하지 않는다.

**회귀 감시**

- `tests/test_route_dashboard.py::PageTests.test_hidden_elements_and_empty_charts_do_not_mislead` — `Stable dashboard geometry`, `height:560px`, `-webkit-line-clamp:3` 존재
- `routeDashboard.py`의 `PAGE_CSS` 고정 지오메트리 블록

**위반 시 조치**: 콘텐츠에 따라 카드 높이가 다시 변하도록 되돌리는 변경은 되돌립니다.

## R12. 세션별 모니터링 (사용량·부하)

**요구**: 여러 세션이 동시에 실행될 때 **각 세션이 얼마나 사용하고 어떤 부하를 만드는지** 한 화면에서 비교할 수 있어야 합니다.

**수용 기준**

- **세션별 모니터링** 표에 각 세션의 서비스·포트, 요청 수와 요청/분, 외부/Plan 경로, 실패/취소, 진행·실행·대기, 외부 p95, 출력 토큰/초, 입력/출력 토큰, RSS·이벤트 루프 p95, 마지막 활동이 표시된다.
- 진행·대기 요청이 있거나 이벤트 루프 p95가 250ms 이상이면 해당 세션에 **부하** 표시가 붙는다.
- 각 행의 **표시** 버튼으로 그 세션을 상세 카드의 기준으로 전환할 수 있고, 표시 중인 세션은 강조된다.
- 모든 값은 각 세션의 프록시가 직접 보고한 값이며, 세션 토큰·자격 증명은 노출하지 않는다.

**회귀 감시**

- `tests/test_route_dashboard.py::MultiSessionTests.test_session_summary_exposes_usage_and_proxy_load`
- `tests/test_route_dashboard.py::MultiSessionTests.test_session_monitor_ui_is_present`
- `routeDashboard.py`의 `_session_summary`, `renderSessionMonitor`

**위반 시 조치**: 세션별 사용량·부하 비교를 제거하거나 단일 세션 요약만 남기는 변경은 되돌립니다.

## R13. 한눈에 보기 개선과 세션 작업(프로젝트) 식별

**요구**: **한눈에 보기**가 현재 상태를 더 직관적으로 보여주고, 세션별 모니터링에서 각 세션이 **어떤 프로젝트/디렉터리/브랜치에서 작업 중인지** 구분되어야 합니다.

**수용 기준**

- **한눈에 보기**는 상태 LED·현재 라우팅·모델, 최근 60분 외부/Plan 비율 막대, 요청·실패율·관측 속도·외부 p95 타일, 표시 세션·활성 세션 수·오늘 토큰/비용/잔액 컨텍스트를 한 카드에 표시한다.
- 새 세션은 실행기가 cwd·프로젝트·저장소·브랜치를 인스턴스 핸드오프에 기록한다.
- 기존 세션(컨텍스트 없는 핸드오프)은 Claude 트랜스크립트의 `cwd`·`gitBranch`와 최근 활동 시각을 매칭해 **추정**으로 표시하고, 추정임을 UI에 표시한다.
- **세션별 모니터링** 표에 프로젝트명·브랜치·cwd(툴팁)와 서비스·포트를 표시하고, 세션 선택 드롭다운도 프로젝트명을 보여준다.
- 트랜스크립트 캐시에 cwd/branch를 저장하며, 캐시 버전을 올려 기존 캐시는 한 번 재스캔한다.

**회귀 감시**

- `tests/test_openrouter_config.py::...test_project_context_reads_the_git_checkout_without_running_git`
- `tests/test_route_dashboard.py::MultiSessionTests.test_inferred_project_uses_the_transcript_cwd_and_branch`
- `tests/test_route_dashboard.py::MultiSessionTests.test_overview_hero_and_project_columns_are_present`
- `routeDashboard.py`의 `project_context`, `_attach_inferred_projects`, `renderOverviewHero`, 트랜스크립트 cwd/gitBranch 파싱

**위반 시 조치**: 프로젝트 식별을 제거하거나 추정/실측 구분 없이 표시하도록 되돌리는 변경은 되돌립니다.

## R14. 프록시 핫패스 쓰기 최소화와 모니터링 분리

**요구**: 대시보드·로그 같은 모니터링 작업이 요청 처리 지연을 키우지 않도록 쓰기 빈도를 줄이고, 프록시 자체 작업이 병목이 되면 계측으로 원인을 확인할 수 있어야 합니다.

**수용 기준**

- 라우트 로그는 **줄마다 개별 동기 append하지 않고 10ms 비동기 배치·상한 큐(4,096줄/2MiB)·드롭 카운터·종료 동기 flush**로 기록한다. 큐가 넘치면 진단 줄만 드롭하고 요청은 계속 처리된다.
- `/api/metrics`에 `routeLog` 지표(written/dropped/errors/queueLines/queuedBytes/writeMsMax/writeMsAvg)가 포함된다.
- 대시보드 프로세스 카드에 **라우트 로그 쓰기 max·큐·드롭**이 표시되어 이벤트 루프 지연 원인을 로그 I/O와 구분할 수 있다.
- 기존 로그 회전, start 마커, 개행 위조 방지, `-Stop`/handover 시 flush 동작이 유지된다.
- 외부 참고(DSH)는 MIT 라이선스이며 **코드를 복사하지 않고 독자 설계**한다. 복사가 발생하면 MIT 고지를 추가한다.

**회귀 감시**

- `tests/logging.test.mjs` — 회전·start 마커·개행 위조·종료 flush
- `tests/metrics.test.mjs` — `routeLog` 지표
- `tests/dashboard_dom.test.mjs` — 프로세스 카드의 로그 쓰기/큐 표시
- `tests/test_route_dashboard.py::PageTests` — `라우트 로그 쓰기 max` UI
- `LICENSE-REVIEW.md` — DSH MIT 참고 검토

**위반 시 조치**: 라우트 로그를 다시 요청 스레드의 동기 per-line 쓰기로 되돌리거나, 모니터링 지표를 제거하는 변경은 되돌립니다.

## R15. 모니터링 조건부 캐시와 게이트웨이 관측 성능

**요구**: 데이터가 바뀌지 않은 폴링은 본문 전송과 DOM 재렌더를 반복하지 않아야 하고, 게이트웨이/프록시 상태를 캐시 적중·지연과 함께 관측할 수 있어야 합니다.

**수용 기준**

- `/api/snapshot`은 변경 가능한 값(시계·샘플링 잡음·pid·원시 RSS/이벤트 루프 버퍼)을 제외한 안정적 내용으로 **ETag**를 만들고, 일치하는 `If-None-Match`에는 **304 Not Modified**를 반환한다.
- 응답에 `x-dashboard-cache: HIT|MISS`를 표시해 캐시 적중을 진단할 수 있다.
- 대시보드 JS는 ETag를 보내고 304를 받으면 기존 `SNAP`을 유지해 **불필요한 전체 렌더를 생략**한다. 새로고침 버튼은 304에서도 안내한다.
- ETag는 캐시에서 빠른 수준으로 계산되어 스냅샷 생성 hot path를 막지 않는다.
- OpenCode(참고)는 MIT이며 코드를 복사하지 않고 캐시·계측 개념만 독자 구현한다. 복사 시 MIT 고지를 추가한다.

**회귀 감시**

- `tests/test_route_dashboard.py::SnapshotCacheTests.test_etag_ignores_clocks_and_sampling_noise`
- `tests/test_route_dashboard.py::SnapshotCacheTests.test_page_polls_with_an_etag_and_handles_304`
- `tests/test_route_dashboard.py::PageTests` — `if-none-match`·304 처리
- `LICENSE-REVIEW.md` — OpenCode MIT 참고 검토

**위반 시 조치**: 조건부 폴링을 제거해 매 주기 전체 본문·DOM을 다시 보내도록 되돌리는 변경은 되돌립니다.

## R16. 이벤트 루프 스파이크 원인 계측

**요구**: 남은 이벤트 루프 지연이 어느 동기 구간에서 발생하는지 추정이 아니라 계측으로 확인할 수 있어야 합니다.

**수용 기준**

- 프록시가 `body_parse`, `diagnostics`(stderr+로그 enqueue), `metrics_serialize`, `metrics_json` 등 동기 구간의 횟수·최대·평균을 기록한다.
- `PerformanceObserver`의 `gc` 엔트리로 GC 횟수·최대·최근 시각을 기록한다.
- `/api/metrics`에 `blocking`(상위 구간)과 `gc`를 노출하고, 프로세스 카드에 `GC n회 · max ... · 동기 블로킹 구간 max`를 표시한다.
- 계측 오버헤드는 요청당 수 마이크로초 수준이며 요청/응답을 변경하지 않는다.
- 서버가 listen을 시작할 때 이벤트 루프 histogram을 reset해 모듈 import·부팅 지연을 요청 지연 통계에서 제외한다.
- `eventLoopSpikes` 시각과 함께 GC·블로킹 상위 구간을 대조해 다음 최적화 대상을 정한다.

**회귀 감시**

- `tests/metrics.test.mjs` — `blocking`·`gc` 형상
- `tests/dashboard_dom.test.mjs` — 프로세스 카드의 GC/블로킹 표시
- `routeDashboard.py`의 프로세스 카드, `claudeRouteProxy.mjs`의 `timeSync`·GC observer

**위반 시 조치**: 원인 계측을 제거해 스파이크 원인을 다시 추정에 의존하도록 되돌리는 변경은 되돌립니다.

## R17. Attention-first 운영 큐

**요구**: 대시보드는 지표 나열보다 “지금 사람이 결정해야 할 것”을 먼저 보여 주고, 확인·미루기·해결 상태를 잃지 않아야 합니다.

**수용 기준**

- `/api/snapshot`의 `attention`은 open/acknowledged/snoozed/resolved 상태와 `counts`를 포함한다.
- 경고가 사라지면 자동으로 `resolved` 이력으로 이동하고, 건강한 상태는 한 줄로 접힌다.
- `POST /api/attention`은 세션 토큰을 요구하며 `ack`·`snooze`(5~1440분)·`resolve`·`reopen`을 지원한다.
- 세션 요약에 `retry_health`(rate_limited/retried, 재시도·시도·회로·Retry-After)가 포함되고 세션 표에 표시된다.
- 속도 제한은 능동 경고(`session_rate_limited`)로 승격되어 주의 큐에 들어간다.
- ETag는 attention의 시계·last_seen 값을 제외해 건강한 상태에서 304를 유지한다.
- Superagent의 attention-first 아이디어를 참고하되 코드를 복사하지 않고 독자 구현한다.

**회귀 감시**

- `tests/test_route_dashboard.py::AttentionQueueTests` — ack/snooze/resolve/reopen/auto-resolve
- `tests/test_route_dashboard.py::MultiSessionTests.test_session_summary_exposes_usage_and_proxy_load` — retry health
- `tests/dashboard_dom.test.mjs` — 주의 큐 액션·건강한 상태
- `LICENSE-REVIEW.md` — Superagent MIT 참고 검토

**위반 시 조치**: 주의 항목의 사람 상태를 저장하지 않거나, 건강한 상태에 큰 카드를 다시 표시하는 변경은 되돌립니다.
