# Claude Code 기능 보존: DeepSeek Harness·OpenCode 비교 분석

분석일: 2026-10-29. 설치된 Claude Code: **2.1.312**. Claude-Cheat 기준은 `aa2504f`와 현재 작업 트리입니다. 이 문서의 보완안은 설계 제안이며, 런타임에 적용한 변경이 아닙니다.

## 1. 결론과 요구사항 정정

사용자의 기준은 **Claude Code를 전면 실행기로 유지하면서 Agent Teams·Subagent·병렬 작업·SSH/MCP·Skills·Plugins·hooks·권한·세션 기능을 최대한 그대로 사용하는 것**입니다. 단순히 원본 CLI를 실행하거나 설정 파일을 남기는 것으로 충족했다고 판단하면 안 됩니다. 같은 작업을 원본과 라우팅 실행에서 비교해야 합니다.

현재 일반 실행 모드는 원본 호스트를 유지하지만 전체 기능 동등성은 검증되지 않았습니다. 최근 추가한 **Local 동시 2건 / 대기 8건 / 5초 초과 시 Plan 우회**는 공유 서버 보호를 위한 제한입니다. 그러나 팀의 긴 병렬 추론을 전제로 검증하지 않아, 사용자가 의도한 Local 경로를 일찍 이탈할 수 있습니다. 이 정책을 전체 기능 보존의 완성으로 설명한 것은 부정확합니다.

두 참고 저장소 모두 독립적인 에이전트 실행기입니다. 자체 subagent·도구 실행·MCP·plugin을 Claude Code 기능과 동일시할 수 없습니다. **실행기를 교체하지 않고 HTTP 모델 경계의 대기·취소·오류·변환·검증 패턴만 적용**하는 방향을 권장합니다.

## 2. 분석 범위와 재현 기준

