# Agent Teams · 추론(thinking) 활성화 연구

- 일자: 2026-11-12
- 상태: **연구 + 우선순위 1~4 구현 완료, P1/T3 대기** — 권장 우선순위의 문서화·env 노출·검증·가시성 항목은 실장되고 10회 시뮬레이션으로 검증됨(§5). 남은 것은 사용자의 실제 로컬 서버 키가 필요한 P1 live probe와 그 결과에 따른 T3
- 범위: `claudeRouteProxy.mjs`(라우팅), `openrouterAdvisor.py`(런처), `modelQualityProbe.py`(품질 검증), `routeDashboard.py`(대시보드), 테스트 스위트

## 0. 결론 요약

| 기능 | 현재 상태 | 활성화까지 필요한 일 |
|---|---|---|
| **Agent Teams** | 프록시 계층은 **그대로 통과 가능**(표준 API shape, 파일 기반 조정). 실질 제약은 로컬 동시접수(2)·일반 로컬 모드 attempt 타임아웃(30s) | A1(코드 0)으로 즉시 부분 동작. A2(소규모)로 제약 완화, A3로 검증 |
| **추론 (thinking)** | 일반 모드는 **의도적 비활성**(요청 강제 disable + 응답 거부). `--local-compat`/`--openrouter-compat` 호환 모드에서는 **thinking 프로토콜이 이미 종단 간 동작**. 다만 로컬 서버가 Anthropic thinking 블로크를 내는지가 변수 | T1(코드 0)으로 호환 모드 사용. T2(소규모)로 검증/가시성. T3(중규모, 선택)로 로컬 추론 출력 → thinking 블로크 브릿지 |

핵심 통찰: 두 기능 모두 **프록시가 shape을 거부해서 막는 것이 아니라, (1) 로컬 동시접수 한계, (2) thinking의 서명(서명 없음 → 다음 턴 Plan 고정) 안전 설계**로 제한되어 있다. 따라서 "구멍을 파는" 작업이 아니라 **한계 노출 + 호환 모드 문서화 + 검증**이 대부분이다.

---

## 1. 아키텍처 복습: 요청이 어떻게 라우팅되는가

### 1.1 세션 모드 (createProxy 옵션, [claudeRouteProxy.mjs:1153-1214](../claudeRouteProxy.mjs))

| 모드 | CLI 플래그 | Plan 폴백 | thinking 요청 | thinking 응답 |
|---|---|---|---|---|
| 일반 (openrouter/local/cli) | — (기본) | **O** (구독 OAuth) | **강제 `disabled`** ([mjs:2183](../claudeRouteProxy.mjs)) | **거부** (rejectThinking, [mjs:912](../claudeRouteProxy.mjs), [mjs:2326](../claudeRouteProxy.mjs)) |
| Local-only | `--local-mode` | X | 강제 `disabled` | 거부 — `text`/`tool_use`만 허용 ([mjs:2342-2359](../claudeRouteProxy.mjs)) |
| Local-compat | `--local-compat` | X | **그대로 전달** | **허용** — thinking/redacted_thinking 필드(서명 포함)까지 ([mjs:2331-2340](../claudeRouteProxy.mjs)) |
| OpenRouter-compat | `--openrouter-compat` | X | **그대로 전달** | **허용** (동일) |

- `compatProfile = localCompat || openrouterCompat`, `noPlan = localOnly || openrouterCompat` ([mjs:1214](../claudeRouteProxy.mjs))
- 호환 모드는 **Plan 폴백을 비활성화**한다 — 모든 트래픽이 구성된 외부/로컬 모델만 사용해야 한다는 보장 때문.

### 1.2 라우팅 결정 (매 요청마다)

```
nativeDetail = compatProfile ? compatExclusion(body, beta)
              : isPro(model) ? 'plan_model'
              : externalExclusion(body, beta, localOnly)
→ nativeDetail 있으면 (일반 모드) Plan 폴백: api.anthropic.com, 저장된 구독 OAuth
  (planModel: 비-Pro alias는 claude-sonnet-5-5로 매핑, [mjs:2026-2039](../claudeRouteProxy.mjs))
```

