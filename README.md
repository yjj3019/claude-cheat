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
| 내려받은 저장소에서 `.\OpenClaude.ps1` 실행 | 해당 저장소의 `openclaude.config.yaml` (예전 `.env.openrouter` / `.env.local-llm`은 읽기 전용 폴백) |
| 기존 PC의 전역 `OpenClaude.ps1` 실행 | 전역 실행기가 가리키는 원본 실행기 폴더 |

전역 실행기를 따로 설치했다면 그 대상 폴더에서 설정을 읽습니다. 저장소를 clone해도 전역 명령이 자동 설치되거나 기존 실행기의 대상이 바뀌지 않습니다. 새 checkout은 해당 폴더의 실행기를 명시적으로 실행하세요.

> 실행 스크립트는 **제품명 형태**입니다: `OpenClaude.ps1`(라우팅 세션), `OpenClaudeDashboard.ps1`(대시보드). 이전 이름 `startClaudeOpenRouter.ps1`·`startOpenClaude.ps1`·`startClaudeDashboard.ps1`·`startDashboard.ps1`은 **호환 shim**으로 남아 있어 기존 바로가기·별칭이 그대로 동작합니다(새 이름으로 인자를 넘기고 안내만 출력).

**어느 폴더에서나 이름만으로 실행하려면** 한 번 설치합니다.

```powershell
.\installLauncher.ps1          # %USERPROFILE%\.local\bin 에 OpenClaude·OpenClaudeDashboard + 이전 이름 포워더 생성
.\installLauncher.ps1 -Remove   # 설치한 포워더 전부 삭제
```

설치 후에는 `D:\AI-Codding\Claude\ShowBox>` 같은 다른 폴더에서 `OpenClaude`(또는 `OpenClaudeDashboard`)만 입력하면 됩니다. 포워더는 이 체크아웃의 경로를 담으므로, 저장소를 옮기면 다시 설치하세요. `.local\bin`이 PATH에 없으면 설치 스크립트가 추가 명령을 안내합니다.

`.env`는 한 줄에 `KEY=VALUE` 하나를 사용합니다. 빈 줄·`#`으로 시작하는 주석·값 전체를 감싼 따옴표는 허용하며, 여러 줄 값·셸 변수 확장은 지원하지 않습니다. 키 뒤에 같은 줄 주석을 붙이지 마세요. 실제 `.env*`는 Git에서 제외합니다. 예제에는 실제 인증값을 넣지 마세요.

## 3. OpenRouter 모델 시작하기

**처음 한 번은 웹 콘솔에서 설정하는 것을 권합니다.** 콘솔은 라우팅 세션이 없어도 실행되고, 설정이 비어 있으면 화면에 시작 안내가 표시됩니다.

```powershell
.\OpenClaudeDashboard.ps1        # http://127.0.0.1:8787/ (포트 고정, 이전 인스턴스는 자동 종료)
.\OpenClaude.ps1 -Setup           # 같은 대시보드를 띄우고 설정 페이지를 브라우저로 엽니다
```

`-Setup`은 설정 파일(`openclaude.config.yaml`과 예전 `.env.*`)을 만들거나 고치지 않고 세션도 시작하지 않습니다. 값은 웹 페이지에서 직접 저장합니다. 설정이 없을 때 `OpenClaude.ps1`이 출력하는 `[setup required]` 안내에도 이 명령이 포함됩니다.

상단의 **사용할 서비스** 선택 상자에서 OpenRouter 또는 Local LLM을 고르면 해당 서비스의 입력란만 표시됩니다(옵션에 `설정됨`/`설정 필요`가 함께 표시되고, 마지막 선택은 브라우저에 기억됩니다). OpenRouter라면 **모델 ID**와 **API 키**를 입력하고 **모델 확인** → **저장**을 누른 뒤 세션을 시작하세요. 모델 확인은 저장 전 값으로도 실행되며, `openclaude.config.yaml`이 없으면 저장 시 `openclaude.config.example.yaml`의 주석을 그대로 가져와 값만 채웁니다. API 키와 서버 주소는 화면에 일부만 표시되고 저장된 원문은 브라우저로 전송되지 않으므로, 바꿀 때만 전체를 입력합니다(비워 두면 유지, 선택 항목인 HTTP 예외·모델 목록 주소는 **지우기**로 삭제). 설정이 없거나 잘못된 상태로 실행기를 시작하면 세션을 열지 않고 콘솔 주소를 안내합니다.

설정 파일을 직접 편집하는 방식도 지원합니다.

### ① 설정 파일 만들기

모든 서비스의 설정은 실행기 폴더의 `openclaude.config.yaml` **한 파일**에 둡니다. 설정 화면에서 저장해도 같은 파일이 만들어집니다. 직접 만들려면 예제를 복사해 편집하세요. 이미 파일이 있으면 덮어쓰지 않습니다.

```powershell
if (-not (Test-Path -LiteralPath 'openclaude.config.yaml')) {
    Copy-Item -LiteralPath 'openclaude.config.example.yaml' -Destination 'openclaude.config.yaml'
}
notepad openclaude.config.yaml
```

OpenRouter 섹션 내용:

```yaml
openrouter:
  model: "REPLACE_WITH_OPENROUTER_MODEL_ID"
  api_key: "YOUR_OPENROUTER_API_KEY"
```

파일 형식은 **YAML의 제한된 부분집합**입니다. `섹션:` 아래에 두 칸 들여쓴 `키: "값"` 줄과 `#` 주석만 쓸 수 있고, 값은 따옴표로 감싸는 것을 권장합니다. 탭·앵커(`&` `*`)·목록·`|` 블록·여러 줄 값은 줄 번호와 함께 거부하며 오류 문구에는 값을 싣지 않습니다. 한 서비스 섹션의 오류는 다른 서비스를 막지 않고, 알 수 없는 최상위 섹션은 그대로 보존합니다. 외부 YAML 라이브러리는 필요 없습니다.

예전 `.env.openrouter` / `.env.local-llm`은 **폐기 예정**입니다. 해당 서비스의 섹션이 새 파일에 없을 때만 읽는 읽기 전용 폴백이며, 설정 화면에서 처음 저장하면 두 서비스의 값이 새 파일로 옮겨집니다(예전 파일은 수정·삭제하지 않습니다). 새 파일에 섹션이 있으면 그 서비스는 새 파일만 사용합니다.

`YOUR_OPENROUTER_API_KEY`를 본인의 실제 OpenRouter 키로 바꿉니다. `api_key`는 **라우터가 파일에서 읽는 항목**입니다. Claude Code의 전역 환경변수에 OpenRouter 키를 넣거나 Plan 로그인을 API 키 로그인으로 바꾸라는 뜻이 아닙니다. 키를 화면·로그·커밋에 공유하지 마세요.