| 대상 | 고정 커밋 | 읽은 주요 범위 |
| --- | --- | --- |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/tree/f3803c96974ad4a94195fb97e5c637e9c2a3afe3) | `f3803c96974ad4a94195fb97e5c637e9c2a3afe3` | core agent-loop/tools, subagent, llm/deepseek/retry, architecture, MCP/skill/SSH 문서, 관련 테스트 |
| [OpenCode](https://github.com/anomalyco/opencode/tree/c1b5f1b416c2bff799a4be6a75d640e9ba28d42f) | `c1b5f1b416c2bff799a4be6a75d640e9ba28d42f` | v2 packages/core, ai, server, schema, sdk의 관련 경로와 테스트 |
| Claude-Cheat | `aa2504f` + 작업 트리 | 런처 환경·모드, Local admission, 재시도·회로 차단, 메시지 검증·thinking 변환, 기존 요구사항·실사용 검증 기록 |

두 저장소를 별도 임시 체크아웃에서 읽었습니다. 패키지 설치, 외부 저장소 코드 실행, 해당 저장소 테스트 실행, 실제 모델 추론은 하지 않았습니다. 테스트 파일을 확인했다는 것은 그 테스트를 실행해 통과했다는 의미가 아닙니다. 전체 파일 전수 보안 감사도 아닙니다. 두 저장소의 LICENSE는 MIT이며, 향후 코드를 직접 가져올 경우 원문 라이선스·저작권 고지가 필요합니다.

## 3. DeepSeek Harness에서 얻은 근거

### 3.1 도구 병렬성은 제한하면서 결과와 취소 계약을 보존

한 step의 병렬 도구 실행 기본값은 10입니다. rolling pool이 실행 자리를 채우고, 순차 실행이 필요한 지점에 장벽을 두며, 결과를 모델이 요청한 순서로 정리합니다. 취소 시 새 도구 실행을 멈추고 이미 시작한 작업을 정리합니다.

- [기본 병렬값](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/core/agent-loop/src/constants.ts#L5)
- [도구 실행 pool](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/core/tools/src/batch.ts#L186)
- [제한 전달 테스트](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/core/agent-loop/tests/tool-executors.spec.ts#L284)

Claude-Cheat 적용점: 에이전트 생성 수, 도구 실행 수, LLM 추론 수를 같은 제한으로 취급하지 않습니다. Claude의 도구 실행을 프록시에서 다시 구현하지 않고, 요청 대기와 취소의 수명만 명확히 관리합니다. DSH의 10은 도구 병렬값이며 Local 서버의 적정 동시 추론 수가 아닙니다.

### 3.2 자식 에이전트는 명시적인 생명주기를 가짐

부모 중단 시 생성 중인 자식까지 취소하고 자식 정리·idle·최종 상태 기록을 기다립니다. 이는 응답 소켓을 닫는 것과 실제 자식 작업 종료를 구분하는 설계입니다.

- [subagent manager 종료 처리](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/subagent/subagent/src/manager.ts#L1125)
- [동시 자식 식별 검증](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/subagent/subagent/tests/structured.spec.ts#L743)

Claude-Cheat 적용점: 큐 취소, 업스트림 연결 취소, Local 서버 계산 종료를 각각 검증해야 합니다. HTTP 연결 종료만으로 GPU 계산도 취소됐다고 표시하면 안 됩니다. 기존 실사용 기록 역시 실제 서버 계산 중단은 미검증으로 남겨 두었습니다.

### 3.3 빈 응답과 미완료 스트림을 구분

DeepSeek 어댑터는 reasoning·tool block·usage·종료 사유를 누적하고 도구 JSON을 검사합니다. 정상 종료했지만 block이 없으면 `EMPTY_RESPONSE`, 종료 조건을 충족하지 못한 EOF는 `STREAM_CLOSED`로 분류합니다.

- [스트림 변환·종료 검사](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/llm/llm-deepseek/src/translate.ts#L104)
- [빈 응답·EOF 테스트](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/llm/llm-deepseek/tests/stream.spec.ts#L61)

Claude-Cheat 적용점: 현재 `200:empty_response`를 혼잡으로 단정하지 않습니다. 본문·reasoning 내용 없이 HTTP 상태, content type, body 길이, block 종류·개수, 종료 사유, 도구 수, 실제 보고된 usage와 모델 일치 여부만 수집하는 진단을 우선합니다. 본문 0바이트, reasoning-only, 잘린 stream, 토큰 예산 소진, 잘못된 tool JSON을 분리해야 합니다.

### 3.4 thinking과 도구 이력은 제공자 계약으로 처리

DSH의 DeepSeek 어댑터에는 thinking enabled/disabled, effort, context capacity 설정이 있으며 도구 호출과 바로 이어지는 결과 ID의 대응을 검증합니다. thinking을 꺼버리거나 다른 제공자의 서명을 임의로 만들어 호환된 것처럼 취급하지 않습니다.

- [모델 설정](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/llm/llm-deepseek/src/config.ts#L22)
- [도구 이력·thinking 직렬화](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/llm/llm-deepseek/src/serialize.ts#L118)

Claude-Cheat 적용점: 현재 일반 모드는 외부 thinking을 끄고 Plan 재사용에 부적합한 reasoning을 제거합니다. 이것은 원본 reasoning 동등성에 제약입니다. 다만 DSH 어댑터를 내부 Local 엔드포인트에 그대로 복사해도 된다는 뜻은 아닙니다. 현재 서버의 실제 API 규격을 먼저 확인해야 합니다.

### 3.5 재시도와 plugin 설계에서 가져올 것

DSH는 오류 코드를 기준으로 재시도하며 기본 재시도 상한·지수 지연·jitter·Retry-After를 관리합니다. 빈 응답도 재시도 대상이지만 그 자체를 부하 측정값으로 사용하지 않습니다. 기본 정책을 그대로 가져오면 기존 CLI 재시도와 중첩될 수 있습니다.

- [재시도 정책](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/llm/llm/src/retry-policy.ts#L12)
- [재시도 실행·종료](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/llm/llm-retry/src/index.ts#L212)
- [plugin 아키텍처](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/docs/architecture.md)

MCP·Skills·SSH는 DSH 자체 plugin입니다. 이를 도입해 Claude의 기존 권한·설정·SSH 도구 경로를 대체하는 것은 이번 요구와 맞지 않습니다.

## 4. OpenCode에서 얻은 근거

### 4.1 subagent의 세션·작업·취소 소유권

subagent는 별도 자식 세션과 job을 갖습니다. foreground 호출은 완료를 기다리고, 취소하면 자식 세션과 job도 중단합니다. 모델은 agent/명시 설정/상속의 코드상 우선순위로 선택됩니다. Claude Agent Teams의 공유 task·mailbox와 같은 구현이라고 볼 수는 없습니다.

- [자식 세션·모델·취소](https://github.com/anomalyco/opencode/blob/c1b5f1b416c2bff799a4be6a75d640e9ba28d42f/packages/core/src/tool/plugin/subagent.ts#L174)
- [subagent 수명 테스트](https://github.com/anomalyco/opencode/blob/c1b5f1b416c2bff799a4be6a75d640e9ba28d42f/packages/core/test/subagent-lifetime.test.ts#L55)

Claude-Cheat 적용점: 원본 Claude의 자식 모델 설정·MCP 상속·권한을 보존하고, API에서 요청별 경로를 확인할 수 있어야 합니다. 프록시가 도구 이름만 보고 별도 자식 실행기를 만들면 안 됩니다.

### 4.2 무제한 도구 병렬 실행을 Local 부하 제어로 오해하면 안 됨

읽은 LLM 도구 루프는 `concurrency: "unbounded"`로 실행합니다. 확인한 llm/job/retry 경로에서는 제공자 전체의 동시 추론 제한을 찾지 못했습니다. 저장소 전체에 그런 기능이 없다고 단정하는 것은 아닙니다.

- [도구 병렬 실행과 결과 순서](https://github.com/anomalyco/opencode/blob/c1b5f1b416c2bff799a4be6a75d640e9ba28d42f/packages/ai/src/llm.ts#L73)

Claude-Cheat 적용점: 병렬 기능 보존을 위해 Local 추론까지 무제한으로 푸는 것은 권장하지 않습니다. Claude의 에이전트 병렬성과 공유 서버의 처리 용량은 별도로 관리해야 합니다.

### 4.3 오류 종류와 재실행 안전성에 따른 재시도

오류를 구조화하고 session 요청과 one-shot 요청의 재시도 정책을 구분합니다. session 경로에는 지수 지연·jitter·상한이 있고, one-shot은 과부하 등 재실행 근거가 있는 오류만 더 제한적으로 재시도합니다.

- [재시도 정책과 상한](https://github.com/anomalyco/opencode/blob/c1b5f1b416c2bff799a4be6a75d640e9ba28d42f/packages/core/src/model-retry.ts#L11)
- [오류 분류](https://github.com/anomalyco/opencode/blob/c1b5f1b416c2bff799a4be6a75d640e9ba28d42f/packages/ai/src/provider-error.ts#L87)
- [HTTP 힌트 해석](https://github.com/anomalyco/opencode/blob/c1b5f1b416c2bff799a4be6a75d640e9ba28d42f/packages/ai/src/route/executor.ts#L20)

Claude-Cheat 적용점: CLI→프록시→모델 서버의 중첩 재시도를 계산하고 총 시간 예산을 정합니다. 일부 응답이 클라이언트에 전달된 뒤에는 재생·Plan 전환으로 도구 호출을 중복시키지 않습니다. 재시도 횟수만 늘려 빈 응답을 덮지 않습니다.

### 4.4 프로토콜 어댑터·모델 능력 선언

공통 이벤트 모델과 제공자별 어댑터를 두고 미완료 스트림·불완전한 tool 입력을 오류로 유지합니다. Local 모델 목록과 도구·이미지·컨텍스트 능력도 구분합니다. 목록에 보이는 모델이라고 실제 tool 품질이 보장되는 것은 아닙니다.

- [미완료 스트림 분류](https://github.com/anomalyco/opencode/blob/c1b5f1b416c2bff799a4be6a75d640e9ba28d42f/packages/ai/src/route/client.ts#L318)
- [도구 스트림 테스트](https://github.com/anomalyco/opencode/blob/c1b5f1b416c2bff799a4be6a75d640e9ba28d42f/packages/ai/test/tool-stream.test.ts#L182)
- [Local 제공자 모델 능력](https://github.com/anomalyco/opencode/blob/c1b5f1b416c2bff799a4be6a75d640e9ba28d42f/packages/core/src/plugin/provider/local.ts#L14)

Claude-Cheat 적용점: Messages·tools·thinking·streaming·context·usage 각각을 설정/선언/실측으로 구분합니다. 제공자 변경만으로 모델 호환성을 가정하지 않습니다. MCP·skill·plugin은 OpenCode 자체 기능이므로 Claude 쪽 구현을 대체하지 않습니다.

## 5. Claude-Cheat와 요구사항 사이의 차이

| 항목 | 현재 코드 근거 | 판단 |
| --- | --- | --- |
| 일반 모드 사용자 설정 | `openrouterAdvisor.py`의 `routed_session`: 비격리 경로는 `os.environ.copy()` 사용 | Agent Teams를 일괄 비활성화하지 않음. 실제 팀 왕복 검증 필요 |
| Local 병렬 추론 | `claudeRouteProxy.mjs`의 `localConcurrency`, `localQueueLimit`, `localQueueWaitMs` | 에이전트 수와 무관한 프록시 단위 2/8/5초 제한. 대기 초과→Plan이 병렬 Local 요구와 충돌 가능 |
| 격리 호환 모드 환경 | `clean_env()`는 `CLAUDE_CODE_*` 제거, compat는 일부 값만 복원 | shell에서만 설정한 team 등 선호가 사라질 수 있음. settings.json에서 재적용될 수 있어 실제 효과 별도 확인 |
| subagent 모델 고정 | `if compat` 안에서 `CLAUDE_CODE_SUBAGENT_MODEL` 설정 | **일반 모드의 강제 고정이 아님**. 호환 모드에서 사용자의 모델 선택 보존 문제 |
| LocalMode 백그라운드 | `local_only and not local_compat`에서 `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` | 명시적 제한 모드. 일반 Local 모드와 혼동하지 말아야 함 |
| reasoning | 일반 외부 요청의 thinking disabled, 일부 이력 제거 | 모델 바꾸기만으로 원본 reasoning 기능 동등하지 않음 |
| native 기능·별칭 | Opus/Haiku/Fable 및 제외 요청은 일반 모드에서 Plan 사용 | Claude 기능 보존과 모든 요청 Local 사용은 다른 조건. 명시적인 모델/우회 정책 필요 |
| 스트리밍 | 기본 전체 응답 검증·버퍼링, 일부 passthrough 경로 | 원본과 첫 응답 체감 차이. 중간 우회 불가 조건 포함해 검증 필요 |
| 현재 진단 API | 모델·경로·Local 실행/대기·실패 건수 제공 | 실제 Agent Team 시작/통신/완료를 증명하지 못함 |

공식 문서도 gateway가 새 필드·헤더·스트림 종료 및 오류 정보를 잘못 처리하면 기능이 깨질 수 있음을 설명합니다. routing hint는 명시 설정 시 받을 수 있으므로 향후 관측에 활용할 수 있지만, 누락 시 에이전트 관계를 추측해서는 안 됩니다. 오류 문구를 모두 일반 오류로 바꾸면 Claude의 기능 협상 재시도가 작동하지 않을 수 있습니다. [공식 gateway 호환성 문서](https://code.claude.com/docs/en/llm-gateway-protocol)

Anthropic은 gateway를 통한 비-Claude 모델 사용을 공식 지원하지 않는다고 명시합니다. 이는 개발을 중단할 이유가 아니라, **전체 기능 보장을 주장하기 전에 실제 버전·모델별 검증이 필요하다는 경계**입니다. [공식 LLM gateway 문서](https://code.claude.com/docs/en/llm-gateway)

## 6. 권장 보완 순서 — 아직 미구현

### P0. 기능 동등성 검증을 완료 기준으로 고정

원본 Claude Code와 Claude-Cheat를 동일 workspace·설정·권한으로 비교합니다. Agent tool이 목록에 존재하는 것만으로 통과하지 않습니다. 팀 생성→작업 배분→메시지 왕복→결과 합산→종료, subagent foreground/background, MCP 도구 발견·호출, SSH 읽기 작업, skill/plugin/hook 실행을 완료까지 검사합니다.

### P1. Local 대기와 Plan 우회를 분리

공유 서버 보호용 실행 한도는 유지하되, 사용자 작업의 허용 시간 예산 내에서 취소 가능한 대기를 제공합니다. 단순한 5초 포화는 upstream 실패와 구분합니다. 대기 만료 시 행동은 Local 대기/명시 오류/승인된 Plan 우회 중 정책으로 드러내야 합니다. 초기에는 구성 가능한 유한 동시성·대기·시간 예산으로 시작하고 실측 없이 AI 기반 최적화나 무제한 큐를 도입하지 않습니다.

추후 적응형 제한은 429·Retry-After·실제 서버 큐 지표 같은 혼잡 근거로 제한을 낮추고, 성공 관측 후 천천히 회복시키는 식으로 검토합니다. `empty_response`를 혼잡 신호로 쓰지 않습니다. 한 프록시 제한으로 다른 사용자나 다른 프록시의 서버 부하까지 제어할 수 없습니다.

### P2. 빈 응답 원인을 분류한 뒤 모델 어댑터 보완

내용 없는 진단 메타데이터로 실패 형태를 구분하고, 현재 Local API가 제공하는 thinking·tool·stream 계약을 확인합니다. reasoning-only를 성공한 최종 답변으로 위장하거나, 없는 서명·토큰 수를 만들지 않습니다. 실제 지원 기능만 변환하며, 지원되지 않는 기능은 명시된 경로로 처리합니다.

### P3. 원본 호스트 설정 보존과 모델 정책 명확화

일반 모드를 기능 보존 기준으로 삼습니다. 격리 호환 모드는 team·subagent·background 관련 환경값의 보존 목록과 명시 사용자 모델의 우선순위를 점검합니다. 인증·라우팅 환경값을 무조건 다시 상속하는 방식으로 고치지 않습니다. 기존 LocalMode를 조용히 의미가 다른 모드로 바꾸지도 않습니다.

### P4. 스트리밍과 재시도 총예산 정리

SSE 이벤트 순서·ping·최종 종료·부분 tool JSON을 계약 테스트로 검증합니다. 이미 전달한 응답은 다른 모델로 이어 붙이지 않습니다. CLI와 프록시의 재시도 중첩을 계측하고, 큐 대기·네트워크·추론·재시도 시간을 분리해 표시합니다.

### P5. 팀 관측은 실제 식별 근거가 있을 때만

지원되는 Claude 버전의 agent/session/prompt 힌트를 로컬에서 제한된 형식으로 읽어 실행·대기·취소·실제 provider를 묶는 방안을 검토합니다. 힌트는 권한 근거로 쓰지 않고, 원문 프롬프트·키·SSH 내용은 저장하지 않습니다. 누락 시 unknown으로 표시하고, background probe 없이 기존 요청에서 집계합니다.

## 7. 합격 기준

| 시나리오 | 합격 조건 | 현재 증거 |
| --- | --- | --- |
| 원본 vs 라우팅 시작 | 모델 변경 이외의 MCP/skills/plugins/hooks/권한 설정 차이 설명 가능 | 일부 기존 검증, 최신 전체 비교 필요 |
| subagent 1개 | 생성·도구·결과 반환 및 실제 모델 확인 | 실제 최신 실행 미검증 |
| 병렬 4개·8개 | 결과 누락·도구 중복 없음, 큐/우회가 선언 정책과 일치 | semaphore 합성 검사만 존재 |
| Agent Team | 생성·분배·메시지·완료·종료 전 과정 | 미검증 |
| 대기 중 취소 | 큐에서 제거, 이후 추론·Plan 호출 없음 | 기존 합성 검사 있음 |
| 실행 중 취소 | 연결 정리와 CLI 재사용, 서버 계산 종료는 별도 증거 | 연결 일부 검증, 서버 계산 미검증 |
| SSH/MCP | 기존 권한 경로로 승인된 읽기 작업 왕복 | 과거 성공 기록, 최신 팀 내부 재검증 필요 |
| Skill/Plugin/Hook | discovery뿐 아니라 실제 실행과 결과 반영 | 최신 통합 재검증 필요 |
| 권한 거부 | 거부한 도구가 Local/Plan/다른 에이전트에서 우회 실행되지 않음 | 원본과 대조 필요 |
| compact·resume | tool pairing·대화 이력·에이전트 관계 유지 | 단일 세션 일부 기록, 팀 재개 미검증 |
| reasoning/stream | malformed/empty/incomplete를 구분하고 도구 중복 실행 없음 | 기존 합성 검사, 현재 서버 원인 미확정 |
| 부하·모델 변경 | 요청별 실제 모델, 대기 시간, 우회 사유가 API와 화면 일치 | 일부 지표 있음, 팀 단위 관측 미구현 |

## 8. 이번 조사에서 변경한 것과 남은 작업

이번에는 분석 문서와 README 안내만 갱신합니다. 기존 작업 트리의 UI·proxy 변경은 보존했습니다. 실행 중 프록시·인증·사용자 설정·LLM 부하는 변경하지 않았고, 새로운 추론 요청도 보내지 않았습니다.

구현 우선순위는 **P0 검증 기준 → P1 병렬 대기/우회 정책 → P2 빈 응답 진단·어댑터**입니다. 대기/우회 기본값·모델 정책·공개 진단 API 변경은 영향과 구체적인 계약을 확정한 뒤 구현해야 합니다. 저장소의 알고리즘을 일부 참고했다고 Claude Code 전체 기능 호환 완료로 표시하지 않습니다.


## 후속 구현·검증 (2026-10-29)

앞선 표와 5초 대기 설명은 최초 조사 시점의 기록입니다. 사용자 승인에 따라 Local 큐 대기를 기본 60초로 변경했습니다. `ROUTE_LOCAL_QUEUE_WAIT_MS`로 0~60000ms 범위에서 설정하며 동시 실행 2개·대기 8개는 유지합니다. 큐 만료·포화 시 기존 Plan/오류 정책을 적용합니다. 이는 추론과 재시도를 포함한 전체 응답 기한이 아닙니다. 실행 중인 프록시에는 재시작 후 적용됩니다.

호환 실행 경로에서 명시된 Agent Teams·subagent·작업 목록·background·IDE·plugin·skill 환경 선호를 보존합니다. 인증 격리와 명시적 제한 LocalMode는 유지합니다. Claude 자체 관리 명령을 모델 연결 검사 없이 전달하도록 보완했으나, `--bg` 이후 프록시 수명 문제까지 해결한 것은 아닙니다.

Plan의 날짜형 Retry-After를 초로 변환하고 일반적인 기능 거부 오류의 본문·헤더 보존을 검증했습니다. 빈 응답은 본문이나 도구 인자를 기록하지 않고 `validation_detail`의 제한된 분류값으로 구분합니다. 자격증명 마스킹과 오류 크기 제한은 유지합니다.

회귀 검증: 대상 Node 테스트 318개, 전체 Node 테스트 598개 통과. Python 런처 테스트 11개 통과, 설정 테스트 118개 실행 중 1개 건너뜀, 실패 없음. 실제 설치된 Claude Code 2.1.312의 격리된 합성 백엔드에서 하위 에이전트 2개 및 4개가 직접 연결과 프록시 연결 모두 병렬 실행·완료됨을 확인했습니다. 4개 시험에서 프록시는 최대 2개씩 처리했고 Local 6건·Plan 0건으로 고유한 자식 결과 4개와 부모 완료를 확인했습니다. 재현 명령은 `node tests/native_subagents_smoke.mjs 2` 및 `node tests/native_subagents_smoke.mjs 4`입니다. 이는 실제 공유 Local 모델의 추론·도구 수행 능력이나 Agent Teams 전체 과정 검증을 대신하지 않습니다.

남은 주요 경계는 사용자 지정 API URL에서의 Remote Control 제한, 분리 실행(`--bg`)의 프록시 수명, 실제 인증 환경의 Agent Teams·SSH/MCP·Skills·Plugins 통합 검증입니다. 전체 기능 동등성은 아직 입증되지 않았습니다.


## 핵심 라우팅 성능·기능 공동 개선 연구 (2026-10-29)

이번 절은 현재 소스와 읽기 전용 운영 관측을 대조한 후속 연구입니다. 런타임 정책·설정은 변경하지 않았고 공유 Local 서버에 시험 추론을 보내지 않았습니다. 앞선 최초 조사에 등장하는 5초는 과거 값이며 현재 소스 및 이번 관측 세션의 큐 대기는 60초입니다.

### 관측과 해석

- 조회 시점 활성 세션: 요청 36건, 성공 35건·실패 1건, Local 경로 12건·Plan 24건, 재시도 27회. 진행 중 변하는 스냅샷이며 전체 기간 통계가 아닙니다.
- Local 동시성 2·대기 8·대기 상한 60000ms. 현재 실행/대기 0, 누적 admission 거절 0. 이 세션에서 큐 포화가 우회의 주요 원인이라는 증거는 없습니다.
- Local 최근 표본 p50 약 11.45초, p95 약 79.87초(12표본). Plan 표본 p95 약 131.60초(24표본). 경로별 측정은 외부 시도와 우회 전 지연의 영향을 받을 수 있으므로 순수 서버 추론 시간으로 해석하지 않습니다.
- 라우팅 로그의 최근 집계 창: circuit_open 16건, fallback 8건; 빈 200 응답 24회, timeout 11회. 세션 누적·로그 시간창·시도 건수는 서로 분모가 다릅니다.
- 파서가 읽은 보존 로그 1422개 이벤트 중 세분화된 empty_content 실패가 50회입니다. 이는 유효한 응답 배열이 비어 있었다는 근거이며, 공유 서버 부하라는 원인을 증명하지 않습니다. 원문 응답·프롬프트·도구 인자·인증 정보는 연구 문서에 저장하지 않았습니다.

### 현재 경로의 장점과 비용

Claude Code가 에이전트·MCP·Skills·Plugins·권한을 담당하고 프록시는 HTTP 모델 경계만 처리합니다. 일반 모드에서 네이티브 기능/Opus·Haiku 등은 Plan으로 보내고, 외부에서 지원 가능한 요청만 변환합니다. 기능 검증을 제거하거나 전용 LocalMode로 전환하는 방식은 이 연구의 성능 개선안이 아닙니다.

`claudeRouteProxy.mjs`의 핵심 근거: 모드/기본값 775~803행, admission 847~881행, 기능 판정 및 대상 선택 1403~1444행, 직렬화 재사용 1464행, 최대 시도 수 1478행, 시간 제한 1490~1494행, 응답 전달 1533행 부근, 재시도/회로 1654~1686행. 줄 번호는 작업 트리에 따라 달라질 수 있습니다.

일반 모드는 최대 4번 시도하며 재시도 중에도 Local 슬롯을 보유합니다. 큐 60초는 전체 응답 기한이 아닙니다. 일반 모드는 시도별 30초 무응답 제한과 300초 시도 상한을 사용하며, 데이터가 도착하면 무응답 타이머를 갱신합니다. noPlan 모드는 타이머를 갱신하지 않아 고정 시도 제한이 유지됩니다. 별도 ceiling이 없다고 무제한 대기하는 것은 아닙니다.

`ROUTE_STREAM_PASSTHROUGH`는 기본 꺼짐입니다. 켜더라도 forceJson 경로에는 적용되지 않습니다. 버퍼링은 유효성 검증과 안전한 우회에 유리하지만 첫 출력이 늦습니다. SSE 프레임 전달 후에는 실패해도 Plan으로 재실행하지 않는 기존 보호가 필요합니다.

### 권장 순서

| 우선순위 | 개선안 | 기대 효과 | 검증 기준·주의점 |
| --- | --- | --- | --- |
| P0 | 빈 응답을 본문 없는 구조 정보로 진단 | empty_content 원인 분리 | 상태·응답 모드·허용된 stop_reason·블록 종류·usage 유무만; 원문/도구 인자 미기록. 서버 로그와 승인된 재현으로 원인 확인 |
| P0 | 큐·각 시도·백오프·검증·Plan·전체 지연 분리 | 실제 병목을 기준으로 튜닝 | 기존 요청에서 메모리 집계, 제한된 크기/빈도, 별도 모델 폴링 없음. 첫 바이트와 첫 유효 토큰 구분 |
| P1 | 실패 종류별 재시도 예산 | 반복 빈 응답과 혼잡 증폭 감소 | 401/403/영구 4xx 재시도 금지 유지, 429/503/529 Retry-After 존중. 빈 응답 반복 감축은 원인·복구율 측정 후 결정 |
| P1 | 요청 전체 Local 단계 예산 | 큐+4회 시도+백오프의 긴 꼬리 제한 | 승인된 큐 최대60초와 추론 상한은 별도. 긴 유효 생성 작업을 잘라내지 않도록 모드/요청별 정책 검증·승인 필요 |
| P2 | 검증 가능한 경로만 선택적 스트리밍 | 첫 출력 지연 감소 | tool JSON·thinking/signature·SSE 종료 보존, 취소/backpressure. 전달 시작 뒤 재시도/모델 전환 금지 |
| P2 | 공유 Local 부하에 맞는 제한적 적응 제어 | 혼잡 시 불필요한 압력 완화 | 초기에는 동시2 상한 유지, 수동 승인된 범위에서만 조정. 클라이언트 오류와 서버 혼잡 분리, 전체 GPU 부하라고 표시하지 않음 |

재시도 대기 중 슬롯 반환은 별도 실험 후보입니다. 바로 적용하면 신규 요청이 장애 서버로 더 유입되거나 재시도 공정성이 깨질 수 있으므로 우선 개선안으로 삼지 않습니다. 프롬프트를 임의 요약하거나 도구 스키마를 삭제하는 토큰 절감도 기능 보존을 해칠 수 있어 제외합니다. 캐시 보고 0만으로 서버 캐시가 없다고 단정하지 않습니다.

### 성능과 기능의 공동 합격 기준

동일한 작업·설정으로 기존 정책과 후보 정책을 비교합니다. 단일 요청, 병렬 2/4/8개, 429/503, 빈 200, 긴 응답, 큐/실행 중 취소, 불완전 SSE를 포함합니다. p50/p95 전체 지연·첫 유효 출력·Local 유효 완료율·의도하지 않은 Plan 우회율·시도 증폭률·메모리/CPU를 함께 측정합니다. 낮은 지연만을 위해 작업 실패가 늘어난 변경은 통과시키지 않습니다.

도구 호출/결과 ID 보존, 중복 도구 실행 없음, 권한 거부 보존, subagent 완료/취소, Agent Teams 생성·대화·집계·종료, SSH/MCP·Skill·Plugin·Hook의 실제 실행을 별도 기능 표로 검증합니다. 기존 합성 2/4 subagent 검증은 실제 Local 모델 능력이나 Agent Teams 전체 검증을 대신하지 않습니다.

공식 근거: [게이트웨이 호환 가이드](https://code.claude.com/docs/en/llm-gateway-protocol)는 스트림 순서·종료·ping·오류 전달 보존을 요구합니다. [게이트웨이 안내](https://code.claude.com/docs/en/llm-gateway)는 비-Claude 모델 라우팅을 공식 지원하지 않으므로 모든 기능 동등성을 단정할 수 없습니다. 확인된 기능은 유지하고 검증되지 않은 조합을 명확히 드러내는 방식으로 개선합니다.

독립 검토 보완: 큐 거절 0은 큐 대기가 없었다는 뜻이 아닙니다. `externalAttempts`는 첫 시도 진입 수이며 실제 총 시도 횟수로 읽지 않습니다. SSE ping만 먼저 전달해도 응답이 시작되어 우회가 어려워지므로, 스트리밍 검토에서는 첫 바이트·첫 유효 콘텐츠·재실행 불가 시점을 구분합니다.

### P0 적용 결과 (2026-10-29)

권장안 중 관측과 회귀 검증을 적용했습니다. `attemptCounts`는 실제 외부·Plan 호출을 세며 기존 카운터의 의미는 유지합니다. `routingTimingMs`는 Local admission 대기, 외부 호출/읽기/검증, 재시도 대기, Plan 호출, 전체 처리 시간을 각각 최근 512개 표본으로 집계합니다. 검증 시간은 외부 단계에 포함되며 별도 추론 시간으로 표시하지 않습니다. 전체 시간은 본문 파싱 이후부터 종료까지이고 실패·취소도 포함합니다. 각 단계의 p95는 합산할 수 없습니다.

빈 응답 로그에는 허용된 응답 형식·종료 사유·블록 종류별 개수·안전한 토큰 정수만 추가합니다. 본문·프롬프트·도구 인자는 기록하지 않습니다. 추가 추론이나 폴링 없이 기존 요청에서 수집하며, 취소와 종료 후 `finally`가 겹쳐도 시간은 한 번만 기록합니다.

대시보드의 실행 상태와 진단 및 `/api/diagnostics`에 새 지표를 연결했습니다. 이전 프록시의 누락값은 미보고로 표시합니다. 대시보드 재시작 후 HTTP 200과 진단 API의 누락값 처리를 확인했습니다. 활성 프록시는 유지했으며 새 계측 수집은 실행기를 재시작한 세션부터 시작합니다.

새 회귀 테스트 11개는 빈 JSON/SSE 진단의 정보 보호, 재시도와 우회 집계, 대기·실행·Plan 취소, 타임아웃, 표본 상한, 도구 ID/인자 왕복, 스트림 전송 후 재실행 방지를 확인합니다. 별도 합성 Claude Code 검증에서는 직접·프록시 경로 모두 하위 작업 4개가 완료됐습니다. 이는 실제 Local 모델의 도구 수행 능력이나 Agent Teams·SSH/MCP·Plugin 전체 실사용 검증을 대신하지 않습니다. P1/P2 정책 변경은 미적용 상태입니다.