- `externalExclusion`이 거부하는 것: 미지 베타, 미지 요청 필드, 미지 콘텐츠 블로크, `thinking.display` 미지 값 등 → **이것이 "native_feature: ..." 오류의 원천** (대시보드 한국어 메시지 [routeDashboard.py:179](../routeDashboard.py))
- **thinking 히스토리 제거**: 모든 외부 요청 사본에서 thinking/redacted_thinking 블로크를 제거 ([mjs:2175-2177](../claudeRouteProxy.mjs), `withoutNativeThinking` [mjs:567-573](../claudeRouteProxy.mjs)). 이유 2가지: ① Plan 모델의 내부 추론을 제3자에게 보내지 않기 ② Plan 폴백 1회 후에도 세션이 라우팅 가능하게 유지.

### 1.3 thinking이 "고정(pinning)"되는 메커니즘 (비활성 설계의 근거)

1. Plan 턴이 **서명된** thinking 블로크를 반환 → Claude Code(클라이언트)가 다음 요청에 그대로 회신
2. 그 요청이 externalExclusion에 걸리면(또는 외부로 보낼 수 없으면) **그 세션의 이후 턴들이 Plan으로 끌려감**
3. 일반 모드는 아예 thinking을 요청하지도·수락하지도 않음으로써 이 루프를 원천 차단
4. (참고) 프록시는 이미 요청 사본에서 thinking을 제거하므로, 호환 모드에서는 제거된 클린 사본만 외부로 goes → pinning 루프가 성립하지 않는다.

### 1.4 로컬 동시접수 (admission)

- `localConcurrency = 2`, `localQueueLimit = 8`, `localQueueWaitMs = 60000` ([mjs:1193-1195](../claudeRouteProxy.mjs), `acquireLocal` [mjs:1284-1307](../claudeRouteProxy.mjs))
- 초과 시 `local_busy` 503 ([mjs:1971](../claudeRouteProxy.mjs), [mjs:2158-2159](../claudeRouteProxy.mjs)) — "Local request queue is full or its wait expired. Plan fallback disabled."
- **env로 조정 가능(A2 이후)**: `ROUTE_LOCAL_CONCURRENCY`(1–64, 기본 2), `ROUTE_LOCAL_QUEUE_LIMIT`(0–256, 기본 8), `ROUTE_LOCAL_QUEUE_WAIT_MS`(0–600000ms, 기본 60000), `ROUTE_LOCAL_ATTEMPT_TIMEOUT_MS`(30s–15min, compat/local-only 기본 120s) — 런처가 올바른 값만 전달.
- attempt 타임아웃: 일반 모드 **30s 침묵 윈도우**(chunk마다 재장전, 300s 총상한) / localOnly·compat **120s 총한도** (env 조정 가능) ([mjs:1156](../claudeRouteProxy.mjs))
- 전역 한계: `maxConcurrentRequests=32`, `maxInflightBytes=64MiB`, `maxConnections=64` — 팀 규모엔 충분.

### 1.5 런처 (openrouterAdvisor.py)

- 호환 모드(compat)에서 사용자 env를 **명시적 허용 목록으로 보존** — `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`, `CLAUDE_CODE_FORK_SUBAGENT`, `CLAUDE_CODE_ENABLE_TASKS`, `CLAUDE_CODE_ENABLE_TODO_TOOLS`, `CLAUDE_CODE_TASK_LIST_ID` 포함 ([openrouterAdvisor.py:832-848](../openrouterAdvisor.py)) → **Agent Teams가 호환 모드에서 동작하도록 의도되어 있음**
- compat에서 모델 alias 고정: `ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-5-5`, `CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-5-5` 등 ([openrouterAdvisor.py:849-851](../openrouterAdvisor.py))
- 일반 OpenRouter/CLI 세션은 `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` ([openrouterAdvisor.py:858-870](../openrouterAdvisor.py)) — **일반 local 세션은 betas 미비활성** (미지 베��� Plan 우회 리스크, §3.3 R4)
- CLI: `--local-mode` / `--local-compat` / `--openrouter-compat` ([openrouterAdvisor.py:1655-1657](../openrouterAdvisor.py))
- 대시보드 capabilities 매트릭스: `tools/vision/thinking/streaming` 기본 `"unknown"` — `tool_roundtrip_evidence`(품질 검증)만 채워짐 ([routeDashboard.py:6544-6547](../routeDashboard.py))