`model`에는 [OpenRouter 모델 목록](https://openrouter.ai/models)의 구체적인 모델 ID를 입력합니다. 기존에 키만 설정했다면 이 항목을 추가해야 합니다. 누락·빈 값·예제 플레이스홀더는 시작 오류로 종료하며 다른 모델이나 Plan으로 자동 대체하지 않습니다. 이 섹션의 두 항목만 읽고, 값은 프록시에만 전달합니다.

모델 선택이 가능해도 모든 모델의 도구 호출과 네이티브 기능을 보장하지 않습니다. 선택 모델이 [Messages API](https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-messages?explorer=true)와 필요한 도구 호출을 지원하는지 확인하세요. 응답의 모델 ID를 설정값과 정확히 비교하므로, 다른 ID로 응답하는 동적 별칭·프리셋은 실패할 수 있으며 미검증입니다. API 주소와 재시도 정책은 예제 파일에 고정값으로 구분했습니다.

### ② 실행하고 Sonnet 선택하기

```powershell
# 실행기 폴더에서 실행: 기본 제공자는 OpenRouter
.\OpenClaude.ps1
```

Claude Code에서 `/model`로 **Sonnet**을 선택합니다. 호환되는 요청은 OpenRouter의 `https://openrouter.ai/api/v1/messages`로 보내며, 대체 모델은 `openclaude.config.yaml`의 `openrouter.model`로 지정합니다. 특정 모델을 기본값으로 선택하지 않습니다.

기존 전역 실행기가 설치된 PC에서는 다른 작업 폴더에서도 다음처럼 실행할 수 있습니다.

```powershell
OpenClaude.ps1
```

실행기는 해당 Claude 프로세스의 연결 주소만 로컬 라우터로 설정하고 기존 Plan 인증을 검사합니다. 저장된 로그인·사용자 설정을 API용 설정으로 덮어쓰지 않습니다. `/status`의 Plan 로그인 표시는 인증 상태이며 **현재 응답을 OpenRouter 모델이 만들었다는 증거는 아닙니다.**

라우팅 세션은 Claude Code의 실험 beta를 끄고 시작합니다(`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, `CLAUDE_CODE_DISABLE_ADVISOR_TOOL`). Claude Code 2.1.310이 기본으로 보내는 `tool_addition`·`context_management`·advisor 도구 요청은 외부 Messages API가 HTTP 400으로 거절하므로, 같은 세션의 Plan 요청도 안정 프로토콜로 전송됩니다. 그 결과 이 세션에서는 advisor·inline 도구·서버 문맥 편집이 사용되지 않습니다. thinking·effort 설정은 요청마다 따로 전달되며, 일반 모드에서는 아래 5절대로 외부 요청에 thinking을 요구하지 않습니다.

### OpenRouter 호환 모드: Plan 전환 차단

```powershell
.\OpenClaude.ps1 -Provider OpenRouter -OpenRouterCompat
```

Claude Code 2.1.304 이상에서 `openclaude.config.yaml`의 같은 OpenRouter 키와 `openrouter.model`을 사용합니다. 주 모델·빠른 모델·하위 에이전트의 Claude 모델 선택을 모두 지정한 같은 OpenRouter 모델로 전달하며, Plan 로그인과 Plan fallback은 사용하지 않습니다. 기존 일반 실행과 Local 모드는 유지합니다.

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
if (-not (Test-Path -LiteralPath 'openclaude.config.yaml')) {
    Copy-Item -LiteralPath 'openclaude.config.example.yaml' -Destination 'openclaude.config.yaml'
}
notepad openclaude.config.yaml
```

같은 PC의 서버 예시입니다. 모델 ID는 서버에 실제로 설치·제공된 값으로 바꾸세요.

```yaml
local:
  url: "http://127.0.0.1:11434/v1/messages"
  model: "YOUR_INSTALLED_MODEL_ID"
  allow_remote: false
  # 인증이 필요할 때만 서버 전용 키 설정
  # api_key: "YOUR_LOCAL_SERVER_KEY"
```

다른 PC의 기존 서버를 사용할 때의 예시입니다. 아래 호스트는 실제 주소로 바꿔야 합니다.

```yaml
local:
  url: "https://llm.example.com/v1/messages"
  model: "YOUR_SERVER_MODEL_ID"
  allow_remote: true
  # api_key: "YOUR_SERVER_KEY"
```

아래 표의 이름은 환경변수 이름입니다. 파일에서는 `local:` 아래에 `url`, `model`, `api_key`, `allow_remote`, `http_url`, `models_url`로 씁니다. 빈 값(`""`)은 "설정 안 함"으로 읽어 기본값을 가리지 않습니다.

| 항목 | 기본값 / 동작 |
| --- | --- |
| `LOCAL_LLM_URL` | `http://127.0.0.1:11434/v1/messages`; 경로는 `/v1/messages`여야 함 |
| `LOCAL_LLM_MODEL` | 실행 시 필수; 서버의 정확한 모델 ID |
| `LOCAL_LLM_API_KEY` | 선택; 해당 서버 전용 `Authorization: Bearer`로 전달 |
| `LOCAL_LLM_ALLOW_REMOTE` | `false`; 원격 서버는 명시적 `true`와 HTTPS 필요 |
| `LOCAL_LLM_HTTP_URL` | Optional exact HTTP Messages URL pin; empty by default. Plaintext HTTP is allowed only when it exactly matches `LOCAL_LLM_URL`. Keep real values in the ignored `openclaude.config.yaml`; prefer HTTPS. |
| `LOCAL_LLM_MODELS_URL` | 같은 origin의 `/v1/models`; 모델 목록 점검에만 사용 |

같은 이름의 **프로세스 환경변수가 파일보다 우선**합니다. `localhost` 대신 허용되는 주소 리터럴 `127.0.0.1` 또는 `[::1]`을 사용하세요. 원격 HTTP는 `LOCAL_LLM_HTTP_URL`이 `LOCAL_LLM_URL`과 정확히 일치할 때만 허용하며, 이 경우 프롬프트와 `LOCAL_LLM_API_KEY` Bearer 키가 암호화 없이 전송됩니다. 그 외에는 HTTPS 또는 별도로 준비한 loopback 터널이 필요합니다.

URL 내 사용자명·비밀번호·쿼리·fragment, redirect, 잘못된 인증 헤더는 거부하고 TLS 인증서 검증을 끄지 않습니다. 원격 허용은 지정한 DNS 이름과 해석 결과를 신뢰한다는 뜻입니다.

### ② 모델 목록만 사전 점검

```powershell
.\OpenClaude.ps1 -CheckLocal
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
.\OpenClaude.ps1 -Provider Local
# 기존 전역 실행기가 설치된 PC에서는 다음 명령도 가능
# OpenClaude.ps1 -Provider Local
```

다시 OpenRouter를 쓰려면 세션을 종료하고 기본 명령으로 재실행합니다. 실행 중인 세션에서 `/model`은 Claude 모델을 선택하며 OpenRouter/Local 제공자를 바꾸는 메뉴가 아닙니다.

`-Provider`의 기본값은 **`Auto`** 입니다. 설정된 서비스가 하나뿐이면 그 서비스를 자동으로 선택하고, **둘 다 유효하게 설정돼 있으면 조용히 한쪽을 고르지 않고** 실행을 거부하며 아래처럼 명시 선택을 요구합니다(종료 코드 2).

```text
Both OpenRouter and Local settings are configured. Choose one explicitly:
  OpenClaude.ps1 -Provider OpenRouter
  OpenClaude.ps1 -Provider Local
```

설정 화면의 **사용할 서비스** 선택 상자에는 설정 여부와 함께 현재 실행 중인 세션이 쓰는 서비스가 `현재 세션`으로 표시됩니다(`/api/status`·`/api/snapshot`의 `providers`, `active_session`). `-Takeover`는 `-Provider`를 생략하면 인수인계 대상 세션의 서비스를 그대로 따릅니다.

### Claude CLI Local 호환 모드

```powershell
.\OpenClaude.ps1 -Provider Local -LocalCompat
```

Claude Code 2.1.304 이상에서 플러그인·스킬·MCP·hooks·하위 에이전트와 추론 설정을 사용하는 별도 선택 모드입니다. 일반 설정과 프로젝트 지침을 로드하며 기본 승인·거절 규칙을 유지합니다. API 키 helper는 비활성화하고, 실행 시 설정으로 프록시 주소와 임시 인증을 고정합니다. 기존 실행과 아래 `-LocalMode`의 제한은 바뀌지 않습니다. `-LocalCompat`은 `-LocalMode`·진단 옵션과 함께 사용할 수 없습니다.

주 모델·빠른 모델·하위 에이전트의 Claude 모델 선택은 **모두 설정한 같은 Local 모델**로 전달합니다. Plan 로그인과 Plan fallback은 사용하지 않습니다. `/effort low`·`/effort high`와 thinking 설정은 서버에 보존해 전달하지만, 제3자 모델의 추론 효과가 Sonnet과 같다는 의미는 아닙니다. 지원하지 않는 beta·본문 필드·토큰 계산 API는 명시적으로 거절합니다.

현재 서버는 SSE 응답의 모델 식별자가 설정과 일치하지 않아 JSON 응답을 완전히 검증한 뒤 CLI용 SSE로 변환합니다. 실시간 토큰 전달은 제공하지 않으며 120초 전체 요청 제한·1회 외부 시도·30초 재요청 대기를 유지합니다. 서명과 공급자 메타데이터는 새로 만들지 않고 보존합니다. 모든 네이티브 서버 기능과 임의의 플러그인 조합을 지원한다고 보장하지 않습니다. [실서버 검증 범위](docs/LIVE-VALIDATION.ko.md#local-호환-모드-검증-2026-10-24)를 참고하세요.

### Claude CLI Local 전용 모드

```powershell
.\OpenClaude.ps1 -Provider Local -LocalMode
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
| `local_configuration_invalid` | `openclaude.config.yaml`(또는 예전 `.env.local-llm`)과 환경변수의 URL·HTTP 허용·모델·키 형식·모델 목록 origin |
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
.\OpenClaude.ps1 -Provider Local -DiagnoseLocal
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

- 일반 모드의 외부 시도는 **무응답 30초**(헤더 대기·청크 간 간격)를 넘기면 실패하고, 응답이 계속 오는 동안은 최대 300초까지 기다립니다. 재시도 간격은 **1초**이며, HTTP 429의 `Retry-After`가 더 길면 그 값(내부 대기 상한 기본 **10초**)을 따릅니다. 네 번 모두 무응답이라면 Plan 요청 전까지 대략 123초가 걸릴 수 있습니다. Local·compat 모드는 기존의 고정 총 시간 제한을 유지합니다. Plan 응답 시간이나 CLI 자체 재시도는 여기에 추가될 수 있습니다.
- `Retry-After`는 정수 초와 HTTP-date(RFC 7231)를 모두 읽고 최대 10분으로 자릅니다. 제공자가 긴 대기를 요구하면 프록시는 턴을 붙잡지 않고, **최종 실패 응답에 429/529 상태와 `Retry-After`(초)**를 실어 클라이언트가 기다리게 합니다. 단순 429에 임의의 기본 대기시간을 만들지 않습니다.
- **회로 차단은 실패 원인을 구분합니다.** 전송 실패·타임아웃·5xx·408·검증 불가한 2xx는 제공자 장애로 보고 회로를 열며, 401·402·403은 제공자 설정 문제로 기존처럼 쿨다운합니다. 반면 429는 제공자가 알려준 `Retry-After`(최대 10분)을 쿨다운으로 사용하고, **요청 자체가 잘못된 4xx는 회로를 열지 않습니다**. half-open 복구 확인은 동시에 하나만 허용되며, 늦게 도착한 이전 시도의 성공·실패는 새 세대의 회로 상태를 덮어쓰지 않습니다.
- 클라이언트가 요청 본문을 보내다 멈추면 **유휴 기준 타임아웃(기본 30초, `ROUTE_REQUEST_IDLE_TIMEOUT_MS`)** 후 연결을 끊고 점유한 턴을 반납합니다. 느리지만 계속 보내는 큰 업로드는 끊지 않습니다. 연결이 끊긴 요청은 업스트림 읽기·타이머를 정리하고 재시도하지 않습니다.
- 대기 기간이 지나도 백그라운드 진단 요청은 보내지 않습니다. 다음 호환 요청으로 복구를 확인하는 동안 다른 요청은 Plan로 보냅니다.
- Plan 요청까지 실패하면 그 오류를 전달합니다. 다른 API 키·제공자로 연쇄 전환하거나 무한 반복하지 않습니다. CLI 자체의 재시도 표시는 라우터 내부의 재시도 횟수와 다를 수 있습니다.
- 외부 응답은 완결성·실제 응답 모델·도구 JSON 등을 검증한 뒤 전달합니다. 외부 스트리밍 응답도 최대 32MiB까지 버퍼링하므로 첫 표시가 늦어질 수 있습니다. 실패한 부분 텍스트나 도구 호출을 전달한 뒤 중간에 Plan 응답을 이어 붙이지 않습니다.
- 요청을 취소하면 재시도와 fallback도 중단합니다. 재시도된 외부 추론은 별도 사용량이 발생할 수 있습니다.
- **Plan으로 보낼 때 도구 호출 id를 계약에 맞춥니다.** 외부 제공자가 만든 `Bash:0#abc` 같은 id는 Anthropic의 `^[a-zA-Z0-9_-]{1,64}$` 규칙을 벗어나 Plan 경로에서 400을 유발할 수 있습니다. 프록시는 Plan 전달 직전에 해당 id만 요청 단위로 재할당하고 `tool_use`↔`tool_result` 쌍을 유지하며, 이미 규칙에 맞는 id는 **바이트 그대로** 둡니다(프롬프트 캐시 보존). 외부 경로는 건드리지 않고, 빈 id는 업스트림 호출 없이 로컬 400으로 거부합니다.
- Plan 전용 모델·토큰 수 계산처럼 외부가 표현할 수 없는 요청은 **처음부터 Plan로 우회**합니다. 라우팅 세션이 실험 beta를 끄므로 CLI는 advisor·inline 도구 요청을 만들지 않습니다. `system` 역할 컨텍스트 메시지(text 블록 포함)와 CLI가 상시 보내는 `interleaved-thinking-2025-05-14`·`effort-2025-11-24`·`context-1m-2025-08-07` beta는 모든 모드에서 허용합니다. 제공자가 붙인 `citations`는 응답에 포함되어 다음 요청 이력으로 다시 전송되므로 입력에서도 허용합니다. 이전 응답의 서명된 thinking 블록은 외부 사본에서만 제거하므로, 한 번 Plan으로 우회한 뒤에도 다음 요청은 다시 외부로 갈 수 있습니다. 이 우회는 외부 장애나 복구 성공으로 세지 않습니다.

프록시 동작은 아래 환경변수로 조정합니다(기본값으로 충분하며, 세션 시작 전에 설정).

| 변수 | 기본값 | 의미 |
| --- | --- | --- |
| `ROUTE_REQUEST_IDLE_TIMEOUT_MS` | `30000` | 요청 본문·헤더의 유휴 타임아웃. `0`이면 끔. 느리지만 계속 오는 업로드는 영향 없음 |
| `ROUTE_RETRY_MAX_WAIT_MS` | `10000` | 프록시 내부 재시도 1회 대기 상한. 긴 `Retry-After`는 클라이언트 응답 헤더로 전달 |
| `ROUTE_HANDOVER_TIMEOUT_MS` | `120000` | 무중단 교체 시 진행 중 턴을 기다리는 상한. 초과하면 종료 대신 서비스 복귀(409) |
| `ROUTE_STREAM_PASSTHROUGH` | (꺼짐) | `1`이면 검증되는 프레임을 즉시 전달해 첫 토큰 지연을 줄임 |
| `ROUTE_STABILIZE_PROMPT_CACHE` | (꺼짐) | `1`이면 외부 전송 직전 system 끝의 `<total_tokens>…` 푸터를 제거해 prefix 캐시를 보존(opt-in) |

## 5-1. 첫 토큰 지연 줄이기 (선택)

기본 동작은 **응답 전체를 검증한 뒤** 한 번에 보내므로, 스트리밍 응답도 생성이 끝나야 화면에 나타납니다. 이 보장(중간 실패 시 재시도·Plan fallback)을 유지하는 대신 첫 토큰이 늦습니다.

```powershell
.\OpenClaude.ps1 -Stream        # 프레임이 검증되는 즉시 전달 (TTFB 단축)
```

| 모드 | 첫 토큰(TTFB) | 실패 시 동작 |
| --- | --- | --- |
| 기본(버퍼링) | 응답 완료 시점 (측정 782ms / 782ms) | 재시도 4회 → Plan fallback |
| `-Stream`(옵트인) | 첫 유효 콘텐츠 즉시 (측정 **339ms** / 완료 772ms) | 첫 토큰 전이면 동일하게 fallback, **이미 보낸 뒤면** 스트림을 오류로 종료(중복 방지) |

- 전달은 **첫 유효 콘텐츠가 검증된 뒤** 시작합니다(빈 응답·thinking 전용·깨진 도구 JSON은 여전히 fallback).
- 환경 변수로도 켤 수 있습니다: `$env:ROUTE_STREAM_PASSTHROUGH = '1'; OpenClaude.ps1`

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
.\OpenClaudeDashboard.ps1
```

`127.0.0.1`에만 열리는 **읽기 전용** 페이지입니다. 포트는 **8787로 고정**되며, 이전에 띄운 대시보드가 남아 있으면 자동으로 종료하고 같은 포트를 다시 사용합니다. 다른 프로그램이 그 포트를 쓰고 있으면 임의 포트로 옮기지 않고 오류로 알립니다. 빈 포트로 대체하려면 `--allow-port-fallback`을 지정하세요.

이전 인스턴스 종료는 `.dashboard.pid`에 기록한 **pid와 프로세스 생성시각이 모두 일치할 때만** 수행합니다(`python routeDashboard.py --stop` 포함). Windows에서 pid가 재사용된 경우·자기/상위 프로세스·구버전(신원 없는) 기록은 종료하지 않고 `identity_mismatch`/`own_tree_pid`/`identity_unverifiable` 한 줄로 보고하며, 그 기록은 다음 시작 때 덮어씁니다. 업그레이드 직후 이전 버전 대시보드가 남아 있다면 그 인스턴스를 한 번 직접 종료(`Ctrl+C`)한 뒤 시작하세요. 비Windows에는 생성시각 조회 수단이 없어 이 검증이 불가능하므로, 안전을 위해 종료를 거부합니다.

**갱신 주기**는 상태에 따라 달라집니다: 라우팅이 진행 중이면 2초, 유휴면 10초, 브라우저 탭이 숨겨져 있으면 15초. 화면 상단에는 한 줄 요약(`오늘 43.8M토큰 · $1.42 · 남은 한도 $48.25 · 우선 확인: …`)이 표시됩니다.

**인사이트** 카드는 숫자를 나열하는 대신 *무엇이 달라졌고 왜 그런지, 다음에 무엇을 할지*를 우선순위로 제시합니다. 각 항목은 근거 수치(칩)를 함께 보여주며, 해당 배너와 같은 원인이면 배너를 중복 표시하지 않습니다.

| 인사이트 | 판단 기준 | 제안 |
| --- | --- | --- |
| 실행기 코드가 오래됨 | 프록시 시작 시각 < 소스 수정 시각 | `.\OpenClaude.ps1` (복사 버튼) |
| 외부 회로 차단 | 최근 창에서 회로 개방 발생 | 네트워크·키·모델 ID 확인 |
| Plan 우회 급증 / 비중 상승 | 10분 Plan 비율 ≥50% / ≥20% | 최다 사유 코드의 의미·조치 |
| 외부 요청 거부 | 400·413·422 실패 누적 | 실행기 갱신 |
| 한도 소진 예상 | 오늘 사용 속도로 남은 한도 7일 이내 | 단가·사용량 조정 |
| 사용 속도 | 남은 일수 추정 | 참고 정보 |
| 캐시 재사용 저하 | 입력 20만 토큰 이상에서 재사용 <15% | 프롬프트 앞부분 고정 |
| 응답 지연 | 이 세션 p95 ≥20초 | **첫 바이트 시간과 생성 대기로 나눠** 표시 — 프록시 구간인지 모델 응답 속도인지 구분해 조치 |
| 세션 유휴 | 마지막 결정 30분 이상 | 참고 정보 |

| 항목 | 내용 |
| --- | --- |
| 지금 상태 | 마지막 `route_selected` 기준 **OpenRouter 사용 중 / Plan 우회 중 / 유휴**, **실제 사용 모델**, 경과 시간, 사유 |
| 경고 배너 | 최근 10분에 Plan 우회 50% 이상, 회로 차단, 400·413·422 거부, 잔액 10% 이하일 때 표시 |
| 최근 60분 | OpenRouter 대 Plan 비율, 회로 차단 횟수 |
| 우회 사유 | `native_feature`의 `detail`별 건수와 **의미·조치 설명**, 외부 실패 상태·검증 실패 분류 |
| 크레딧 | 키 한도·이번 달 사용액·잔액·리셋일 (OpenRouter `/api/v1/key`) |
| 오늘 토큰 | OpenRouter·Plan 토큰, 입력/출력, 캐시(5분·**1시간 TTL 분리**), **비용(상태 표기)** |
| 시간대별 토큰 | 최근 24시간을 OpenRouter·Plan 막대로 표시 |
| 라우팅 전환 기록 | 경로가 바뀐 시점·사유·모델·건수·지속 시간 (최근 12회) |
| 세션별 사용 | **고유 턴 수**(중복 제거), 외부/Plan 턴 수, 외부 토큰, **비용과 상태**, 사용 모델 |
| 도움말 | CLI 명령어(`--status` 계열), HTTP API, 사유 코드표(의미·조치), 상태 배지, 종료 코드, 데이터 출처 |

**턴은 정확히 한 번만 셉니다.** Claude Code는 같은 응답을 스트리밍 중 여러 번 기록하고 `/resume`·포크는 히스토리를 복사하므로, 파일별 합산은 같은 턴을 여러 번 셉니다(이 PC의 7일 창에서 assistant 행의 42%가 중복). 대시보드는 `message.id`+`requestId`(없으면 `msg:`/`uuid:` 폴백)로 턴을 식별하고, 중복 행은 턴 수를 늘리지 않으며 더 커진 토큰 차액만 반영합니다. 따라서 이전 버전보다 턴 수가 낮게 보이는 것이 정상입니다.

**비용에는 상태가 붙습니다.** OpenRouter 경유분은 카탈로그 단가 기반 **추정(estimated)**, Plan 경유분은 구독 포함 **included(귀속용 $0)**, 단가를 확인할 수 없는 모델은 **unknown(비용 null)** 입니다. 캐시 생성 토큰은 5분·1시간 TTL을 분리해 1시간 단가가 있으면 그 값으로, 없으면 표준 단가로 계산하며 어느 쪽을 썼는지 함께 표시합니다. 실제 청구는 제공자 기록을 기준으로 확인하세요. **실제 사용 모델**은 프록시가 응답을 전달하기 직전에 기록합니다. CLI가 보여주는 모델 이름은 요청 별칭(`claude-sonnet-5-5` 등)이므로, 실제 제공자 모델은 이 값으로 확인하세요.

집계 캐시(`.usage-cache.json`)는 턴 소유권 원장을 포함해 실사용 7일 창에서 약 **0.5MB**입니다(합성 2,000파일·10만 턴 기준 5.4MB, 상한 20만 클레임에서 오래된 파일 단위로 축출). 캐시 버전이 바뀌면 한 번 전체 재스캔합니다.

키·프롬프트·요청 본문은 표시하지 않습니다. 설정 저장을 요청했을 때만 실행기 폴더의 `openclaude.config.yaml` 하나를 수정하고, 그 외에는 아무것도 기록하지 않습니다. 대시보드는 세션과 별개 프로세스이므로 라우팅 세션과 함께 띄워 두면 됩니다.

### 설정 편집과 연결 테스트

설정은 대시보드와 **분리된 페이지**(`http://127.0.0.1:8787/settings`)에서 관리합니다. 상단 내비게이션의 **설정**으로 이동하세요. 두 페이지는 같은 서버·같은 세션 토큰을 사용합니다.

| 항목 | 동작 |
| --- | --- |
| 저장 | 지정된 키만 수정하고 다른 주석·섹션·줄바꿈(CRLF)·BOM은 그대로 유지합니다. 이전 파일은 **바이트 그대로** `openclaude.config.yaml.bak`에 남깁니다. 해당 서비스 섹션이 아직 없고 예전 `.env`에 값이 있으면, 섹션을 새로 만들 때 그 값을 함께 옮겨 자격증명이 가려지지 않게 합니다. 동시 저장은 파일 잠금으로 직렬화되며, 같은 파일을 읽을 수 없으면(인코딩·문법 오류) 덮어쓰지 않고 오류와 줄 번호만 알려 줍니다 |
| 비밀값 | 브라우저로 다시 전송하지 않습니다(`sk-or-…cd0`처럼 마스킹). 입력을 비우면 저장된 값을 유지하고, 새 값을 넣을 때만 교체합니다 |
| 서버 주소 | Messages·HTTP 예외·모델 목록 주소도 키처럼 일부만 보입니다(`http://12•••••.1:11434/v1/messages`: 호스트는 앞뒤 2자만, `user:pass@`·쿼리는 가림, 표준 경로가 아닌 사용자 지정 path는 `/•••`). 원문은 브라우저로 전송하지 않고, 비우면 유지합니다 |
| 지우기 | 선택 항목(HTTP 예외·모델 목록 주소)은 **지우기** 버튼으로 삭제합니다(`POST /api/config`의 `clear` 목록). 필수 항목과 키는 지울 수 없습니다 |
| 검증 | 모델 ID 형식, 키 형식, Local URL 규칙(루프백·HTTPS·동일 출처)을 저장 전에 확인하며 실패하면 파일을 건드리지 않습니다 |
| 보호 | 설정 조회(`GET /api/config`)와 저장(POST) 모두 페이지에 내장된 세션 토큰이 필요하고, POST는 동일 출처일 때만 허용합니다(다른 사이트에서 호출 불가) |
| 연결 테스트 | **모델 확인**은 모델 목록만 조회하고, **추론 테스트**는 16토큰으로 1회 호출합니다(소액 과금 가능) |
| 모델 선택 | **목록** 버튼으로 제공자 모델을 검색해 고릅니다(컨텍스트 길이·가격 표시). `GET /api/models?provider=openrouter\|local` (세션 토큰 필요, 10분 캐시) |
| 적용 시점 | 저장한 설정은 **새 세션부터** 적용됩니다. 실행 중인 세션은 재시작하세요 |

### CLI 감지와 연결 테스트 (진단 전용)

설정 화면의 **CLI (연결 테스트 전용)** 항목은 설치된 Codex · Grok · Claude Code CLI를 찾아 버전을 보여 주고, 외부 통신이 되는지 확인하는 **진단 도구**입니다. **요청을 CLI로 보내는 라우팅은 아직 지원하지 않습니다**(`OpenClaude.ps1 -Provider`에 CLI는 없습니다).

| 항목 | 동작 |
| --- | --- |
| 감지 | `PATH`와 알려진 설치 폴더(`~\.grok\bin`, `%APPDATA%\npm`, `~\.local\bin`)를 찾고, 이미 실행 중인 세션의 PATH가 낡았으면 사용자·시스템 PATH를 레지스트리에서 다시 읽어 보정합니다. `GET /api/cli`(세션 토큰 필요) |
| 감지 확인 | `--version`만 실행합니다(네트워크 사용 없음) |
| 연결 테스트 | `Reply with exactly OK` **한 줄**을 선택한 CLI로 보냅니다. 그 CLI의 **로그인으로 외부 서비스를 실제로 호출**하므로 소액 과금이나 사용량이 생길 수 있습니다 |
| 실행 방식 | 셸 없이 직접 실행합니다(npm `.cmd` 래퍼는 내용을 읽어 실제 대상으로 풀고, 인식 못 하면 거부). 실행 파일은 **절대 경로 + 이름 일치 + 존재**를 확인하고 **UNC 경로와 심볼릭 링크는 거부**합니다(하드 링크는 여전히 구분 못 함). 매번 빈 임시 폴더에서 실행하고 프롬프트는 인자가 아니라 파일/표준 입력으로 전달합니다. Windows에서는 자식 프로세스를 **kill-on-close Job Object**에 넣어, CLI 루트가 먼저 끝나도 남은 손자 프로세스를 실행 종료 시 정리합니다 |
| 환경 | 허용 목록만 자식 프로세스에 전달합니다(PATH·시스템 변수, 프록시·인증서 변수, 해당 CLI의 로그인 변수). `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_*`, `OPENROUTER_*`, 대시보드 토큰은 절대 전달하지 않습니다(`claude -p`가 이 프록시를 다시 호출하는 재귀를 막기 위함) |
| 결과 | `ready`, `timeout`(방화벽·프록시 의심), `not_logged_in`, `unsupported_flag`(CLI 버전이 옵션을 거부), `not_found` 등 **분류·종료 코드·소요 시간만** 표시합니다. CLI의 출력·오류 원문은 표시도 기록도 하지 않습니다 |
| 제한 | 동시에 하나만 실행합니다. 제한 시간은 10~120초(기본 60)입니다 |

도구 차단 플래그(예: Codex `--disable shell_tool`)는 **최선 노력이며 실제로 파일 읽기를 막는지는 검증하지 않았습니다.** 연결 테스트의 프롬프트는 고정 문자열이지만, CLI는 에이전트이므로 신뢰하지 않는 입력을 보내는 용도로 쓰지 마세요.

### 요청 구성 계측 (CLI 후보 측정)

대시보드의 **요청 구성 (CLI 후보)** 카드는 요청을 **모델 계열 × 도구 유무**로 센 것입니다. 텍스트 전용 CLI가 처리할 수 있는 요청(도구·tool_result·이미지 없음)이 실제로 얼마나 되는지 재려는 용도입니다.
- 프록시가 요청마다 **내용 없는** 이벤트 한 줄(`request_shape`: 계열, 도구, 도구 블록, 비텍스트, 스트림 여부)을 경로 로그에 남깁니다. 모델 ID·프롬프트·도구 이름은 기록하지 않습니다.
- OpenRouter 세션에서만 기록됩니다(경로 로그가 있을 때). `/api/status`의 `request_shapes`로도 볼 수 있습니다.
- 참고: Opus·Haiku·Fable 요청은 지금도 항상 Plan으로 가므로, CLI 후보가 되려면 도구 없는 **Sonnet** 요청이어야 합니다. 이 수치가 작으면 CLI 라우팅은 효과가 작습니다.

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

실행 중인 프록시는 자체 카운터를 `GET /api/metrics`로 제공합니다(같은 세션 토큰 필요, 127.0.0.1 전용). 요청 수, 외부·Plan 경로 분포, p50/p95 지연, 재시도·스트림·회로 횟수, 송수신 바이트를 담고 있으며, 같은 값이 실행기 폴더의 `.proxy-metrics.json`에 1초 간격으로 기록되어 대시보드의 **프록시 처리량** 카드에 표시됩니다.

## 7. 문제 해결과 일반 Claude로 돌아가기

| 증상 | 먼저 확인할 내용 |
| --- | --- |
| `openclaude.config.yaml`을 찾지 못함 | 현재 작업 폴더가 아닌 실제 실행기 폴더에 저장했는지 확인 |
| OpenRouter 401 / `Missing Authentication header` | 전용 파일의 실제 키·유효성을 확인; HUD 플러그인 설치 성공은 API 인증 성공과 별개 |
| Local 설정을 바꿔도 이전 서버 사용 | 프로세스 환경변수 우선순위와 전역 실행기 대상 확인 후 세션 재시작 |
| Local 모델 목록 성공인데 추론 실패 | Messages API·모델 ID·도구/응답 형식 호환성을 별도 확인 |
| Sonnet 선택 후 계속 Plan 사용 | `-Debug`로 `route_selected` 확인; `native_feature`는 Plan 전용 모델·토큰 계산·네이티브 이력 때문에 정상일 수 있음. `unsupported_beta`가 반복되면 CLI가 라우터가 모르는 beta를 추가한 것이므로 실행기를 갱신 |
| 대시보드에 `실행기 재시작 필요` | 실행 중인 프록시가 현재 코드보다 먼저 시작됨. 경고의 **명령 복사** 버튼으로 `.\OpenClaude.ps1`을 복사해 세션을 재시작. `지금 상태` 카드의 **프록시 코드**·**소스 수정** 시각으로 차이를 확인 |
| 세션이 갑자기 끊김 | `.route.log`에서 `parent_disconnected`(실행기 종료) 여부 확인. 다른 스크립트·작업이 `python` 프로세스를 **일괄 종료**하지 않았는지 확인. 대시보드는 `python routeDashboard.py --stop`으로만 종료 |
| `503 ... restart required`가 반복됨 | 과거 실행기/프록시 잔존 여부·첫 upstream 오류를 확인하고 최신 실행기로 새 세션 시작; 후속 503만으로 원인 단정 금지 |
| HUD·MCP 일부가 동작하지 않음 | 일반 Claude에서도 같은지 비교; 설정 로드와 서버 연결·권한 정상 여부는 별도 |
| 응답 전 대기가 길어짐 | 외부 4회 시도와 버퍼링 여부 확인; 사용자가 취소하면 후속 시도도 중단 |

라우팅 세션은 판단 근거를 실행기 폴더의 `.route.log`에 함께 남깁니다. 세션을 시작할 때마다 `proxy start` 줄이 추가되고, `route_selected`·`external_attempt_failed`·`circuit_open` 같은 이벤트가 시각과 함께 쌓입니다. `-Debug`를 켜지 않은 지난 세션도 이 파일로 확인할 수 있으며, `*.log` 규칙으로 Git에서 제외됩니다.

같은 턴의 로그 줄은 **8자리 `traceId`** 로 묶이므로 `external_attempt_failed` 4회와 `circuit_open`·`route_selected`를 한 번에 재구성할 수 있습니다(`metrics.last.traceId`에도 같은 값). 로그는 **단일 sink에서 자격증명을 세척**합니다: 설정된 API 키, `Bearer …`, `sk-*`/`sk-ant-*`/`sk-or-*`, `x-api-key` 값은 `[redacted:provider-key|bearer-token|api-key]` 태그로 바뀌고 모델명·상태코드·traceId는 유지됩니다. 제공자 오류 본문은 최대 **64KiB**까지만 읽어 같은 규칙으로 세척한 뒤 전달하며, 더 긴 본문은 잘렸다는 이벤트를 남깁니다. 세척은 패턴 기반이므로 문자열이 난독화된 자격증명까지 보장하지는 않습니다.

코드나 설정을 수정한 뒤에는 라우팅 세션을 종료하고 다시 실행해야 합니다. 일반 Plan 환경으로 돌아가려면 **깨끗한 새 터미널**에서 평소처럼 실행하세요.

```powershell
claude
```

이 실행기는 부모 터미널의 전역 설정을 바꾸지 않습니다. 과거에 수동으로 설정한 `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_CONFIG_DIR` 등이 사용자/시스템 환경변수에 남아 있다면 새 터미널도 이를 상속하므로 별도로 확인해야 합니다. 로그인 파일이나 기존 MCP·사용자 설정 전체를 삭제하지 마세요.

보안 모델·신뢰 경계·CSRF/DNS 리바인딩 처리와 **잔여 위험**은 [`docs/SECURITY.ko.md`](docs/SECURITY.ko.md)에 정리되어 있습니다.

## 8. 무중단 재시작 (프록시 교체)

프록시 코드를 바꾼 뒤에도 **실행 중인 세션의 포트를 그대로** 이어받을 수 있습니다.

```powershell
OpenClaude -Takeover        # 다른 터미널에서 실행
```

동작 순서:

1. `.session-handoff.json`에서 실행 중인 포트·토큰을 읽습니다.
2. 기존 프록시에 `POST /api/handover`(세션 토큰 필요)를 보냅니다.
3. 기존 프록시는 **새 요청에 503 + `Retry-After: 1`**로 응답하고, 진행 중인 턴이 끝날 때까지 기다린 뒤 종료합니다(기본 상한 **120초**, `ROUTE_HANDOVER_TIMEOUT_MS`로 조정).
4. 상한을 넘겨도 진행 중인 턴이 남아 있으면 **종료하지 않고 서비스로 되돌아갑니다.** 이때 응답은 HTTP **409**와 `{"drained": false, "reverted": true, …}`이고, 실행기는 교체를 시작하지 않은 채 "턴이 끝난 뒤 다시 시도하라"는 오류를 냅니다. 진행 중인 응답을 잘라 세션을 끊는 것보다 안전합니다.
5. 새 프록시가 **같은 포트·같은 토큰**으로 즉시 바인딩합니다.

CLI는 포트를 바꾸지 않으므로 재연결만 하면 계속 동작합니다. 드레인 대기 중 새 요청은 503으로 거절되며, 클라이언트는 재시도합니다. 동시에 여러 번 요청해도 **하나의 드레인만 실행**되고 종료 훅은 한 번만 호출됩니다. 강제 킬 없이 코드가 교체되는 경로는 이 방법뿐입니다.

## 9. 검증 방법과 남은 한계

공개 저장소에는 개인 PC 구성, 계정 사용량, 청구 내역, 실제 세션 로그를 보관하지 않습니다. 아래 명령은 합성 데이터와 로컬 모의 서버로 검사하며 외부 모델을 호출하지 않습니다.

```powershell
node --test tests/*.test.mjs
python -m unittest discover -s tests -p 'test_*.py' -v
python openrouterAdvisor.py --self-test
node --check claudeRouteProxy.mjs
node tests/simulate.mjs --soak 3000        # 합성 트래픽·장기 세션·메모리
python openrouterAdvisor.py --print "Reply with exactly OK"   # 실제 CLI 1턴(자동화용)
node tests/fuzz.mjs --iterations 2000      # 프록시 퍼징(결정적 시드)
python -m unittest tests.test_fuzz_dashboard   # 대시보드 퍼징
python tests/live_smoke.py --dry-run       # 설정 존재 여부만 확인(무과금)
```

- Windows 프로세스 트리 테스트(`test_local_lifecycle`)와 동시성 스트레스(`test_dashboard_concurrency`)는 **기본 스위트에서 건너뜁니다**. 실제 프로세스 트리를 띄우거나 서버에 12스레드를 걸기 때문에 느리고 부하에 민감합니다. 필요할 때만 `$env:CLAUDE_CHEAT_SLOW = '1'`로 켜세요.
- 프로세스 트리 테스트는 **Git Bash**가 필요합니다. `C:\Windows\System32\bash.exe`(WSL)는 Windows 파이썬 경로를 실행하지 못해 자동으로 건너뜁니다.
- 대시보드는 **Host 허용목록**(127.0.0.1·localhost·::1)으로 DNS 리바인딩을 막고, `nosniff`·`no-referrer`·`frame-ancestors DENY`(X-Frame-Options) 헤더를 보냅니다. 쓰기 API는 세션 토큰 + 동일 출처를 함께 요구합니다.
- 실제 제공자 호출은 기본 스위트에서 제외되어 있습니다. 설정 변경 후 한 번 확인하려면 `python tests/live_smoke.py`(16토큰, 약 1회 0.01센트 미만)를 사용하세요.
- 실행기에는 **비대화형 `--print "프롬프트"`** 모드가 있습니다(기본 JSON 출력, `--print-text`로 사람용). 실제 CLI 1턴을 자동 검증하려면 `$env:CLAUDE_CHEAT_LIVE = '1'; python -m unittest tests.test_cli_integration`.

검사 대상은 요청·응답 보존, 인증 격리, 도구 결과 왕복, 최초 요청과 재시도 3회, Plan fallback, 회로 복구, 취소·타임아웃·빈 응답 처리입니다. 합성 테스트 통과를 실제 서비스나 모든 Claude Code 기능의 통과로 해석하면 안 됩니다.

### 시뮬레이션과 성능 측정

모의 업스트림으로 트래픽을 만들어 처리량·지연·메모리·라우팅 결과를 측정합니다. 외부 모델을 호출하지 않습니다.

```powershell
node tests/simulate.mjs                                   # 혼합 트래픽 + 장기 세션 + 회로 차단
node tests/simulate.mjs --requests 4000 --concurrency 48 --json
node tests/bench_proxy.mjs --quick                        # 새 프로세스 반복 측정 + 정확성 게이트
node tests/bench_proxy.mjs --soak-seconds 10              # 시간 기준 소크(자식 프록시 RSS·GC 후 heap)
node --test tests/bench_harness.test.mjs                  # 벤치 하네스 자체 회귀 테스트
node --test tests/simulation.test.mjs                     # 위 시뮬레이터 스모크 테스트
```

`bench_proxy.mjs`는 **부하 생성기(부모)와 프록시(자식 프로세스)를 분리**합니다. 반복마다 새 프로세스를 띄워 프로세스별 중앙값과 프로세스 간 중앙값·편차를 함께 보고하고, 속도뿐 아니라 **응답·스트리밍·업스트림 재작성 본문이 기대값과 같은지**를 검사합니다. 재시도 시나리오는 시도 횟수를 정확히 단정하며, 결과 JSON(`.bench/`, git 제외)에 환경·커밋·워크로드·판정을 남깁니다. 종료 코드는 `0` 통과, `1` 정확성/예산/소크 증가 실패, `2` 사용법 오류입니다.

시나리오는 일반 응답 · SSE 스트림 · 도구 호출 · 429 재시도 · 1MB 본문 · 미지원 beta 우회 · 회로 차단/쿨다운이며, 각 요청이 기대한 경로로 갔는지 응답 표식으로 확인합니다. 하나라도 어긋나면 종료 코드 1입니다.

측정 기준(이 개발 PC, Node 24). 아래 표에서 시뮬레이터·소크 값은 클라이언트와 프록시가 한 프로세스, `bench_proxy.mjs` 값은 분리 측정입니다.

| 항목 | 값 |
| --- | --- |
| 혼합 처리량 | 1,200요청/동시성 24에서 약 700–980 req/s, p50 16–25ms, p95 55–82ms |
| 장기 세션 | 120턴·247KB 이력 3.1ms (최적화 전 3.9ms) |
| 본문 크기 | 1MB 7.7–8.9ms (약 120MB/s) · 새 프로세스 하네스 1MB 8.4–9.8ms |
| 재시도 비용 | 1MB + 재시도 1회 10.7ms → 12.8ms(수정 전), 4회 실패 9.8ms → 12.1ms (약 17–19%↓) |
| 회로 | 실패 4회 → Plan fallback, 쿨다운 중 Plan, 복구 후 외부 복귀 |
| 장시간 소크 | 150,000요청 266MB · **1,200,000요청에서 RSS 1.09GB**(V8 힙 확장, 256MB 상한에서 통과 → 누수 아님) |
| 세션 집계 캐시 | 재시작 시 콜드 스캔 **1,119ms → 20ms**(실데이터 357파일·464MB). `.usage-cache.json`(v5)에 파일별 집계·**크기·mtime·앞/뒤 4KB sha256 지문**·턴 소유권 원장을 저장해, 같은 크기로 덮어쓴 파일도 재스캔하고 바뀌지 않은 파일은 읽지 않습니다 |
| 페이지 전송 | 39.3KB → **12.0KB**(gzip, `Accept-Encoding` 협상). 루프백 렌더 2.7ms/요청 |
| 대용량 로그 | 콜드 리드는 **마지막 4MB만** 파싱(오프셋은 절대 위치 유지). 수 주 누적된 로그도 시작이 느려지지 않음 |
| 힙 상한 | 실행기가 프록시를 `--max-old-space-size=512`로 띄웁니다. **1,200,000요청 재측정: 피크 510MB · drift 239MB** (상한 미적용 시 1.09GB) |

장시간 부하는 `--soak`으로 3,000요청까지 돌려 RSS와 처리율을 확인합니다.

```powershell
node tests/simulate.mjs --soak 3000 --concurrency 16
```

측정 예: 3,000요청에서 **RSS 피크 245MB · 증가폭 5.6MB** (누수 없음), 지연 링 버퍼는 512개로 고정. 메모리 증가폭이 판정 기준이고, 창별 중앙 지연은 참고용으로 출력합니다(`simulate.mjs`는 클라이언트·프록시가 한 프로세스라 GC 영향이 섞입니다).

프로세스를 분리한 소크는 `bench_proxy.mjs --soak-seconds`를 사용합니다. 10초 소크 측정 예(다른 작업이 함께 도는 PC): **12,535요청(1,253 req/s)**, 시작/중간/끝 중앙 지연 5.28/5.32/4.64ms, **RSS +42.2MB · GC 후 heap +0.09MB**, 오류 0. 판정은 GC 후 heap 증가폭(기본 12MB)과 RSS 증가폭(기본 192MB) 기준입니다.

```powershell
node tests/bench_proxy.mjs --soak-seconds 10
```

대시보드 갱신 비용도 같은 방식으로 측정합니다(2초마다 스냅샷 재생성).

```powershell
python tests/bench_dashboard.py            # 10MB 로그 · 2,000개 세션 파일
```

| 항목 | 값 |
| --- | --- |
| `.route.log` 파싱 | 10MB 전체 읽기 38~278ms → **증분 tail 1~3ms** (새 줄 200개 1ms) |
| 세션 스캔 | 2,000파일 첫 스캔 5초(백그라운드), 이후 증분 40ms → **23ms** (`os.scandir` 메타데이터 재사용) |
| 스냅샷 갱신 | 2,000파일·10MB 로그에서 **입력 변화 없음 8ms**, 새 결정 직후 첫 갱신 43ms |

측정으로 찾아 고친 것: 요청 본문을 **재시도 루프 안에서 4번 직렬화**하던 것을 1회로 옮겼고, 서명된 추론 이력이 없는 대화에서 **메시지 배열을 매 요청 재구성하지 않도록** 바꿨으며(장기 세션 약 19% 개선), 라우팅 로그를 **파일 오프셋에서 증분으로** 읽고(회전·절단은 선두 바이트 지문으로 감지), **새 라우팅 결정이 없으면 세션 스캔 자체를 생략**하며(서명 캐시, 60초마다 강제 재스캔), 1MB 이상 본문은 **단일 청크일 때 복사 없이** 전달합니다. 창당 이벤트가 2,000개를 넘으면 최근 2,000개만 집계해 갱신 시간을 제한합니다. 같은 명령으로 언제든 재측정할 수 있습니다.

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
| [OpenClaude.ps1](OpenClaude.ps1) | 제공자 선택과 Python 실행기 호출 |
| [openrouterAdvisor.py](openrouterAdvisor.py) | Plan 인증 검사·환경 격리·프록시 수명 관리·Local 목록 점검 |
| [localProcessTree.py](localProcessTree.py) | Windows Local 전용 진입점의 Job 소유권·프로세스 트리 종료 감시 |
| [claudeRouteProxy.mjs](claudeRouteProxy.mjs) | 모델별 라우팅·응답 검증·재시도·fallback·복구 |
| [openclaude.config.example.yaml](openclaude.config.example.yaml) | 키가 없는 통합 설정 예제 (OpenRouter·Local) |
| [.env.local-llm.example](.env.local-llm.example) | 키가 없는 Local 설정 예제 (폐기 예정, 위 파일로 대체) |
| [tests/claudeRouteProxy.test.mjs](tests/claudeRouteProxy.test.mjs) | 프록시 합성 회귀 검사 |
| [tests/localLlmIntegration.test.mjs](tests/localLlmIntegration.test.mjs) | loopback HTTP 서버를 사용하는 합성 통합 검사 |
| [tests/test_local_config.py](tests/test_local_config.py) | Local 설정과 메타데이터 점검 검사 |
| [tests/test_local_lifecycle.py](tests/test_local_lifecycle.py) | Windows 합성 CLI·실행기 강제 종료 후 프록시·연결 정리 검사 |
| [docs/HISTORY.ko.md](docs/HISTORY.ko.md) | 이전 구현·진단 기록; 현재 정책과 구분 |

`openrouterAdvisor.py`라는 이름은 이전 구현에서 유지한 이름입니다. 이 파일에는 별도의 상담(MCP) 도구가 없으며, Claude Code의 네이티브 advisor와도 무관합니다. 개인 진단 스크립트·로그인 파일·실제 API 키·개인 설정은 배포하지 않습니다.

소스와 문서는 [MIT License](LICENSE)를 적용합니다. 저작권·허가 고지를 유지하는 조건으로 사용·수정·배포할 수 있습니다. 추적 소스 검토에서 Node.js·Python 표준 라이브러리와 별도 설치 Claude CLI를 사용하며, 번들 외부 패키지나 별도 소스 라이선스 고지는 발견하지 못했습니다. 선택 범위와 Apache-2.0 비교는 [라이선스 검토](LICENSE-REVIEW.md)에 기록했습니다.

Claude Code·Node.js·Python·외부 서비스와 모델 자체는 배포 대상이 아니며 각각의 라이선스와 이용 조건이 적용됩니다. MIT는 계정 접근 권한·모델 사용권·요금 정책을 변경하지 않습니다.
