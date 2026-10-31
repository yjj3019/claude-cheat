# Claude Code 호환성 기준선과 릴리스 추적

이 문서는 Claude Code 버전이 바뀔 때 Claude-Cheat의 라우팅·스트리밍·도구 호출이 조용히 깨지지 않았는지 확인하는 절차와, 지금까지 실제로 확인한 범위를 구분해 기록한다. **새 라우터를 도입하는 결정 문서가 아니다.**

- 기준: `main` 커밋 `249b85b` + 이 문서 작성 시점의 미커밋 변경
- 작성일: 2026-10-31
- 원칙: "테스트 통과"와 "실제 Claude Code에서 동작 확인"을 섞어 쓰지 않는다.

## 1. 검증 계층

| 계층 | 의미 | 이 저장소의 실행 방법 |
|---|---|---|
| 합성 테스트 | 프록시·실행기 로직만 고정. 외부 추론 없음 | `node --test tests/*.test.mjs`, `python -m unittest` |
| 실제 CLI 스모크 | 설치된 Claude Code를 격리 설정·합성 백엔드로 실행 | 옵트인 스크립트, 사용량·환경 영향 있음 |
| 실서비스 확인 | 실제 Local/OpenRouter 모델 추론 | 별도 옵트인, 비용·서버 부하 발생 |
| 미검증 | 위 어디에도 해당하지 않음 | 문서에 그대로 남긴다 |

## 2. 확인한 Claude Code 버전

| 버전 | 확인 방식 | 결과 | 한계 |
|---|---|---|---|
| 2.1.304 | 실제 CLI, Windows ConPTY, 격리 설정, 합성 백엔드 | 통과 기록 있음 | 시각 렌더링·OS 종료·실제 서버 계산 중단은 미검증 |
| 2.1.310 | 설치 바이너리 문자열 스캔(`ANTHROPIC_CUSTOM_MODEL_OPTION` 훅) | 훅 존재만 확인 | `/model` 픽커 표시·런타임 동작 미검증 |
| 2.1.312 | 합성 백엔드, 하위 에이전트 2개·4개 병렬 | 통과 기록 있음 | 공유 Local 모델 품질·Agent Teams 전체 과정 아님 |
| 2.1.316 | 설치 버전 확인만 (`claude --version`) | 런타임 미검증 | **합성 테스트만 통과한 상태** |

이 표는 지원 보장이 아니라 확인 이력이다. 표에 없는 버전은 새로 검증하기 전까지 미검증으로 취급한다.

합성 스위트 현황(2026-10-31): Node 697개 통과, Python 644개 실행·실패 0·스킵 11. 이 수치는 합성 계층만 뜻하며 2.1.316 실제 CLI 검증을 대체하지 않는다.

## 3. 릴리스 체크리스트 (P0)

새 Claude Code 릴리스가 나오면 릴리스 노트에서 아래 항목을 먼저 확인하고, 해당되면 아래 4절 절차를 실행한다.

- **게이트웨이 요청 형식:** 필수 필드 추가·변경, `anthropic-beta` 헤더, prompt caching, 도구 스키마 형태
- **인증:** OAuth/API 키 경로, 401·403 처리, 키 helper 비활성 동작
- **스트리밍:** 이벤트 순서, `content_block` index, `input_json_delta` 부분 JSON, `message_stop` 누락, `ping`
- **도구 호출:** `tool_use` id·name·input, `tool_result` 왕복, 병렬 도구, MCP 도구 이름
- **오류·복구:** 429/5xx, `Retry-After`, 응답 전송 시작 후 재시도 금지 경계
- **수명:** Ctrl+C, `/exit`, `--bg`, 하위 에이전트, Agent Teams
- **모델 선택:** `/model` 픽커 훅, `ANTHROPIC_CUSTOM_MODEL_OPTION*`
- **게이트웨이 제약:** Anthropic은 비 Claude 모델 라우팅을 공식 지원하지 않으므로 기능 동등성을 주장하지 않는다

## 4. 릴리스 대응 절차

1. `claude --version`으로 설치 버전을 기록한다.
2. 합성 스위트를 실행한다. 실패하면 그 자리에서 원인을 분리한다.
   ```
   node --test tests/*.test.mjs
   python -m unittest discover -s tests -p 'test_*.py'
   ```