---

## 2. Agent Teams

### 2.1 동작 원리 (공식 문서 대조)

- **구성**: team lead(주 세션) + teammates. 각 teammate = **독립 Claude Code 인스턴스**(독립 컨텍스트). 표시 모드: in-process(기본, 주 터미널 내) / split panes(tmux·iTerm2 필요 — **Windows 미지원**).
- **조정 매개체**: **API가 아니라 파일** — 메일박스 `~/.claude/teams/{team}/inboxes/{agent}.json`, 공유 태스크 `~/.claude/tasks/{team}/`(파일 잠금으로 race 방지). → **에이전트 간 메시징은 프록시 트래픽을 만들지 않는다.**
- **프록시가 보는 것**: N개의 독립 `/v1/messages` 세션(동시 최대 N 턴). 툴은 `Agent`(name 포함 spawn), `SendMessage`, `TaskCreate/TaskGet/TaskList/TaskUpdate` — 모두 **표준 tool 정의/`tool_use` 블로크** shape.
- **spawn 조건**: **인터랙티브 세션만** (`-p` headless/Agent SDK에서는 teammate spawn 불가 — Claude Code 측 제약).
- **teammate 모델**: spawn 프롬프트 명시 > 서브에이전트 정의 `model` > `CLAUDE_CODE_SUBAGENT_MODEL` > lead 모델.
- **공식 문서상 알려진 한계**(우리 구현과 무관하게 존재): in-process teammate는 `/resume`·`/rewind` 미복원, 태스크 상태 지연 가능, shutdown 지연, 세션당 팀 1개, 팀 중첩 불가, in-process teammate의 background 서브에이전트 불가.

### 2.2 현재 구현의 상태 — 무엇이 되고, 무엇이 막혀 있나

**되는 것(코드 불필요):**
1. 프록시 shape 검증: 팀 툴 세트는 표준 `tools`/`tool_use`/`cache_control` shape → `externalExclusion` 통과. 서브에이전트 스모크([tests/native_subagents_smoke.mjs](../tests/native_subagents_smoke.mjs))가 `Agent` 툴·병렬 워커·완료 수집을 프록시 통과로 이미 검증.
2. 런처가 호환 모드에서 AGENT_TEAMS env를 보존 — 인텐션 명확.
3. split-pane teammate는 lead의 자식 프로세스 → `ANTHROPIC_BASE_URL`(프록시)을 승계 → 프록시 경유. in-process teammate는 동일 프로세스이므로 당연.

**막혀 있는 것(제약 목록):**
| # | 제약 | 위치 | 영향 |
|---|---|---|---|
| C1 | `localConcurrency=2` (env 미노출) | [mjs:1193](../claudeRouteProxy.mjs) | 로컬에서 teammate 2명 초과 시 큐 대기(최대 8, 60s) → 초과 시 `local_busy`. teammate는 API 오류 시 lead에 실패 통보(공식 문서) → 동작은 하되 지연/실패 표면화. **A2 이후 env로 완화** |
| C2 | 일반 로컬 세션 attempt 타임아웃 30s | [mjs:1156](../claudeRouteProxy.mjs) | GPU 경쟁 시 30s 턴 실패 가능성(120s인 localOnly/compat은 여유). 팀 사용은 compat 모드와 조합이 유리. **A2 이후 compat/local-only 한도를 env로 상향** |
| C3 | headless(-p) 불가 | Claude Code 측 | 팀 검증·자동화 테스트에 제약(§2.3 A3) |
| C4 | **팀 트래픽 테스트 부재** | [tests/native_subagents_smoke.mjs:43,94](../tests/native_subagents_smoke.mjs)가 `AGENT_TEAMS=0` (테스트 격리 목적, 프록시 결함 아님) | 회귀 안전망 없음 |
| C5 | Windows: split panes 불가 | tmux/iTerm2 의존 | in-process 모드만 — Windows 사용자에게 해당 |
| R4(리스크) | 일반 local 세션의 실험 베타 헤더 | [openrouterAdvisor.py:858-870](../openrouterAdvisor.py)는 OpenRouter/CLI만 비활성 | 향후 Claude Code가 새 베타를 기본 전송하면 `unsupported_beta` → Plan(구독 없으면 403). 버전 의존 리스크 — live 검증 필요 |

