# Claude Code · Sonnet → OpenRouter DeepSeek

기존 Claude Code Pro 로그인과 설정을 유지하면서 Sonnet 추론만 OpenRouter의 DeepSeek로 대체하는 구성의 작업 기록입니다.

**상태: 합의된 Sonnet 치환 구성 구현·최종 재검증 완료 — 2026-10-20 12:23 KST.**

- 기록 기준: 2026-10-20 13:27 KST (확장 테스트 및 Pro 요청 오류 격리 수정)
- 이 저장소에는 현재 문서만 있습니다. 실행 코드·API 키·OAuth·개인 설정은 포함하지 않습니다.

## 목표와 현재 구성

```text
Claude Code — 기존 Pro 로그인·설정·권한·MCP·플러그인·HUD
  └─ 프로세스 전용 로컬 라우터
      ├─ Sonnet → OpenRouter / DeepSeek V4.1 Flash 1017
      ├─ Opus   → Anthropic / 기존 Pro OAuth
      ├─ Haiku  → Anthropic / 기존 Pro OAuth
      ├─ Fable  → Anthropic / 기존 Pro OAuth · 이용 가능 여부는 앱·플랜 판단
      └─ 미등록 모델·유료 API 대체 → 차단
```

| 항목 | 현재 동작 |
| --- | --- |
| Sonnet 실제 추론 | `deepseek/deepseek-v4.1-flash-1017` · OpenRouter 별도 과금 |
| Opus·Haiku·Fable | 저장된 Claude Pro OAuth 인증으로 Anthropic에 전달; 이용 가능 여부는 앱·계정 플랜이 판단 |
| API 키 보관 | 기존 로컬 `.env.openrouter`를 라우터만 읽음 |
| Claude Code 인증 | OpenRouter 키로 대체하지 않음 |
| 기존 설정 | 원래 사용자·프로젝트 설정을 로드하며 파일을 다시 쓰지 않음 |
| 실행 범위 | 실행한 자식 프로세스에만 로컬 주소·호환 옵션 적용 |
| 장애 처리 | Pro HTTP4xx는 해당 요청에만 반환; 전송·응답 무결성 장애와 Pro5xx·OpenRouter 오류는 해당 제공자 경로 중단. 다른 제공자·모델로 자동 전환하지 않음 |
| 네이티브 advisor | 혼합 세션에서만 비활성화; 일반 Pro 세션의 저장된 설정은 유지 |

Fable 이용 가능 여부와 Usage credits 사용 여부는 Claude 앱·계정 플랜의 판단에 맡깁니다. 라우터는 Fable을 별도로 차단하거나 크레딧 OFF를 요구하지 않으며 계정의 크레딧 설정을 변경하지 않습니다. Sonnet의 DeepSeek 연결과 제공자 간 인증 분리·자동 API 대체 금지는 유지합니다.

## 해결한 오류

### 1. OpenRouter HTTP 400 이후 반복 503

기존 CLI가 Anthropic의 최신 inline-tools 형식인 중간 system 메시지의 `tool_addition` 등을 생성했습니다. 실제 CLI 요청의 역할·자료형·블록 종류를 모의 upstream으로 확인했고, OpenRouter의 해당 형식 거부를 별도 최소 요청으로 재현했습니다.

혼합 자식 세션에만 공식 호환 옵션을 적용했습니다.

```text
CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1
```

도구를 표준 root 정의로 제공하고 중간 system text의 순서를 유지합니다. system 메시지를 root로 이동하거나 도구 결과를 일반 텍스트로 바꾸는 변환은 채택하지 않았습니다. 실험 기능 비활성화는 inline-tools만의 변경이 아니므로, 모든 Anthropic 실험 기능이 원래와 동일하게 동작한다고 보증하지 않습니다.

### 2. 응답은 끝났지만 다음 대화가 503으로 차단됨

정상 `message_stop` 뒤의 선택적 `[DONE]` 표식을 JSON으로 파싱하면 오류가 나는 경우를 재현했습니다. 같은 청크와 분할된 종료 표식 사례 2개가 수정 전 실패하고 수정 후 통과했습니다.

- 검증된 시작·delta·stop 뒤의 종료 표식만 소비하며 CLI에 전달하지 않습니다.
- 종료 전 `[DONE]`, 이미 받은 종료 후 추가 메시지·오류, 잘린 응답은 계속 거부합니다.
- `message_stop`을 완료 경계로 삼습니다. 이후의 모든 청크를 기다리며 검사하는 구조는 아닙니다.
- 과거 실제 SyntaxError의 원문을 수집해 원인을 확정한 것은 아닙니다. 동일 증상의 재현과 수정 뒤 실제 후속 대화 성공을 확인했습니다.

### 3. Pro 모델 하나의 거부가 전체 Pro 경로를 중단함

Fable 실호출에서 Anthropic HTTP429 뒤 CLI가 재시도하다 180초 제한으로 종료됐습니다. 특정 플랜·크레딧·용량 중 어느 원인인지는 확인하지 못했습니다. 이후 Pro 전체가 막히는 문제는 코드와 모의 검사로 확인했으며 동일 세션의 실제 Opus 실패를 관측한 것은 아닙니다.

Pro HTTP4xx는 원래 상태를 해당 요청에 반환하고 이후 요청과 진행 중인 다른 Pro 스트림을 유지하도록 수정했습니다. 유효한 Retry-After만 전달하며 라우터 자체 재시도·모델 변경·API 대체는 없습니다. CLI의 동일 경로 재시도는 계속 가능합니다. Pro5xx·전송 장애·손상 응답·모델 불일치 및 OpenRouter 오류 중단은 유지합니다. 14개 회귀 검사가 수정 전 실패하고 수정 후 통과했습니다.

## 후보 저장소 검토

기존 구현을 기준으로 소스를 비교했으며 새 라우터로 교체하거나 설치하지 않았습니다.

| 후보 | 검토한 commit | 채택을 보류한 이유 |
| --- | --- | --- |
| [musistudio/claude-code-router](https://github.com/musistudio/claude-code-router) | `057abaa1f95519ab8faf999e1ccef2b80ccdeb9e` | 조사한 core에서 이번 inline-tools 문제의 변환 근거를 확인하지 못함. 외부 gateway 의존성 내부까지 검증한 것은 아님 |
| [1rgs/claude-code-proxy](https://github.com/1rgs/claude-code-proxy) | `5e45ba683ded931c1832cfca6468a791c6855e45` | 중간 system 지원과 도구 결과 보존이 요구와 맞지 않는 변환 경로 |
| [krzychdre/claude-local-proxy](https://github.com/krzychdre/claude-local-proxy) | `86ead843cf9ac96edca41d890a9f4a36986940d9` | 중간 system·미지원 블록·일부 제어 필드의 의미나 보존 문제 |
| [bborbe/claude-code-router](https://github.com/bborbe/claude-code-router) | `4cd12422d4113f89c7e3b3fea203c67b731e049d` | 중간 system을 root로 이동하는 처리와 미일치 모델의 default provider 전달을 그대로 채택할 수 없음 |

이는 해당 commit의 조사 범위에 대한 판단이며 저장소 전체나 향후 버전의 지원 여부를 단정하지 않습니다. CCR의 현재 OAuth 가져오기 지원도 확인했으므로, “CCR은 무조건 API 키만 지원한다”는 과거 설명은 정정합니다.

