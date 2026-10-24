# Claude Code: Sonnet을 OpenRouter 또는 Local LLM으로 사용하기

**한국어** | [English](README.en.md) · [MIT License](LICENSE)

기존 Claude Code의 **Plan 로그인과 사용자 설정을 사용하면서**, 호환되는 Sonnet 요청만 OpenRouter에서 지정한 모델 또는 지정한 Local LLM으로 보내는 로컬 라우터입니다. 외부 모델 요청이 실패하면 기존 Plan Sonnet으로 fallback하고, 연결이 복구되면 다시 외부 모델을 시도합니다.

**현재 상태:** 라우팅·재시도·fallback·복구·Local 설정은 구현했습니다. 다만 **모든 Claude 고유 기능을 유지하면서 모든 Sonnet 요청을 외부 모델로 대체하는 것은 아직 달성하지 못했습니다.** 지원되지 않는 기능이나 대화 이력이 포함된 요청은 원본 Plan 경로를 사용합니다. [검증 범위와 한계](#8-검증-방법과-남은-한계)를 먼저 확인하세요.

## 목차

1. [어떤 요청이 어디로 가나요?](#1-어떤-요청이-어디로-가나요)
2. [준비와 설정 파일 위치](#2-준비와-설정-파일-위치)
3. [OpenRouter 모델 시작하기](#3-openrouter-모델-시작하기)
4. [Local LLM으로 전환하기](#4-local-llm으로-전환하기)
5. [재시도·fallback·복구 원리](#5-재시도fallback복구-원리)
6. [실제로 사용한 모델 확인하기](#6-실제로-사용한-모델-확인하기)
7. [문제 해결과 일반 Claude로 돌아가기](#7-문제-해결과-일반-claude로-돌아가기)
8. [검증 방법과 남은 한계](#8-검증-방법과-남은-한계)
9. [파일 구성과 라이선스](#9-파일-구성과-라이선스)

## 1. 어떤 요청이 어디로 가나요?

```text
Claude Code — 기존 Plan 로그인·사용자 설정
  └─ 이 PC의 로컬 라우터
       ├─ 호환되는 Sonnet 요청 → 선택한 OpenRouter 모델 또는 Local LLM
       ├─ 외부 요청이 총 4회 실패 → 기존 Plan Sonnet
       └─ Opus·Haiku·Fable / 외부 미지원 기능 → 기존 Plan 경로
```

| 선택 또는 상황 | 실제 처리 경로 | 인증과 사용량 |
| --- | --- | --- |
| Sonnet + OpenRouter 모드 + 호환 요청 | `OPENROUTER_MODEL`로 지정한 모델 | OpenRouter 키와 별도 API 사용량 |
| Sonnet + Local 모드 + 호환 요청 | 직접 지정한 서버·모델 | 해당 서버 전용 인증과 사용 정책 |
| Opus·Haiku·Fable | Anthropic | 저장된 Plan OAuth와 계정 사용량 |
| Sonnet에 외부 미지원 기능·이력이 포함됨 | 원본 Plan Sonnet | Plan 계정 사용량 |
| 외부 제공자 장애 후 fallback | 원본 Plan Sonnet | Plan 계정 사용량 |

- Fable을 별도로 차단하거나 크레딧 OFF 입력을 요구하지 않습니다. 모델 접근 권한과 추가 사용 크레딧은 Claude 앱·계정 설정에 따릅니다.
- Anthropic용 유료 API 키로 자동 전환하는 경로는 없습니다. 다만 **계정에서 허용한 추가 사용 크레딧까지 라우터가 차단하는 것은 아닙니다.**
- 외부 제공자에는 Plan OAuth·계정 헤더를 보내지 않고 제공자 전용 인증을 새로 구성합니다.
- OpenRouter와 Local 사이를 자동으로 바꾸지 않습니다. 실행 시 하나를 선택하고, 장애 시 Plan Sonnet으로만 fallback합니다.
- 기존 MCP·플러그인·HUD·권한 설정을 사용합니다. 기존 서버의 인증 실패나 연결 장애를 자동으로 복구하는 기능은 아닙니다.

## 2. 준비와 설정 파일 위치

별도로 설치한 **Claude Code, Node.js, Python**과 기존 **Claude Plan 로그인**이 필요합니다. 현재 인증 검사는 `subscriptionType="pro"`인 계정을 대상으로 하며 Max 등 다른 플랜의 지원을 보장하지 않습니다. 이 소스에 필요한 추가 npm·pip 패키지는 없습니다.

Claude Code·Node.js·Python을 설치하고 각 버전과 호환성을 직접 확인하세요. 특정 개인 PC 구성이나 최소 지원 버전을 보장하지 않습니다.

```powershell
# 처음 내려받는 경우에만 실행
git clone https://github.com/yjj3019/claude-cheat.git
cd claude-cheat

# 도구 설치와 일반 Claude 로그인 상태 확인
node --version
python --version
claude --version
claude auth status
```

**설정 파일은 현재 작업 폴더가 아니라 `openrouterAdvisor.py`가 있는 폴더에서 읽습니다.**

| 실행 방식 | 읽는 설정 위치 |
| --- | --- |
| 내려받은 저장소에서 `.\startClaudeOpenRouter.ps1` 실행 | 해당 저장소의 `.env.openrouter` / `.env.local-llm` |
| 기존 PC의 전역 `startClaudeOpenRouter.ps1` 실행 | 전역 실행기가 가리키는 원본 실행기 폴더 |

전역 실행기를 따로 설치했다면 그 대상 폴더에서 설정을 읽습니다. 저장소를 clone해도 전역 명령이 자동 설치되거나 기존 실행기의 대상이 바뀌지 않습니다. 새 checkout은 해당 폴더의 실행기를 명시적으로 실행하세요.

`.env`는 한 줄에 `KEY=VALUE` 하나를 사용합니다. 빈 줄·`#`으로 시작하는 주석·값 전체를 감싼 따옴표는 허용하며, 여러 줄 값·셸 변수 확장은 지원하지 않습니다. 키 뒤에 같은 줄 주석을 붙이지 마세요. 실제 `.env*`는 Git에서 제외합니다. 예제에는 실제 인증값을 넣지 마세요.

## 3. OpenRouter 모델 시작하기

### ① 라우터 전용 키 파일 만들기

실행기 폴더에서 다음 파일을 열고 저장합니다. 이미 파일이 있으면 기존 내용을 먼저 확인하세요.

```powershell
notepad .env.openrouter
```

파일 내용:

```dotenv
ANTHROPIC_AUTH_TOKEN=YOUR_OPENROUTER_API_KEY
OPENROUTER_MODEL=REPLACE_WITH_OPENROUTER_MODEL_ID
```

`YOUR_OPENROUTER_API_KEY`를 본인의 실제 OpenRouter 키로 바꿉니다. 이 변수명은 **라우터가 파일에서 읽는 항목**입니다. Claude Code의 전역 환경변수에 OpenRouter 키를 넣거나 Plan 로그인을 API 키 로그인으로 바꾸라는 뜻이 아닙니다. 키를 화면·로그·커밋에 공유하지 마세요.

`OPENROUTER_MODEL`에는 [OpenRouter 모델 목록](https://openrouter.ai/models)의 구체적인 모델 ID를 입력합니다. 기존에 키만 설정했다면 이 항목을 추가해야 합니다. 누락·빈 값·예제 플레이스홀더는 시작 오류로 종료하며 다른 모델이나 Plan으로 자동 대체하지 않습니다. 이 파일의 두 항목만 읽고, 값은 프록시에만 전달합니다.

모델 선택이 가능해도 모든 모델의 도구 호출과 네이티브 기능을 보장하지 않습니다. 선택 모델이 [Messages API](https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-messages?explorer=true)와 필요한 도구 호출을 지원하는지 확인하세요. 응답의 모델 ID를 설정값과 정확히 비교하므로, 다른 ID로 응답하는 동적 별칭·프리셋은 실패할 수 있으며 미검증입니다. API 주소와 재시도 정책은 예제 파일에 고정값으로 구분했습니다.

### ② 실행하고 Sonnet 선택하기

```powershell
# 실행기 폴더에서 실행: 기본 제공자는 OpenRouter
.\startClaudeOpenRouter.ps1
```

Claude Code에서 `/model`로 **Sonnet**을 선택합니다. 호환되는 요청은 OpenRouter의 `https://openrouter.ai/api/v1/messages`로 보내며, 대체 모델은 `.env.openrouter`의 `OPENROUTER_MODEL`로 지정합니다. 특정 모델을 기본값으로 선택하지 않습니다.

기존 전역 실행기가 설치된 PC에서는 다른 작업 폴더에서도 다음처럼 실행할 수 있습니다.

```powershell
startClaudeOpenRouter.ps1
```

실행기는 해당 Claude 프로세스의 연결 주소만 로컬 라우터로 설정하고 기존 Plan 인증을 검사합니다. 저장된 로그인·사용자 설정을 API용 설정으로 덮어쓰지 않습니다. `/status`의 Plan 로그인 표시는 인증 상태이며 **현재 응답을 OpenRouter 모델이 만들었다는 증거는 아닙니다.**

라우팅 세션은 Claude Code의 실험 beta를 끄고 시작합니다(`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, `CLAUDE_CODE_DISABLE_ADVISOR_TOOL`). Claude Code 2.1.310이 기본으로 보내는 `tool_addition`·`context_management`·advisor 도구 요청은 외부 Messages API가 HTTP 400으로 거절하므로, 같은 세션의 Plan 요청도 안정 프로토콜로 전송됩니다. 그 결과 이 세션에서는 advisor·inline 도구·서버 문맥 편집이 사용되지 않습니다. thinking·effort 설정은 요청마다 따로 전달되며, 일반 모드에서는 아래 5절대로 외부 요청에 thinking을 요구하지 않습니다.

### OpenRouter 호환 모드: Plan 전환 차단

```powershell
.\startClaudeOpenRouter.ps1 -Provider OpenRouter -OpenRouterCompat
```

Claude Code 2.1.304 이상에서 `.env.openrouter`의 같은 키와 `OPENROUTER_MODEL`을 사용합니다. 주 모델·빠른 모델·하위 에이전트의 Claude 모델 선택을 모두 지정한 같은 OpenRouter 모델로 전달하며, Plan 로그인과 Plan fallback은 사용하지 않습니다. 기존 일반 실행과 Local 모드는 유지합니다.

LocalCompat과 같은 호환 처리로 플러그인·스킬·MCP·hooks·하위 에이전트 및 thinking·추론 강도 설정을 허용합니다. 사용자·프로젝트 설정과 기본 승인·거절 규칙을 유지하고, API 키 helper는 끕니다. 실행 설정으로 프록시 주소와 임시 인증을 고정하며 제공자 키는 프록시에만 전달합니다. 알려진 CLI 제어 필드만 변환하고, 지원하지 않는 beta·필드·토큰 계산 API는 명시적으로 거절합니다.

응답은 JSON 전체를 검증한 뒤 CLI용 SSE로 전달합니다. 요청당 120초·외부 시도 1회·오류 후 30초 재요청 대기를 적용하며 OpenRouter의 제공자 fallback도 차단합니다. 선택 모델의 Messages API·도구·thinking 지원과 정확한 응답 모델 ID가 필요합니다. 추론 강도 전달이 Sonnet과 동일한 추론 효과를 보증하지는 않습니다.

### 실행 전 연결 점검

모든 대화형 실행은 선택 제공자의 설정으로 최소 추론을 1회 점검한 뒤 결과를 출력합니다. 제한은 15초·출력 512토큰이며 OpenRouter 사용료가 발생할 수 있습니다. `connection_check`, `provider`, `inference_checked`, `model_confirmed`, `reply_confirmed`, `reason`과 필요한 HTTP `status`만 기록하고 주소·키·본문은 출력하지 않습니다.

점검 실패도 CLI 진입을 계속합니다. 일반 실행은 기존 Plan 라우팅과 fallback을 유지하고, `-LocalMode`·`-LocalCompat`·`-OpenRouterCompat`은 Plan 차단을 유지합니다. 실패 시 `provider_connection_unavailable`의 `action`은 `continue`, `plan_fallback`은 해당 모드 정책을 표시합니다. 이후 요청에서 제공자 연결을 다시 시도할 수 있습니다. 설정 누락·잘못된 설정·CLI 실행 불가 같은 시작 오류는 별도로 종료합니다.

`ready`는 모델 ID·정확한 점검 답변·정상 종료를 확인했다는 뜻입니다. `timeout`, `authentication_failed`, `http_error`, `connection_failed`는 접속 관련 실패이며, `reply_mismatch`, `incomplete_response`, `invalid_response`는 응답 검증 실패입니다. 특히 추론 모델이 512토큰을 소진하거나 다른 문구로 답하면 연결돼 있어도 점검은 실패할 수 있습니다. 이 점검이 전체 CLI 기능 호환성을 보증하지는 않습니다. 별도 `-CheckLocal`·`-DiagnoseLocal`의 진단 후 종료 동작은 유지합니다.

## 4. Local LLM으로 전환하기

### 지원하는 서버 형식

기존 서버가 **Anthropic 호환 `/v1/messages` API**를 제공해야 합니다. OpenAI 전용 `/v1/chat/completions` 또는 Ollama의 원시 `/api/chat`만 제공하는 서버를 변환해 주지는 않습니다. 서비스 이름만으로 호환 여부를 판단하지 말고 서버의 실제 API를 확인하세요.

모델 설치나 서버 기동은 이 실행기가 수행하지 않습니다. Local 모드에서는 OpenRouter 키를 읽지 않습니다.

### ① 예제 복사 후 주소·모델 지정

```powershell
# 기존 설정은 덮어쓰지 않음
if (-not (Test-Path -LiteralPath '.env.local-llm')) {
    Copy-Item -LiteralPath '.env.local-llm.example' -Destination '.env.local-llm'
}
notepad .env.local-llm
```

같은 PC의 서버 예시입니다. 모델 ID는 서버에 실제로 설치·제공된 값으로 바꾸세요.

```dotenv
LOCAL_LLM_URL=http://127.0.0.1:11434/v1/messages
LOCAL_LLM_MODEL=YOUR_INSTALLED_MODEL_ID
LOCAL_LLM_ALLOW_REMOTE=false
# 인증이 필요할 때만 서버 전용 키 설정
# LOCAL_LLM_API_KEY=YOUR_LOCAL_SERVER_KEY
```

다른 PC의 기존 서버를 사용할 때의 예시입니다. 아래 호스트는 실제 주소로 바꿔야 합니다.

```dotenv
LOCAL_LLM_URL=https://llm.example.com/v1/messages
LOCAL_LLM_MODEL=YOUR_SERVER_MODEL_ID
LOCAL_LLM_ALLOW_REMOTE=true
# LOCAL_LLM_API_KEY=YOUR_SERVER_KEY
```

| 항목 | 기본값 / 동작 |
| --- | --- |
| `LOCAL_LLM_URL` | `http://127.0.0.1:11434/v1/messages`; 경로는 `/v1/messages`여야 함 |
| `LOCAL_LLM_MODEL` | 실행 시 필수; 서버의 정확한 모델 ID |
| `LOCAL_LLM_API_KEY` | 선택; 해당 서버 전용 `Authorization: Bearer`로 전달 |
| `LOCAL_LLM_ALLOW_REMOTE` | `false`; 원격 서버는 명시적 `true`와 HTTPS 필요 |
| `LOCAL_LLM_HTTP_URL` | Optional exact HTTP Messages URL pin; empty by default. Plaintext HTTP is allowed only when it exactly matches `LOCAL_LLM_URL`. Keep real values in ignored `.env.local-llm`; prefer HTTPS. |
| `LOCAL_LLM_MODELS_URL` | 같은 origin의 `/v1/models`; 모델 목록 점검에만 사용 |

같은 이름의 **프로세스 환경변수가 파일보다 우선**합니다. `localhost` 대신 허용되는 주소 리터럴 `127.0.0.1` 또는 `[::1]`을 사용하세요. 원격 HTTP는 `LOCAL_LLM_HTTP_URL`이 `LOCAL_LLM_URL`과 정확히 일치할 때만 허용하며, 이 경우 프롬프트와 `LOCAL_LLM_API_KEY` Bearer 키가 암호화 없이 전송됩니다. 그 외에는 HTTPS 또는 별도로 준비한 loopback 터널이 필요합니다.

URL 내 사용자명·비밀번호·쿼리·fragment, redirect, 잘못된 인증 헤더는 거부하고 TLS 인증서 검증을 끄지 않습니다. 원격 허용은 지정한 DNS 이름과 해석 결과를 신뢰한다는 뜻입니다.

### ② 모델 목록만 사전 점검

```powershell
.\startClaudeOpenRouter.ps1 -CheckLocal
```

이 명령은 Local 모델 목록에 **GET만** 보냅니다. Claude 실행·Plan 인증·추론·도구 실행·fallback을 하지 않습니다. 환경 프록시와 redirect를 사용하지 않으며 응답 크기는 1MiB, 소켓 타임아웃은 5초입니다. 전체 실행 시간이 반드시 5초 이하라는 뜻은 아닙니다.

성공 출력 예시이며, 실제 서버를 검사한 결과는 아닙니다.

```json
{"local_check":true,"inference_checked":false,"model_count":2,"configured_model_found":true,"reason":"ready_metadata_only"}
```

| 결과 `reason` | 의미와 확인할 사항 |
| --- | --- |
| `ready_metadata_only` | 모델 목록에 지정한 ID 존재; 추론은 아직 미검증 |
| `model_not_found` / `model_not_configured` | 모델 ID 불일치 / 미설정 |
| `connection_failed` | 서버·포트·TLS·연결 상태 확인 |
| `http_error` / `redirect_rejected` | HTTP 상태·인증·최종 URL 확인 |
| `invalid_model_list` / `response_too_large` | 목록 응답 형식 또는 크기 제한 확인 |
| `invalid_configuration_or_response` | URL·설정값·응답 파싱 확인 |

모델 목록 응답은 `data[].id` 또는 `models[].name`/`models[].model` 형식을 읽습니다. Ollama의 목록을 쓰는 경우 같은 scheme·host·port의 `/api/tags`를 `LOCAL_LLM_MODELS_URL`로 지정할 수 있습니다. **목록 조회 성공만으로 Messages API·스트리밍·도구 호출까지 지원한다고 판단하면 안 됩니다.** 목록 API가 없는 서버는 이 점검을 생략할 수 있지만 실제 추론 검증이 별도로 필요합니다. 점검 실패는 종료 코드 1을 반환하며 Plan로 대신 연결하지 않습니다.

### ③ Local 모드로 실행

```powershell
.\startClaudeOpenRouter.ps1 -Provider Local
# 기존 전역 실행기가 설치된 PC에서는 다음 명령도 가능
# startClaudeOpenRouter.ps1 -Provider Local
```

다시 OpenRouter를 쓰려면 세션을 종료하고 기본 명령으로 재실행합니다. 실행 중인 세션에서 `/model`은 Claude 모델을 선택하며 OpenRouter/Local 제공자를 바꾸는 메뉴가 아닙니다.

### Claude CLI Local 호환 모드

```powershell
.\startClaudeOpenRouter.ps1 -Provider Local -LocalCompat
```

Claude Code 2.1.304 이상에서 플러그인·스킬·MCP·hooks·하위 에이전트와 추론 설정을 사용하는 별도 선택 모드입니다. 일반 설정과 프로젝트 지침을 로드하며 기본 승인·거절 규칙을 유지합니다. API 키 helper는 비활성화하고, 실행 시 설정으로 프록시 주소와 임시 인증을 고정합니다. 기존 실행과 아래 `-LocalMode`의 제한은 바뀌지 않습니다. `-LocalCompat`은 `-LocalMode`·진단 옵션과 함께 사용할 수 없습니다.

주 모델·빠른 모델·하위 에이전트의 Claude 모델 선택은 **모두 설정한 같은 Local 모델**로 전달합니다. Plan 로그인과 Plan fallback은 사용하지 않습니다. `/effort low`·`/effort high`와 thinking 설정은 서버에 보존해 전달하지만, 제3자 모델의 추론 효과가 Sonnet과 같다는 의미는 아닙니다. 지원하지 않는 beta·본문 필드·토큰 계산 API는 명시적으로 거절합니다.

현재 서버는 SSE 응답의 모델 식별자가 설정과 일치하지 않아 JSON 응답을 완전히 검증한 뒤 CLI용 SSE로 변환합니다. 실시간 토큰 전달은 제공하지 않으며 120초 전체 요청 제한·1회 외부 시도·30초 재요청 대기를 유지합니다. 서명과 공급자 메타데이터는 새로 만들지 않고 보존합니다. 모든 네이티브 서버 기능과 임의의 플러그인 조합을 지원한다고 보장하지 않습니다. [실서버 검증 범위](docs/LIVE-VALIDATION.ko.md#local-호환-모드-검증-2026-10-24)를 참고하세요.

### Claude CLI Local 전용 모드

```powershell
.\startClaudeOpenRouter.ps1 -Provider Local -LocalMode
```

일반 Claude CLI를 Local 서버로 실행하는 별도 모드입니다. Claude Code **2.1.304 이상**이 필요하며, 2.1.304로 검증했습니다. 이전 버전은 프록시와 세션을 시작하기 전에 차단합니다. `--safe-mode`에서 Bash/Read/Edit/Write/Glob/Grep를 제공하며, 기본 권한 확인(`default`)과 기존 사용자·프로젝트의 deny/ask 규칙을 유지합니다. 권한 우회나 Auto 모드를 켜지 않습니다. 기존 실행 명령은 그대로 유지합니다.

기존 `--bare`의 세 도구 제한을 해결했습니다. 2026-10-23 실제 CLI·Local 서버에서 Write/Glob/Grep의 영문·공백·한글·한글과 공백 경로 조합을 확인했습니다. [호환성 개선과 검증 결과](docs/LIVE-VALIDATION.ko.md#local-모드-파일-도구-호환성-개선-2026-10-23)를 참고하세요.

새 Local 세션에는 실행 당시 작업 디렉터리를 기본 system 지침에 추가해, 자동 압축 후에도 상대 경로의 기본 기준을 명시합니다. 문맥에 등장하는 이력·설정·로그 경로는 이 기준을 바꾸지 않으며, 사용자가 명시한 다른 기준 디렉터리는 해당 요청에 적용합니다. 적용하려면 같은 명령으로 새 Local 세션을 시작하세요. [상대 경로 검증과 범위](docs/LIVE-VALIDATION.ko.md#local-모드-자동-압축-후-상대-경로-기준-개선-2026-10-23)를 참고하세요.

이 세션에서는 네이티브 beta·advisor·thinking을 제한합니다. Safe 모드로 사용자·프로젝트의 hooks·플러그인·skills·subagents·CLAUDE.md 자동 로딩을 생략하고, 빈 strict MCP 설정과 MCP 도구 차단, hooks·자동 메모리·IDE·Chrome·배경 작업 제한을 적용합니다. API 키 helper도 이 실행에서 비활성화합니다. 필요한 프로젝트 지침은 명시적으로 제공하세요. 관리 정책의 hooks·명령 예외는 CLI 정책을 따르며 이번 검증 범위에 포함하지 않았습니다. [공식 CLI 문서](https://code.claude.com/docs/en/cli-reference)를 참고하세요.

Plan 로그인을 조회하지 않으며 CLI에는 로컬 프록시용 임시 Bearer 토큰만 전달합니다. Local 서버 키는 프록시만 보유합니다. 미지원 기능이나 Local 실패를 Plan으로 전환하지 않고 명시적으로 거절합니다. 프록시의 외부 시도는 한 번이며 CLI 자체 재시도는 별도입니다. CLI의 `system` 역할 컨텍스트 메시지(비어 있지 않은 문자열 또는 text 블록)는 모든 모드에서 역할·내용을 그대로 전달합니다. 도구·thinking 블록이 섞인 system 메시지와 알 수 없는 메시지 필드는 계속 거절합니다.

Local 실패 안내는 아래 코드로 구분합니다. 실패 뒤 기본 대기시간은 30초이며, 안내에 남은 초를 올림해 표시합니다. 대기가 끝나면 새 요청으로 복구를 확인하세요. 실패한 작업이 자동으로 완료되지는 않습니다.

| 코드 | 의미와 조치 |
|---|---|
| `local_timeout` | 요청 시간 초과. 대기 후 새 요청으로 확인 |
| `local_authentication_failed` | 서버가 401/403 반환. Local 키와 접근 권한을 확인하고, 인증 설정을 바꿨다면 세션 재시작 |
| `local_empty_response` | 비어 있거나 공백뿐인 응답. 서버 응답 상태 확인 |
| `local_upstream_error` | 그 밖의 연결·상태·응답 검증 실패. 라우팅 로그의 고정 오류 분류 확인 |
| `local_cooldown` | 재요청 대기 중. 직전 실패 원인과 남은 대기시간 표시 |
| `local_recovery_in_progress` | 다른 요청이 복구 확인 중. 해당 요청이 끝날 때까지 대기 |

CLI의 후속 요청 때문에 최초 오류 대신 대기 안내가 마지막에 표시될 수 있어, 대기 안내에도 직전 원인을 남깁니다. `local_mode_rejected` 로그의 `code`, `retry_after_seconds`, `previous_error_code`로도 확인할 수 있습니다. 복구 확인 중에는 남은 시간을 임의로 표시하지 않습니다. 오류 응답의 HTTP 400·`api_error` 형식과 기존 CLI 재시도 정책은 유지하며, 안내에 서버 응답 원문·주소·키를 포함하지 않습니다.

추론을 시작하기 전의 실패는 `openrouter_start_failed` 이벤트에 고정 `code`와 조치 안내를 추가합니다. 종료 코드는 1이며, 원문 예외·주소·키는 출력하지 않습니다. 이 안내는 `-LocalMode`에 적용하고 일반 실행·진단 모드는 유지합니다.

| 시작 오류 코드 | 확인할 내용 |
|---|---|
| `local_cli_not_found` / `local_node_not_found` | Claude Code·Node.js 설치와 PATH |
| `local_cli_unsupported` / `local_cli_version_check_failed` | `claude --version`, 최소 2.1.304, 실행 권한 |
| `local_configuration_invalid` | `.env.local-llm`과 환경변수의 URL·HTTP 허용·모델·키 형식·모델 목록 origin |
| `local_output_budget_invalid` | `CLAUDE_CODE_MAX_OUTPUT_TOKENS`가 양의 정수인지 확인 |
| `local_proxy_start_failed` | Node.js·프록시 파일·루프백 접근 권한. 준비 제한 시간은 기존 15초 유지 |
| `local_cli_start_failed` | Claude Code 실행 권한과 설치 상태 |
| `local_process_cleanup_failed` | Python·Windows Job 권한과 감시 파일 확인. 정리 준비 실패 시 CLI를 시작하지 않음 |

Windows에서 기존 명령의 `-LocalMode` 진입점은 CLI·프록시 생성 전에 실행기를 [Windows Job Object](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)에 넣습니다. 창을 띄우지 않는 감시 프로세스 하나가 실행기 종료를 확인하면 Job을 닫아 CLI·프록시·Bash와 그 자식도 정리합니다. PID 이름 검색이나 전역 프로세스 종료를 사용하지 않습니다. Job 준비에 실패하면 관리되지 않는 CLI를 시작하지 않습니다.

기존 부모 파이프 감시도 유지합니다. 일반 실행·진단 모드와 모듈을 import해 호출하는 경로에는 새 Job을 설치하지 않으며, 비 Windows 환경의 종료 동작은 기존대로입니다. 새 보호는 실행 중인 세션에 소급 적용되지 않으므로 Local 세션을 새로 시작하세요. 일반 종료와 Ctrl+C를 우선 사용하고, [프로세스 트리 검증 범위](docs/LIVE-VALIDATION.ko.md#windows-local-프로세스-트리-정리)를 참고하세요.

출력 예산 부족으로 압축이 중단되지 않도록 이 모드의 실행 시 설정에 `CLAUDE_CODE_MAX_OUTPUT_TOKENS`를 기본 8,192로 지정합니다. 부모 프로세스에서 지정한 양의 정수 예산이 더 크면 유지하고, 더 작으면 8,192로 올립니다. 이 실행 시 설정은 사용자·프로젝트 파일의 예산보다 우선하므로, 더 큰 예산은 부모 환경변수로 지정하세요. 파일이나 부모 환경은 수정하지 않습니다. 압축뿐 아니라 이 Local 세션의 일반 출력에도 적용됩니다. CLI의 모델별 상한과 컨텍스트 여유에 따른 조정은 그대로 적용하며, 프록시에서 `max_tokens`를 강제로 올리지 않습니다. 서버도 해당 출력 예산을 지원해야 합니다. `-LocalMode`가 없는 기존 실행과 진단 모드는 바뀌지 않습니다.

Local 응답은 JSON으로 받아 설정한 모델을 검증한 뒤 CLI용 SSE로 변환합니다. 도구 인자·순서·종료 사유·사용량을 보존하고, 서버 전용 `tool_use.provider_specific_fields`는 object/null일 때만 보존합니다. CLI가 후속 이력에서 이 필드를 생략할 수 있으며, 해당 상태가 필수인 서버는 별도 검증이 필요합니다. 모르는 응답 필드나 thinking 블록은 삭제하지 않고 거절합니다. `interleaved-thinking-2025-05-14`·`effort-2025-11-24` 호환 헤더 2개만 이 모드에서 추가 허용합니다.

새 Local 세션으로 시작하세요. 기존 Plan 대화의 네이티브 이력은 재개하지 마세요. Local 모드의 요청 제한 시간은 긴 대화 압축을 위해 120초이며, 일반 모드는 무응답 30초 기준을 유지합니다(위 5절 참고). 토큰 수 계산 API는 지원하지 않습니다. 긴 대화의 압축은 서버와 CLI 버전에 따라 별도 검증해야 합니다. [압축 검증 절차](docs/LIVE-VALIDATION.ko.md#local-모드의-긴-대화압축-점검)를 참고하세요. `-CheckLocal`·`-DiagnoseLocal`과 함께 사용할 수 없습니다. 원래 환경은 종료 후 `-LocalMode` 없이 다시 실행하면 됩니다.

### Local 실제 추론 진단

```powershell
.\startClaudeOpenRouter.ps1 -Provider Local -DiagnoseLocal
```

기존 실행과 분리된 진단 모드입니다. Claude CLI를 실행하지 않고 beta·도구·대화 이력 없이 고정 합성 질문을 Local Messages API에 한 번 보냅니다. 출력은 최대 512토큰, 전체 요청은 60초로 제한하며 재시도·Plan fallback·리다이렉트를 하지 않습니다. 기존 주소 검증과 HTTP 허용 설정을 그대로 적용합니다. 실제 추론이므로 서버 자원을 사용합니다.

응답 모델이 설정과 일치하고, `LOCAL_DIAGNOSTIC_OK`라는 정확한 문구로 답하며 `end_turn`으로 종료해야 성공입니다. 성공 시 `local_diagnostic`, `inference_checked`, `model_confirmed`, `reply_confirmed`가 모두 `true`입니다. 실패하면 고정 오류 분류만 출력하고 종료 코드 1을 반환합니다. 이 결과는 최소 Messages 추론 검증이며 일반 CLI의 전체 기능 호환성을 보증하지 않습니다. 모델 목록만 조회하는 `-CheckLocal`과는 함께 사용할 수 없습니다.

## 5. 재시도·fallback·복구 원리

```text
호환 Sonnet 요청
  → 외부 제공자 최초 시도 + 재시도 3회 = 최대 4회
  → 401·402·403·404는 재시도 없이 즉시 Plan + 30초 대기, 400·413·422는 해당 요청만 Plan(대기 없음)
  → 외부로 보내는 요청에서 `metadata`(계정·세션 식별자)와 이전 응답의 서명된 thinking 블록은 제거, 일반 모드에서는 thinking을 요청하지 않음(외부 응답에 thinking이 오면 그 요청만 Plan)
  → Plan 전달은 스트림 요청의 헤더 120초·청크 간 무응답 300초를 넘기면 끊음, 요청 본문 상한은 32MiB
  → 모두 실패하면 원본 Plan Sonnet으로 1회 전달
  → 이후 30초 동안 들어오는 요청은 Plan로 전달
  → 30초 후 호환되는 실제 요청 1개로 외부 복구 확인
      ├─ 성공: 외부 모델 사용 재개
      └─ 실패: Plan fallback 및 대기 기간 재시작
```

- 일반 모드의 외부 시도는 **무응답 30초**(헤더 대기·청크 간 간격)를 넘기면 실패하고, 응답이 계속 오는 동안은 최대 300초까지 기다립니다. 재시도 간격은 **1초**이며, HTTP 429의 `Retry-After`가 더 길면 그 값(최대 10초)을 따릅니다. 네 번 모두 무응답이라면 Plan 요청 전까지 대략 123초가 걸릴 수 있습니다. Local·compat 모드는 기존의 고정 총 시간 제한을 유지합니다. Plan 응답 시간이나 CLI 자체 재시도는 여기에 추가될 수 있습니다.
- 대기 기간이 지나도 백그라운드 진단 요청은 보내지 않습니다. 다음 호환 요청으로 복구를 확인하는 동안 다른 요청은 Plan로 보냅니다.
- Plan 요청까지 실패하면 그 오류를 전달합니다. 다른 API 키·제공자로 연쇄 전환하거나 무한 반복하지 않습니다. CLI 자체의 재시도 표시는 라우터 내부의 재시도 횟수와 다를 수 있습니다.
- 외부 응답은 완결성·실제 응답 모델·도구 JSON 등을 검증한 뒤 전달합니다. 외부 스트리밍 응답도 최대 32MiB까지 버퍼링하므로 첫 표시가 늦어질 수 있습니다. 실패한 부분 텍스트나 도구 호출을 전달한 뒤 중간에 Plan 응답을 이어 붙이지 않습니다.
- 요청을 취소하면 재시도와 fallback도 중단합니다. 재시도된 외부 추론은 별도 사용량이 발생할 수 있습니다.
- Plan 전용 모델·토큰 수 계산처럼 외부가 표현할 수 없는 요청은 **처음부터 Plan로 우회**합니다. 라우팅 세션이 실험 beta를 끄므로 CLI는 advisor·inline 도구 요청을 만들지 않습니다. `system` 역할 컨텍스트 메시지(text 블록 포함)와 CLI가 상시 보내는 `interleaved-thinking-2025-05-14`·`effort-2025-11-24`·`context-1m-2025-08-07` beta는 모든 모드에서 허용합니다. 제공자가 붙인 `citations`는 응답에 포함되어 다음 요청 이력으로 다시 전송되므로 입력에서도 허용합니다. 이전 응답의 서명된 thinking 블록은 외부 사본에서만 제거하므로, 한 번 Plan으로 우회한 뒤에도 다음 요청은 다시 외부로 갈 수 있습니다. 이 우회는 외부 장애나 복구 성공으로 세지 않습니다.

## 6. 실제로 사용한 모델 확인하기

Sonnet 선택과 HUD의 모델·비용 표시는 실제 외부 라우팅이나 청구를 보증하지 않습니다. 라우터는 외부 응답의 모델을 검증한 뒤 CLI에 보여주는 모델 이름을 원래 요청 이름에 맞춥니다. **요청별 라우팅 로그와 제공자 사용 기록을 함께 확인하세요.**

로그 예시:

```json
{"event":"route_selected","provider":"openrouter","reason":"replacement"}
{"event":"route_selected","provider":"plan","reason":"native_feature","detail":"unsupported_beta"}
```

라우터 로그(`route_selected` 등)는 기본적으로 숨겨지며 `-Debug`(또는 `python openrouterAdvisor.py --debug`)를 지정할 때만 터미널 stderr에 표시됩니다. 프록시 시작 오류와 `provider_connection_unavailable`은 `-Debug` 없이도 출력됩니다. 라우팅을 확인하려면 `-Debug`로 실행하세요.

| 로그 항목 | 의미 |
| --- | --- |
| `route_selected` + `openrouter` 또는 `local` + `replacement` | 외부 응답 검증을 통과한 대체 경로 |
| `route_selected` + `plan` + `native_feature` | 기존 기능·이력 등을 보존하기 위한 Plan 경로 |
| `route_selected` + `plan` + `fallback` | 외부 시도 실패 후 Plan 경로 |
| `route_selected` + `plan` + `circuit_open` | 외부 대기 기간 또는 복구 확인 중 Plan 경로 |
| `external_attempt_failed` | 외부 시도 실패; 시도 번호·상태·오류 분류 확인 |
| `circuit_open` / `circuit_recovered` | 외부 장애 대기 시작 / 외부 복구 확인 |

라우터 자체 진단 로그는 키·요청 본문을 남기지 않는 고정 메타데이터를 사용합니다. 외부 제공자로 보내는 요청에는 실제 대화·코드·도구 정의와 결과가 포함될 수 있습니다. 이 로깅 정책이 외부 서비스의 보관 정책까지 바꾸는 것은 아닙니다.

`native_feature`의 `detail`은 처음 확인된 제외 사유입니다. `token_count`는 토큰 수 계산, `plan_model`은 Plan 전용 모델, `unsupported_beta`는 미지원 beta 헤더입니다. 나머지 `unsupported_*`는 요청 필드·메시지 형식·thinking/output/tool_choice 설정·콘텐츠·도구 정의 검사의 실패를 구분합니다. 로그는 고정 분류명만 기록하며 헤더 원문·질문·키를 기록하지 않습니다. `unsupported_beta`의 `beta_features`는 날짜 접미사가 있는 기능 식별자를 최대 16개 표시하고, `unsupported_beta_count`는 전체 개수를 표시합니다. 비표준 값과 인증정보 형태의 값은 마스킹합니다.

### 실시간 대시보드

```powershell
.\startClaudeDashboard.ps1
```

`127.0.0.1`에만 열리는 **읽기 전용** 페이지로, 2초마다 자동 갱신됩니다. 기본 포트는 8787이며 사용 중이면 빈 포트로 자동 대체되고 실제 주소를 출력합니다.

| 항목 | 내용 |
| --- | --- |
| 지금 상태 | 마지막 `route_selected` 기준 **OpenRouter 사용 중 / Plan 우회 중 / 유휴**, **실제 사용 모델**, 경과 시간, 사유 |
| 경고 배너 | 최근 10분에 Plan 우회 50% 이상, 회로 차단, 400·413·422 거부, 잔액 10% 이하일 때 표시 |
| 최근 60분 | OpenRouter 대 Plan 비율, 회로 차단 횟수 |
| 우회 사유 | `native_feature`의 `detail`별 건수, 외부 실패 상태·검증 실패 분류 |
| 크레딧 | 키 한도·이번 달 사용액·잔액·리셋일 (OpenRouter `/api/v1/key`) |
| 오늘 토큰 | OpenRouter·Plan 토큰, 입력/출력, 캐시, **추정 비용** |
| 시간대별 토큰 | 최근 24시간을 OpenRouter·Plan 막대로 표시 |
| 세션별 사용 | 턴 수, 외부/Plan 턴 수, 외부 토큰, **추정 비용**, 사용 모델 |

추정 비용은 OpenRouter `/api/v1/models`의 모델 단가로 계산하며, 실제 청구는 제공자 기록을 기준으로 확인하세요. **실제 사용 모델**은 프록시가 응답을 전달하기 직전에 기록합니다. CLI가 보여주는 모델 이름은 요청 별칭(`claude-sonnet-5-5` 등)이므로, 실제 제공자 모델은 이 값으로 확인하세요.

키·프롬프트·요청 본문은 표시하지 않고 서버는 아무것도 기록하지 않습니다. 대시보드는 세션과 별개 프로세스이므로 라우팅 세션과 함께 띄워 두면 됩니다.

### 스크립트·에이전트용 상태 API

페이지를 열지 않고도 한 번에 상태를 확인할 수 있습니다.

```powershell
python routeDashboard.py --status           # 한 줄 JSON + 종료 코드
python routeDashboard.py --status --pretty  # 읽기 좋게
python routeDashboard.py --status --usage    # 토큰·비용까지 집계(느림)
python routeDashboard.py --status --offline  # OpenRouter 호출 생략
```

| 종료 코드 | 의미 |
| --- | --- |
| 0 | 정상 (외부 경로 사용 중이거나 유휴) |
| 2 | 현재 Plan 경로 |
| 3 | 심각 경고 (회로 차단·잔액 부족 등) |
| 4 | 라우팅 기록 없음 (아직 실행기 세션 없음) |

출력에는 `state`, `provider`, `reason`, `detail`, **`model`**, `last_event_age_seconds`, `window_counts`, `plan_reasons`, `failures`, `alerts`, `account`가 포함됩니다. 대시보드를 띄워 둔 경우 `GET /api/status`로 같은 내용을 받습니다. 두 경로 모두 키·프롬프트·요청 본문을 포함하지 않습니다.

## 7. 문제 해결과 일반 Claude로 돌아가기

| 증상 | 먼저 확인할 내용 |
| --- | --- |
| `.env.openrouter`를 찾지 못함 | 현재 작업 폴더가 아닌 실제 실행기 폴더에 저장했는지 확인 |
| OpenRouter 401 / `Missing Authentication header` | 전용 파일의 실제 키·유효성을 확인; HUD 플러그인 설치 성공은 API 인증 성공과 별개 |
| Local 설정을 바꿔도 이전 서버 사용 | 프로세스 환경변수 우선순위와 전역 실행기 대상 확인 후 세션 재시작 |
| Local 모델 목록 성공인데 추론 실패 | Messages API·모델 ID·도구/응답 형식 호환성을 별도 확인 |
| Sonnet 선택 후 계속 Plan 사용 | `-Debug`로 `route_selected` 확인; `native_feature`는 Plan 전용 모델·토큰 계산·네이티브 이력 때문에 정상일 수 있음. `unsupported_beta`가 반복되면 CLI가 라우터가 모르는 beta를 추가한 것이므로 실행기를 갱신 |
| `503 ... restart required`가 반복됨 | 과거 실행기/프록시 잔존 여부·첫 upstream 오류를 확인하고 최신 실행기로 새 세션 시작; 후속 503만으로 원인 단정 금지 |
| HUD·MCP 일부가 동작하지 않음 | 일반 Claude에서도 같은지 비교; 설정 로드와 서버 연결·권한 정상 여부는 별도 |
| 응답 전 대기가 길어짐 | 외부 4회 시도와 버퍼링 여부 확인; 사용자가 취소하면 후속 시도도 중단 |

라우팅 세션은 판단 근거를 실행기 폴더의 `.route.log`에 함께 남깁니다. 세션을 시작할 때마다 `proxy start` 줄이 추가되고, `route_selected`·`external_attempt_failed`·`circuit_open` 같은 이벤트가 시각과 함께 쌓입니다. `-Debug`를 켜지 않은 지난 세션도 이 파일로 확인할 수 있으며, `*.log` 규칙으로 Git에서 제외됩니다.

코드나 설정을 수정한 뒤에는 라우팅 세션을 종료하고 다시 실행해야 합니다. 일반 Plan 환경으로 돌아가려면 **깨끗한 새 터미널**에서 평소처럼 실행하세요.

```powershell
claude
```

이 실행기는 부모 터미널의 전역 설정을 바꾸지 않습니다. 과거에 수동으로 설정한 `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_CONFIG_DIR` 등이 사용자/시스템 환경변수에 남아 있다면 새 터미널도 이를 상속하므로 별도로 확인해야 합니다. 로그인 파일이나 기존 MCP·사용자 설정 전체를 삭제하지 마세요.

## 8. 검증 방법과 남은 한계

공개 저장소에는 개인 PC 구성, 계정 사용량, 청구 내역, 실제 세션 로그를 보관하지 않습니다. 아래 명령은 합성 데이터와 로컬 모의 서버로 검사하며 외부 모델을 호출하지 않습니다.

```powershell
node --test tests/*.test.mjs
python -m unittest discover -s tests -p 'test_*.py' -v
python openrouterAdvisor.py --self-test
node --check claudeRouteProxy.mjs
```

검사 대상은 요청·응답 보존, 인증 격리, 도구 결과 왕복, 최초 요청과 재시도 3회, Plan fallback, 회로 복구, 취소·타임아웃·빈 응답 처리입니다. 합성 테스트 통과를 실제 서비스나 모든 Claude Code 기능의 통과로 해석하면 안 됩니다.

- 실제 Local 서버의 인증·추론·도구 호출은 별도 검증이 필요합니다.
- 미지원 네이티브 기능이나 서명된 이력이 있으면 Plan 경로를 사용합니다. 따라서 외부 모델 사용률이 0%일 수 있고, 같은 대화에서 외부 경로로 돌아오지 못할 수도 있습니다.
- 외부 추론의 네이티브 advisor만 별도로 구독 경로에서 실행하는 기능은 구현하지 않았습니다. custom `ANTHROPIC_BASE_URL`에서 원격 managed settings·organization policy를 포함한 모든 네이티브 기능의 동등성을 보장하지 않습니다.
- 요청 본문은 32MiB로 제한하며 초과 시 fallback 전에 HTTP 413을 반환합니다. 버전이 숫자로 시작하는 `claude-sonnet-*`, `claude-opus-*`, `claude-haiku-*`, `claude-fable-*`, 설정한 OpenRouter 모델 ID와 과거 클라이언트 모델 별칭을 허용하며, 미등록 모델은 HTTP 403으로 거부합니다.
- 경로는 POST `/v1/messages`, `/v1/messages/count_tokens`(선택적 `?beta=true`)와 로컬 확인 HEAD `/api/hello`만 허용합니다. 다른 경로는 구독으로 우회하지 않고 거부합니다.
- 사용자 지정 `CLAUDE_CONFIG_DIR`는 유지하되, 미설정 또는 과거 전용 `~/.claude-openrouter`이면 자식에서 `~/.claude`를 사용합니다. 과거 폴더의 MCP·플러그인을 자동 이전하거나 폴더를 복사·삭제하지 않습니다.
- Local·compat 격리 세션의 임의 토큰(nonce)은 Claude Code 프로세스의 `--settings` 인자와 환경변수에 들어갑니다. 같은 사용자 권한의 프로세스(명령줄을 수집하는 EDR·감사 도구 포함)가 읽을 수 있고, 유출되면 해당 세션 동안 로컬 프록시를 통해 OpenRouter/Local 사용량을 소비할 수 있습니다. 세션마다 새로 생성하며 `127.0.0.1`에만 바인드합니다. Plan 접근 권한은 주지 않습니다.
- OpenRouter 응답은 `id`·`role=assistant`·토큰 `usage`가 없거나 SSE `event:` 이름이 JSON `type`과 다르면 거부(재시도 후 Plan)합니다. 기본 모델에서 2026-10-24 실호출로 이 형태를 확인했고(스트림·비스트림 각 1회), 다른 모델은 확인하지 못했습니다. Local 응답에는 메타데이터 형태를 강제하지 않으며 `event:` 이름 일치만 확인합니다.
- `claude`가 npm 설치의 `.cmd`/`.bat` shim으로 해석되면 격리 모드(Local·OpenRouter compat)는 경고를 출력합니다. cmd.exe가 shim을 거치는 인자의 따옴표·`&`·`%`를 잘못 해석할 가능성이 있으나 실제 재현은 하지 못했습니다. 네이티브 `claude.exe` 설치를 권장합니다.
- HTTP 429가 발생하면 해당 계정의 Usage 화면과 응답을 직접 확인해야 합니다. 특정 계정의 한도·초기화 일정은 저장소에서 관리하지 않습니다.

일반적인 검증 절차는 [연결·장애 복구 점검](docs/LIVE-VALIDATION.ko.md)을 참고하세요.

## 9. 파일 구성과 라이선스

| 파일 | 역할 |
| --- | --- |
| [startClaudeOpenRouter.ps1](startClaudeOpenRouter.ps1) | 제공자 선택과 Python 실행기 호출 |
| [openrouterAdvisor.py](openrouterAdvisor.py) | Plan 인증 검사·환경 격리·프록시 수명 관리·Local 목록 점검 |
| [localProcessTree.py](localProcessTree.py) | Windows Local 전용 진입점의 Job 소유권·프로세스 트리 종료 감시 |
| [claudeRouteProxy.mjs](claudeRouteProxy.mjs) | 모델별 라우팅·응답 검증·재시도·fallback·복구 |
| [.env.local-llm.example](.env.local-llm.example) | 키가 없는 Local 설정 예제 |
| [tests/claudeRouteProxy.test.mjs](tests/claudeRouteProxy.test.mjs) | 프록시 합성 회귀 검사 |
| [tests/localLlmIntegration.test.mjs](tests/localLlmIntegration.test.mjs) | loopback HTTP 서버를 사용하는 합성 통합 검사 |
| [tests/test_local_config.py](tests/test_local_config.py) | Local 설정과 메타데이터 점검 검사 |
| [tests/test_local_lifecycle.py](tests/test_local_lifecycle.py) | Windows 합성 CLI·실행기 강제 종료 후 프록시·연결 정리 검사 |
| [docs/HISTORY.ko.md](docs/HISTORY.ko.md) | 이전 구현·진단 기록; 현재 정책과 구분 |

`openrouterAdvisor.py`라는 이름은 이전 구현에서 유지한 이름입니다. 이 파일에는 별도의 상담(MCP) 도구가 없으며, Claude Code의 네이티브 advisor와도 무관합니다. 개인 진단 스크립트·로그인 파일·실제 API 키·개인 설정은 배포하지 않습니다.

소스와 문서는 [MIT License](LICENSE)를 적용합니다. 저작권·허가 고지를 유지하는 조건으로 사용·수정·배포할 수 있습니다. 추적 소스 검토에서 Node.js·Python 표준 라이브러리와 별도 설치 Claude CLI를 사용하며, 번들 외부 패키지나 별도 소스 라이선스 고지는 발견하지 못했습니다. 선택 범위와 Apache-2.0 비교는 [라이선스 검토](LICENSE-REVIEW.md)에 기록했습니다.

Claude Code·Node.js·Python·외부 서비스와 모델 자체는 배포 대상이 아니며 각각의 라이선스와 이용 조건이 적용됩니다. MIT는 계정 접근 권한·모델 사용권·요금 정책을 변경하지 않습니다.