### 2.3 활성화 설계안

**A1 — 코드 0 (즉시 가능, 부분 동작)**
- 인터랙티브 세션 + `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`(settings.json `env` 또는 셸) + in-process 모드.
- 로컬: teammate 2명까지 실질 병렬, 초과분은 큐(60s)로 흡수. OpenRouter 일반 모드: Plan 폴백이 항상 뒷받침.
- 문서화(README + 대시보드 힌트)만 추가하면 "사용 가능" 상태.

**A2 — 소규모 코드 (한계 완화)**
1. `claudeRouteProxy.mjs`: `localConcurrency`/`localQueueLimit`/`localQueueWaitMs`를 env로 노출 (기존 `boundedLimit` 클램프 패턴 재사용: `ROUTE_LOCAL_CONCURRENCY`(1..64) 등).
2. 런처: env passthrough + `--local-compat` 팀 권장 조합 문서화.
3. (선택) 일반 로컬 세션 attempt 타임아웃 env(`ROUTE_LOCAL_ATTEMPT_MS`) 또는 120s로 상향.
- 노력: ~0.5일 (코드+테스트).

**A3 — 검증 계획**
1. **시뮬레이션 테스트**(실제 Claude Code 불필요): `createProxy` + fetch fixture로 팀 툴 세트·`cache_control` 포함 N=4 동시 세션 → ① 큐잉 후 전원 완료 ② 포화 시 `local_busy` ③ metrics `routes.external` 정확성. 기존 `tests/claudeRouteProxy.test.mjs` 인프라 재사용.
2. **Live**: 사용자 인터랙티브 세션 + 로컬 모델 + teammate 2~3명 → 메일박스/태스크 파일 생성, 완료, 대시보드 metrics 확인. (headless 불가 → 수동/live 스크립트)
3. 대시보드 capabilities에 `agent_teams: "supported"/"limited"` 행 추가 (T2와 통합 가능).

**리스크**: GPU 메모리(서버 측 큐로 완화 — LM Studio/Ollama가 자체 직렬화), 토큰 비용 선형 증가, 실험 기능 불안정(resume/shutdown), R4 버전 의존 베타.

---

## 3. 추론 (thinking)

### 3.1 현재 동작 정리 (§1.1 표가 소결)

- **오늘 "thinking이 되는" 유일한 경로** = 일반 모드의 **Plan 폴백** — 서명된 thinking을 진짜 Anthropic이 반환(구독 소모). 대시보드 "Plan 우회 중" 상태가 이 경로.
- **호환 모드**(`--local-compat`)는 thinking **프로토콜 전체가 이미 열려 있음**: 요청 `thinking` 그대로 전달, 응답 thinking 블로크(서명·`provider_specific_fields` 포함) 허용, 히스토리 thinking은 모든 외부 사본에서 제거되어 pinning 루프가 성립하지 않음.
- **결정 변수**: 로컬 서버가 Anthropic `thinking` 파라미터를 인식해서 thinking 블로크를 **내느냐** 여부.

### 3.2 로컬 모델 현실 (중요)

- Anthropic `thinking`(extended thinking)은 **Claude 전용 메커니즘**이다. LM Studio·Ollama·llama.cpp·vLLM의 Anthropic 호환 엔드포인트는 대부분 `thinking` 필드를 **무시**하거나 자체 추론과 무관하게 취급.
- 로컬 **추론 모델**(Qwen3-thinking, DeepSeek-R1 계열 등)은 추론을 **일반 텍스트**(또는 OpenAI `reasoning_content` — Anthropic 엔드포인트로는 서버 구현에 따라 노출 유무 다름)로 출력 → 현재 프록시는 `text` 블로크를 그대로 허용([mjs:2347](../claudeRouteProxy.mjs)) → **"추론 내용이 보이는 것"은 이미 지금 동작**.
- 즉, "thinking 패널(별도 reasoning 표시)"을 로컬에서 얻으려면 ① 서버가 thinking 블로크를 내야 하거나 ② **응답 변환 브릿지**가 필요하다.