3. 계약 테스트(5절) 중 변경과 관련된 항목이 실제로 실행됐는지 확인한다.
4. 필요할 때만 옵트인 실제 CLI 스모크를 실행한다. 개인 설정·인증을 건드리지 않는 격리 환경에서 수행하고, 실행 여부와 결과를 이 문서에 추가한다.
5. 2절 표를 갱신한다. 통과하지 못한 항목은 지우지 말고 "미검증" 또는 "회귀"로 남긴다.

## 5. 계약 테스트가 고정하는 경계

스트리밍·도구 호출 관련 동작은 [tests/streaming.test.mjs](../tests/streaming.test.mjs)에 고정되어 있다.

- 혼합 텍스트·도구 스트림의 content-block 순서와 완전한 도구 입력
- 잘린 도구 JSON: 버퍼링 모드에서는 재시도 후 Plan fallback, 전송 후에는 스트림 오류로 종료
- 첫 바이트 전송 후 전송 실패·프로토콜 위반 시 재시도·fallback 금지(중복 출력 방지)
- 빈 도구 식별자·늦은 thinking 차단
- 느린 클라이언트에서 backpressure 유지

이 테스트들은 **프록시 로직**을 고정한다. Claude Code가 같은 이벤트를 받아 도구를 실행하는지는 별도 실제 CLI 확인이 필요하다.

## 6. 미검증 범위

- 2.1.316 실제 CLI에서의 도구 실행·MCP·서브에이전트·Agent Teams 전체 흐름
- 응답 전송 중 취소 시 제공자 서버의 실제 계산 중단
- OpenRouter `provider.only`의 Messages API 실제 강제 여부
- 실제 Local 모델의 도구 호출 품질과 장기 대화 압축
- Windows Terminal 시각 렌더링, 강제 종료·OS 종료 시나리오

## 7. 1단계 추적 대상 (라우팅·스트리밍)

