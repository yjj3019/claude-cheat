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