### 3.3 활성화 설계안

**T1 — 코드 0 (즉시 가능)**
- thinking을 써야 하는 턴: 일반 모드 + 구독 → **자동 Plan 폴백**(현재 동작, 그대로 유지).
- 로컬 thinking 프로토콜: `--local-compat` 세션 사용 — 서버가 thinking 블로크를 내면 Claude Code가 표시. **서버 능력 live 검증 필요** (§3.4 P1).

**T2 — 소규모 코드 (검증·가시성)**
1. `modelQualityProbe.py`: thinking round-trip 단계 추가 — `thinking: {type:'enabled', budget_tokens:N}` 요청 → 응답에 thinking 블로크 존재/shape(서명 유무) 확인 → 결과 JSON에 `thinking_supported: yes|no|partial`.
2. 대시보드 `capabilities.thinking`을 `"unknown"`에서 검증 결과로 갱신 ([routeDashboard.py:6545](../routeDashboard.py)) + 설정 페이지 로컬 섹션에 "thinking: 서버 미지원 — 텍스트로 출력" 안내.
3. 문서: 모드 선택 가이드(팀·thinking → `--local-compat` 권장) — README + 대시보드 힌트.
- 노력: ~0.5~1일 (코드+테스트).

**T3 — 중규모, 선택 (로컬 reasoning → thinking 블로크 브릿지)**
- 호환 모드 한정, 로컬 응답 경로에서 서버 특정 추론 출력을 Anthropic thinking 블로크로 매핑 (예: Ollama `message.thinking` ↔ Anthropic block; 서버별 어댑터).
- **서명 없는 블로크의 안정성**: 프록시가 이미 모든 외부 요청 사본에서 thinking을 제거([mjs:2175-2177](../claudeRouteProxy.mjs)) → 클라이언트가 회신해도 라우팅에 영향 없음 → **로컬 세션은 안정**.
- **필요한 새 가드**: Plan 폴백 바디는 thinking을 보존하는 데([mjs:2021-2022](../claudeRouteProxy.mjs)), **서명 없는** thinking을 Anthropic이 거부(400) → Plan 바디 구성 시 unsigned thinking 제거 필터 + 테스트.
- 리스크: 서버별 어댑터 유지보수, 서명 없는 thinking의 Claude Code 표시 의미론(표시는 되나 "reasoning omitted" 처리 여부) — live 확인 필요. 추론 품질 자체가 모델 의존.
- 노력: 2~3일 (어댑터 1종 + 가드 + 테스트). 서버 종류가 확정되면 시작 권장.

### 3.4 검증 계획

- **P1 live probe (사용자 서버, 실제 키 필요 — 더미 키엔 401)**:
  ```bash
  python -c "import json,urllib.request; r=urllib.request.Request('http://172.21.135.113:4000/v1/messages', data=json.dumps({'model':'<모델ID>','max_tokens':256,'stream':False,'thinking':{'type':'enabled','budget_tokens':1024},'messages':[{'role':'user','content':'2+2는? 추론해'}]}).encode(), headers={'content-type':'application/json','authorization':'Bearer <실제키>','anthropic-version':'2023-06-01'}); print(json.dumps(json.loads(urllib.request.urlopen(r, timeout=120).read()), ensure_ascii=False, indent=1))"
  ```
  → `content`에 `thinking` 블로크가 있느냐가 T1/T3 분기점.
- **P2 unit**: Plan 바디 unsigned thinking 제거 가드 테스트 (T3 시).
- **P3 probe**: T2의 thinking round-trip + 대시보드 표시.

---

## 4. 권장 우선순위 (2026-11-12 진행 결과)