| 우선순위 | 대상 | 확인할 것 | 한계 |
|---|---|---|---|
| P0 | [anthropics/claude-code 릴리스](https://github.com/anthropics/claude-code/releases) | 3절 체크리스트 항목의 변경 여부 | 릴리스 노트만으로 기능 동등성은 증명되지 않음 |
| P1 | [lemelech/claudeRouter](https://github.com/lemelech/claudeRouter#known-limitations) | SSE 전송 시작 후 제공자 전환 불가 경계의 비교 사례 | 문서 주장만 확인, 코드 미감사 |
| P1 | [Anoopnk/anthropic-proxy](https://github.com/Anoopnk/anthropic-proxy#what-it-translates) | 혼합 텍스트·도구 스트림의 index·이벤트 순서 처리 사례 | thinking·cache 필드 손실 등 손실 모델은 참고하지 않음 |
| P1 | [musistudio/claude-code-router](https://github.com/musistudio/claude-code-router) | 설정 모델과 실제 응답 모델의 분리 표시 방식 | 범위·의존성이 다른 제품 |
| P2 | [Oaklight/llm-rosetta](https://github.com/Oaklight/llm-rosetta) | 다중 프로토콜 변환 구조와 적합성 도구 | 도입 결정 아님 |

외부 저장소의 코드를 복사하지 않는다. 비교는 동작 사례와 경계 확인 목적으로만 한다.

## 8. 2단계 추적 대상 (프로토콜·장애·기능 왕복)

1단계가 라우팅·스트리밍 경계에 집중했다면, 2단계는 **공식 클라이언트 동작 비교**, **실제 기능 왕복**, **실서버 취소**를 다룬다. 외부 도구는 설치하지 않았고, 아래 항목은 조사·검증 후보다.

| 분야 | 대상 | 이 저장소에 쓸 방법 | 한계 |
|---|---|---|---|
| 공식 프로토콜 | [Anthropic TypeScript SDK `BetaMessageStream`](https://github.com/anthropics/anthropic-sdk-typescript/blob/main/src/lib/BetaMessageStream.ts) | 부분 `input_json_delta`, thinking·서명 이벤트 누적 방식을 우리 검증기와 대조해 누락된 계약 테스트를 찾는다 | SDK 동작이 Claude Code 전체 동작을 대표하지는 않는다 |
| 장애 재현 | [Shopify/toxiproxy](https://github.com/Shopify/toxiproxy) | 연결 지연·절단·데이터 분할에서 fallback·취소·중복 실행 방지를 시험한다 | 기존 합성 서버로 충분하면 도입하지 않는다. 429 같은 응답은 별도 mock 필요 |
| API 적합성 | [Oaklight/llm-comply](https://github.com/Oaklight/llm-comply) | Anthropic 형식 검사 항목과 기존 합성 테스트를 대조해 빈틈을 찾는다 | 통과가 기능 동등성을 뜻하지 않는다. 실패 출력에 요청·응답이 포함될 수 있어 민감정보 노출 주의 |
| MCP 왕복 | [MCP Inspector 테스트 서버](https://github.com/modelcontextprotocol/inspector/blob/main/docs/test-servers.md) | 도구 발견→승인→호출→결과 반환→중단을 원본 경로와 라우팅 경로에서 비교한다 | MCP 경로 검증이며 Messages 프로토콜 검증을 대신하지 않는다 |
| Local 서버 | [vLLM](https://github.com/vllm-project/vllm), [llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md), [Ollama Anthropic 호환성](https://github.com/ollama/ollama/blob/main/docs/api/anthropic-compatibility.mdx) | **실제 사용하는 서버일 때만** Messages API 호환성과 취소 시 서버 측 동작을 확인한다 | 서버별·버전별로 다르며 다른 서버에 일반화할 수 없다. llama.cpp는 `--jinja` 설정도 확인 |
| 모델 능력 | [BFCL](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard) | 프록시가 정상이어도 모델이 도구를 잘못 고를 수 있으므로 모델 자체 평가 항목을 참고한다 | 벤치마크 점수는 에이전트 작업·권한·MCP 호환성을 대신하지 않는다 |
| Windows 프로세스 | [win_safesubprocess](https://github.com/mokusatsu/win_safesubprocess) · [Node.js child_process](https://github.com/nodejs/node/blob/main/doc/api/child_process.md) | 부모 강제 종료와 자손 정리 경합을 `localProcessTree.py` 동작과 비교한다 | 외부 의존성 추가 근거가 아니다. 동작 사례 비교용 |
| 부하 실험 | [llm-d inference simulator](https://github.com/llm-d/llm-d-inference-sim/blob/main/docs/metrics.md) | GPU 없이 큐·지연 실험을 설계할 때 지표 항목을 참고한다 | 프로토콜 연결은 검증되지 않았다 |

## 9. 우선 실험 설계

### 9.1 실제 취소가 서버 추론 중단까지 이어지는지

전제: **실제 사용하는 Local 서버와 모델이 설정되어 있어야 한다.** 서버 제품·버전을 먼저 기록한다.

1. 대기 중 요청을 취소한다: Local 슬롯 대기가 걸린 상태에서 클라이언트 요청을 중단하고, 프록시 연결이 정리되는지 확인한다.
2. 추론 중 요청을 취소한다: 첫 토큰 수신 후 중단하고, 프록시 연결 정리와 **서버 측 계산 중단**을 각각 기록한다.
3. 다음 요청이 정상 완료되는지 확인한다(연결·슬롯 누수 없음).

**증거로 인정하지 않는 것:** 프록시 소켓 종료, 합성 job 정리, 클라이언트 연결 끊김만으로는 서버 계산 중단을 증명하지 않는다. 서버 측 지표·로그가 없으면 결과는 "프록시 연결 정리 확인, 서버 계산 중단 미확인"으로 기록한다.

### 9.2 Agent Teams·MCP·Skills 실제 왕복

1. 통제된 MCP 테스트 서버로 도구 발견→승인된 호출→결과 반환→서버 중단을 확인한다.
2. 같은 흐름을 원본 Claude Code 경로와 라우팅 경로에서 각각 실행해 결과를 비교한다.
3. subagent 생성·결과 반환, Skills 로드, 권한 거부는 **항목별로** 기록한다.

**증거로 인정하지 않는 것:** 합성 백엔드에서의 성공, 모델 목록 조회 성공, 도구 제안(probe) 통과는 실제 도구 실행·MCP·Agent Teams 완료의 증거가 아니다.

## 10. 현재 환경 제약

- 2026-10-31 기준 이 작업 환경에서 설정된 제공자는 **OpenRouter만** 확인됐다. Local·CLI는 미설정이므로 9.1 취소 실험을 지금 실행할 수 없다.
- 취소 실험은 Local 서버 구성이 선행되어야 하고, MCP·Agent Teams 왕복은 실제 모델 사용과 권한 승인이 필요하므로 별도 승인 후 실행한다.
- 이 문서 작성 시점에 외부 도구 설치·실험 실행·GitHub 구독은 하지 않았다.
