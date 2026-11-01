# OpenCode 전수 조사 — 기능 향상 요소

조사 기준: [`anomalyco/opencode`](https://github.com/anomalyco/opencode) `8c750e3` (clone, read-only). 라이선스는 **MIT**([LICENSE-REVIEW](../LICENSE-REVIEW.md)). 이 문서는 **우리 `claude-cheat`에 적용 가능한 요소**를 추출한 것이며, OpenCode 소스나 문서 문장을 복사하지 않았습니다.

조사 범위: 36개 package 디렉터리, 33개 사용자 문서(`packages/web/src/content/docs`), `specs/v2` 설계 문서, package README와 루트 문서. 전체 9,266개 파일을 한 줄씩 정독한 것은 아니고, **기능 인벤토리와 성능/관측 계약을 중심으로** 조사했습니다.

## 1. OpenCode의 기능 영역

| 영역 | 핵심 기능 | 우리 제품과의 관계 |
| --- | --- | --- |
| Agents | `build`, `plan`, `general`, `explore`, `compaction`, `title`, `summary`; primary/subagent; Tab 전환; `@` 호출; custom agent | **개념 참고**: 라우팅 프로파일(External-only/Plan-safe/Local-only)로 응용 가능 |
| Permissions | action별 `allow`/`ask`/`deny`, auto 모드, wildcard·home 확장·외부 디렉터리, per-agent override | **개념 참고**: 라우팅 정책·비용 차단·위험 모델 차단에 응용 |
| Server | `opencode serve`, OpenAPI `/doc`, Basic auth, SSE `/global/event`, session/message/command/shell/file/VCS/provider API | **직접 적용**: `/api/health`, SSE 이벤트 피드, OpenAPI/SDK |
| Event stream | 하나의 인코딩 feed + 연결별 bounded queue(4,096), overflow 시 해당 연결만 종료, `server.connected`·15초 heartbeat, 연결 없으면 직렬화 생략 | **직접 적용**: 대시보드 실시간 push로 폴링 대체 |
| Providers/Models | 75+ provider, models.dev, local model, variant, provider policy(allow/deny wildcard, last-match-wins, Console 최종 권한) | **직접 적용**: provider/model 정책, 모델 capability·비용 카탈로그 |
| Plugins | local/npm plugin, load order, event hook(command/file/tool/permission/session) | **개념 참고**: 라우팅 전후 hook, 알림 연동 |
| Custom tools | TS/JS 정의 + Python 등 subprocess 실행; arguments/context/permission | **제한적**: proxy에는 tool 실행 seam이 없음 |
| Skills/Commands/Rules | `.opencode/skills/SKILL.md` on-demand, markdown/JSON command, `AGENTS.md` rules, Claude Code 호환 | **참고**: 프로젝트 컨텍스트·운영 명령 문서화 |
| LSP/Formatters | 언어 서버 진단, 파일 편집 후 formatter | **해당 없음** |
| TUI/Desktop/Web/IDE/ACP | 터미널 UI, Electron desktop, browser UI, IDE extension, ACP | **일부 참고**: 원격 모니터링 UI·IDE 연동 |
| Stats/Console | 1분 browser/5분 shared cache, HIT/MISS, revalidate, Markdown/JSON content negotiation | **직접 적용**: 대시보드 통계 export·캐시 헤더(ETag 이미 적용) |
| SDK/HTTP recorder | in-process SDK(no listener), typed client, HTTP/WebSocket cassette replay | **직접 적용**: 자동화 SDK, provider 통합 테스트 |
| Perf harness | test-suite benchmark + hypothesis loop + profiling | **참고**: 프록시/대시보드 벤치마크 방법론 |

## 2. 우선 적용 후보

### P0. SSE 모니터링 피드 (`/api/events`)

OpenCode의 event-stream 설계가 가장 직접적으로 유용합니다.

- **하나의 feed에서 프레임을 한 번만 encode**하고 연결별 bounded queue에 immutable frame을 offer.
- 연결별 queue(예: 4,096) 초과 시 **그 연결만** `overflow`로 종료; 다른 연결과 발행은 계속.
- `server.connected` 1회 + 15초 heartbeat, 연결이 없으면 JSON encode 생략.
- 우리 대시보드는 현재 2초 폴링 + ETag/304로 최적화했지만, **변경 시점 push**가 더 정확하고 지연이 낮습니다.
- 구현: `routeDashboard.py`의 `_refresh_loop`가 snapshot revision을 만들고, `EventFeed`가 연결 큐에 push; JS는 `EventSource` + polling fallback(ETag 유지).

**기대 효과:** 유휴 시 트래픽 0, 이벤트 지연 2초→즉시, 느린 탭이 다른 탭/발행을 막지 않음.

### P0. Provider/Model 정책 엔진

OpenCode `experimental.policies`의 단순하지만 강력한 모델을 라우팅에 이식합니다.

- `action`: `provider.use`, `model.use`, `route.use`(예: plan/external/local)
- `resource`: provider/model slug, wildcard(`company-*`)
- `effect`: `allow`/`deny`, **마지막 매칭 우선**, config 우선순위(user > project > managed)
- 정책은 endpoint/credential 설정과 분리; 정책이 allow여도 자격 증명이 없으면 사용 불가.
- 대시보드 설정 페이지에 정책 편집·검증·현재 적용 결과를 표시.

**기대 효과:** 비용 높은 모델/실수로 인한 provider 사용 차단, 조직/프로젝트별 정책 중앙화.

### P1. `/api/health` + 운영 상태 요약

OpenCode `/global/health`와 server status를 참고해 다음을 한 번에 제공합니다.

- `{ healthy, version, source_commit, uptime, proxy: {hash, queue, loop}, dashboard: {age, cache}, sessions: n }`
- 대시보드·스크립트·모니터링이 같은 판정을 사용; 현재는 여러 API를 조합해야 함.
- 준비/지연/오류를 구조화된 code로 반환.

### P1. Stats export와 content negotiation

OpenCode stats의 cache·content negotiation을 우리 `/api/usage-stats`에 적용합니다.

- `format=json|csv|md`(또는 `Accept`), 짧은 TTL + ETag/304, `Vary: Accept`.
- 대시보드의 사용량/경로 요약을 Markdown/CSV로 내려받기.
- OpenCode stats의 **1분 browser / 5분 shared** 정책처럼 export 응답의 재검증 정책을 명시.

### P1. HTTP recorder/replay로 provider 통합 테스트 고정

OpenCode `packages/http-recorder`는 실제 HTTP/WebSocket 트래픽을 cassette로 기록·재생합니다.

- OpenRouter/Local의 실제 오류·지연·SSE shape를 익명화 fixture로 저장.
- 현재 단위 테스트의 fake fetch보다 **실제 wire shape 회귀**를 잡기 쉬움.
- 시크릿은 redaction하고 cassette는 Git에 넣지 않는 정책 필요.

### P2. 모델 카탈로그와 capability 검증

- OpenRouter `/api/v1/models`와 Local `/v1/models`에서 context length·tool/vision/reasoning·가격을 수집.
- 세션/모델 선택 시 **tools·streaming 지원 여부를 사전 경고**하고, 현재 proxy의 검증 결과와 연결.
- 모델 단가·캐시 단가를 대시보드 비용 예측에 사용.

### P2. 세션 프로젝트 액션

- 세션별 모니터링의 프로젝트·브랜치·cwd를 **복사/폴더 열기** 액션으로 연결.
- OpenCode `/vcs`처럼 branch·변경 상태를 읽기 전용으로 표시(가능한 경우).
- 경로는 로컬 전용이며 외부 API에 노출하지 않음.

### P2. 라우팅 plugin hook seam

- OpenCode plugin event hook을 참고해 `before_route`/`after_response`/`on_alert` 같은 **로컬 hook**을 제공.
- hook은 기본 비활성, 타임아웃·실패 시 기존 라우팅을 막지 않음(정책 실패는 deny).
- 사용자별 커스텀 정책을 코드 수정 없이 추가.

### P3. 테스트 성능 하네스

- OpenCode `perf/test-suite.md`처럼 “가설 → 변경 → before/after → keep/revert” 표를 운영.
- 우리 벤치마크(`tests/bench_proxy.mjs`, `bench_dashboard.py`)를 연결해 변경마다 **프록시 오버헤드·스냅샷·세션 발견**을 같은 기준으로 측정.

## 3. 적용하지 않을 영역

LSP·formatter·custom tool 실행·TUI·IDE/ACP·desktop·mDNS·WSL·provider OAuth·tool permission은 우리 proxy/launcher의 책임이 아니므로 제품 코드에 넣지 않습니다. Agent Team/Subagents는 별도 런타임(DSH) 기능이며 이 저장소의 라우터 기능과 분리합니다.

## 4. 이미 반영된 OpenCode/DSH 참고 요소

- **ETag/304 + `x-dashboard-cache: HIT/MISS`** 조건부 스냅샷(`aa928b3`)
- **비동기 배치 라우트 로그**와 writer 큐/드롭 지표(`2794bd0`, `4d200ee`)
- **GC·동기 구간 계측**(`blocking`·`gc`)과 프로세스 카드 표시(`1301625`)
- 서버 listen 시 histogram reset으로 부팅 지연 제외(`522d6ae`)

## 5. 라이선스 준수

OpenCode는 **MIT**입니다. 지금까지의 참고는 개념 수준이며 코드 복사가 없으므로 별도 고지를 추가하지 않았습니다. SSE feed·정책 엔진 등을 구현할 때도 **독자 코드**로 작성하고, 코드를 실질적으로 복사·번역하는 경우 MIT 저작권·허가 고지를 `LICENSE`/`THIRD_PARTY_NOTICES`에 추가합니다.

## 6. 권장 순서

1. P0 SSE `/api/events` + `/api/health` (관측 성능·지연 개선)
2. P0 provider/model 정책 엔진 (비용·보안)
3. P1 stats export/content negotiation
4. P1 HTTP recorder/replay
5. P2 모델 카탈로그·세션 프로젝트 액션·hook seam