| 순서 | 항목 | 코드량 | 효과 | 상태 |
|---|---|---|---|---|
| 1 | A1+T1 문서화(모드 선택 가이드: 팀·thinking → `--local-compat`) | 0 | 즉시 "사용 가능" 경로 확보 | **완료** — README "모드 선택 가이드: Agent Teams와 추론(thinking)" + 대시보드 Local 설정 힌트(`localExtra`) |
| 2 | A2 로컬 동시접수·타임아웃 env 노출 | 소 | 팀 실사용 병렬도 확보 | **완료** — `ROUTE_LOCAL_CONCURRENCY`(1–64)·`ROUTE_LOCAL_QUEUE_LIMIT`(0–256)·`ROUTE_LOCAL_QUEUE_WAIT_MS`(0–600000, 기존 60s 상한 완화)·`ROUTE_LOCAL_ATTEMPT_TIMEOUT_MS`(30s–15min, compat/local-only) + 런처 passthrough(잘못된 값은 무시+경고) + 테스트 |
| 3 | T2 품질 검증 thinking round-trip + 대시보드 가시성 | 소 | 서버 능력 확인·사용자 안내 | **완료** — `modelQualityProbe.evaluate_thinking`(`--thinking` 옵트인 3번째 호출) + 도구 왕복 검사 결과·evidence에 `thinking` + `capabilities.thinking`/`agent_teams` + Local 설정 힌트 |
| 4 | A3 시뮬레이션 + live 팀 검증, 대시보드 `agent_teams` 행 | 중(테스트) | 회귀 안전망 | **시뮬레이션 완료** — `tests/simulate_agent_teams_thinking.mjs`(10라운드, §5) + `capabilities.agent_teams` 행. **live 팀 검증은 사용자 인터랙티브 세션 필요**(아래 절차) |
| 5 | P1 live probe → (서버가 thinking 미지원이면) T3 브릿지 | 중 | 로컬 thinking 패널(원하면) | **준비 완료** — `python modelQualityProbe.py --provider local --run --thinking` (실제 키 필요). T3는 P1 결과 대기(§3.3) |

**A3 live 팀 검증 절차(사용자 인터랙티브 세션):**
1. `$env:ROUTE_LOCAL_CONCURRENCY="4"` + `.\OpenClaude.ps1 -Provider Local -LocalCompat`(또는 OpenRouter).
2. 셸 또는 settings.json `env`에 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`.
3. 세션에서 teammate 2~3명 spawn → `~/.claude/teams/{team}/inboxes/*.json`(메일박스)과 `~/.claude/tasks/{team}/`(공유 태스크) 파일 생성 확인, 작업 완료.
4. 대시보드에서 `routes.external` 증가와 teammate 턴 수 일치, `local_busy` 없음을 확인(있으면 `ROUTE_LOCAL_CONCURRENCY` 상향).
5. R4 확인: 라우팅 로그에 `native_feature:unsupported_beta` → Plan(403) 전환이 없어야 함(향후 Claude Code 버전별 재확인).

**P1 live probe:**
```powershell
python modelQualityProbe.py --provider local --run --thinking
```
→ `thinking.thinking_status`: `present`(서버가 thinking 블록 출력 — compat 세션에서 thinking 패널 기대 가능) / `absent`(평문 응답 — T1 수준, 추론은 텍스트로 표시) / `error`(서버가 thinking 파라미터 거부 — 파라미터 없이 재확인). 더미 키면 401이므로 저장된 실제 키가 필요합니다.

## 5. 10회 시뮬레이션 결과와 문제점 (2026-11-12)

`tests/simulate_agent_teams_thinking.mjs` — `createProxy` + fetch fixture로 각 기능 10라운드. 합성 GPU 지연(턴당 90–220ms), 팀 툴 세트(Agent/SendMessage/TaskCreate/Get/List/Update + Bash)·`cache_control` 포함. JSON 리포트: `tests/.sim-teams-thinking-report.json`.

### 5.1 Agent Teams — 10라운드 × 버스트(동시 4–12턴, 총 80턴)

각 라운드가 **모든 teammate의 모든 턴을 동시에 발사**(전원 동시 유휴 worst case). 일반 로컬 / local-compat 모드 교대, teammate 2–6명.

| 관찰 | 결과 |
|---|---|
| `local_busy` 503 | 정확히 **burst > 10**(동시 2 + 대기 8)일 때만 발생: 10라운드 중 2라운드(6명×2턴=12)마다 2턴 각 거절 → **4/80턴**. 두 모드 모두 동일 |
| burst ≤ 10 | 전원 흡수(0 거절). 합성 지연 기준 최대 큐 대기 **877ms** |
| 예상 밖 실패(400/500/손상/pinning) | **없음** — 10/10 라운드 안정 |

**도출된 문제점과 해석:**
- **P-A1(설계 한계, 이제 완화됨)**: 기본 한계(2+8)에서 6명 팀의 버스트는 1/6 턴이 `local_busy`. A2 이후 `ROUTE_LOCAL_CONCURRENCY=4`(4+8=12)로 해소.
- **P-A2(실사용 함정)**: 실제 GPU 지연(턴당 수 초)에서는 **큐 대기 × 턴당 지연 > `localQueueWaitMs`(기본 60s)가 일반적** — 예: 20s/턴 × 대기열 8 = 160s. 예전에는 대기 상한을 올릴 경로가 없어(60s 고정) `local_busy`가 불가피. A2로 대기 10분까지 가능.
- **P-A3(코드 확인)**: 큐 대기 시간은 attempt 타임아웃에 **계산되지 않음**(admission 이후에 attempt 시작). 일반 로컬은 30초가 *침묵 윈도우*(chunk마다 재장전) + 300초 총상한이라 스트리밍 유지 시 긴 생성은 생존. compat/local-only는 **120초 고정 총한도** — 느린 추론 모델의 긴 턴을 끊을 수 있었음 → A2 `ROUTE_LOCAL_ATTEMPT_TIMEOUT_MS`(최대 15분).
- **P-A4(harness 교훈, 제품 결함 아님)**: 일반 모드 모델 게이트는 **전체 모델 ID**(`claude-sonnet-5-5`)만 허용하고 bare `sonnet`은 403 `Model blocked` — real Claude Code는 전체 ID를 보내므로 실사용 영향 없음. 시뮬레이션 클라이언트는 전체 ID 사용으로 수정.

### 5.2 thinking — 10라운드 × 6시나리오 (60 probe, 10/10 라운드 일관)

| 시나리오 | 모드 | 결과(10/10 일관) | 분류 |
|---|---|---|---|
| S1 thinking=enabled 요청 | 일반 local | 업스트림에 `thinking:{type:'disabled'}` 수신 — **모형이 결코 추론하지 못함(무음 능력 손실, 의도적 설계)** | known-limitation |
| S2 서버가 thinking 블록 반환 | 일반 local | **HTTP 503 `local_upstream_error`** — 턴 실패, Plan 폴백 없음(local 세션). thinking 지원 로컬 서버가 일반 로컬 세션을 깨뜨림 | known-limitation |
| S3 thinking=adaptive 왕복 | local-compat | 요청 그대로 전달 + **서명 없는 thinking 블록을 클라이언트에 반환**(JSON·SSE 델타 종합)까지 동작 | verified-working |
| S4 thinking 히스토리 회신 | local-compat | 외부 요청 사본에서 **자동 제거** — pinning 루프 불성립 | verified-working |
| S5 thinking.display(미지원 설정) | 일반 openrouter | **Plan → HTTP 403 "Saved subscription OAuth required"**, 업스트림 호출 0회 — 구독 OAuth 없이는 하드 실패 | known-limitation |
| S6 서명된 thinking 히스토리(Plan 턴 후) | 일반 local | 로컬 업스트림 전에 제거 — **세션 라우팅 유지** | verified-working |

**도출된 문제점:**
- **P-T1**: 일반 로컬의 S2 — 로컬 서버가 thinking을 내는 순간 그 세션은 계속 깨짐(재시도 후 503). 유일한 해결 = `--local-compat`. 사용자 혼동 포인트 → README·대시보드 힌트로 명시.
- **P-T2**: S5 — thinking의 *미지원 설정값*(`display` 등)은 Plan 우회로 이어지는데, 구독 없으면 403으로 죽음. compat 모드로 가려면 전체 세션을 바꿀 수 없는 일반 OpenRouter 사용자에게는 dead end — T3 브릿지가 아닌 **compat 세션 권장**이 현실적 해결.
- **P-T3(변수, P1로 확정)**: S3의 "서버가 thinking 블록을 내는 것"은 fixture 전제 — 실제 사용자 서버(172.21.135.113:4000, LM Studio 추정)는 미확인. `absent`면 T1 수준(텍스트 추론)에서 종료, `present`이면 T3 불필요(이미 동작).

### 5.3 시뮬레이션으로 확인되지 않은 것 (live 필요)

1. **R4**: 실제 Claude Code가 보내는 베타 헤더 세트의 Plan 우회 여부(버전 의존).
2. **Agent Teams의 파일 기반 조정**(메일박스/태스크)은 Claude Code 내부 동작 — 프록시 시뮬레이션 범위 밖.
3. **S2의 503이 teammate 세션에서 lead에 어떻게 보고되는가**(공식 문서상 실패 통보) — live 확인.
4. thinking 블록의 **클라이언트 표시 의미론**(서명 없으면 "reasoning omitted"?).

## 6. 리스크·미확인 사항

1. **R4**: 일반 local 세션의 실험 베타 헤더 — 향후 Claude Code 버전이 새 베타를 기본 보내면 Plan 우회(구독 없는 환경 403). live 검증 + 런처의 local 세션 베타 정책 검토 필요.
2. Agent Teams는 Claude Code 측 **실험 기능** — resume/shutdown/태스크 지연 한계는 프록시 밖.
3. **Windows**: split panes 불가 → in-process 한정.
4. 로컬 서버별 thinking 능력 **서버 의존** — P1 probe로 확정 (사용자 서버 포트 4000 = LM Studio 추정).
5. 팀의 토큰 비용 선형 증가 — 대시보드 usage 통계로 추적 가능(기존 기능).
6. T3 서명 없는 thinking의 클라이언트 표시 의미론 — live 확인 필요.

## 부록: 증거 인덱스

- 모드/옵션: [claudeRouteProxy.mjs:1153-1269](../claudeRouteProxy.mjs) (createProxy 시그니처, `noPlan`/`compatProfile` 1214)
- thinking: 강제 disable [mjs:2182-2183](../claudeRouteProxy.mjs), 히스토리 제거 [mjs:560-577](../claudeRouteProxy.mjs), 요청 검증 `unsupported_thinking_config` [mjs:686-689,715](../claudeRouteProxy.mjs), 응답 거부 `rejectThinking` [mjs:912](../claudeRouteProxy.mjs)+[mjs:2326](../claudeRouteProxy.mjs), local-only text/tool_use만 [mjs:2342-2359](../claudeRouteProxy.mjs), compat thinking 필드 허용 [mjs:2331-2340](../claudeRouteProxy.mjs)
- Plan 폴백: [mjs:1990-2039](../claudeRouteProxy.mjs) (구독 OAuth, planModel 매핑)
- 로컬 admission: [mjs:1193-1195](../claudeRouteProxy.mjs), [mjs:1271-1307](../claudeRouteProxy.mjs), `local_busy` [mjs:1969-1971](../claudeRouteProxy.mjs)
- 런처: compat env 보존(AGENT_TEAMS 포함) [openrouterAdvisor.py:832-851](../openrouterAdvisor.py), 베타 비활성 [openrouterAdvisor.py:858-870](../openrouterAdvisor.py), CLI 플래그 [openrouterAdvisor.py:1655-1657](../openrouterAdvisor.py)
- 대시보드: capabilities 매트릭스 [routeDashboard.py:6544-6547](../routeDashboard.py), `unsupported_thinking_config` 메시지 [routeDashboard.py:179](../routeDashboard.py)
- 테스트: 서브에이전트 스모크(teams 명시 비활성) [tests/native_subagents_smoke.mjs:4,43,94](../tests/native_subagents_smoke.mjs), 도구 품질 검증 [modelQualityProbe.py:19-60](../modelQualityProbe.py)
- 공식 문서: code.claude.com/docs/en/agent-teams (2026-11-12 확인) — 아키텍처(메일박스/태스크 파일), spawn 조건(인터랙티브), 모델 선택, 알려진 한계
