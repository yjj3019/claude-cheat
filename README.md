# Claude-Cheat

**한국어** | [English](README.en.md) · [MIT License](LICENSE)

**현재 버전: 1.0.5** — 대시보드 쓰기 주소(`#token=…`) 자동 오픈 · 키만으로 모델 목록 조회 · 저지연 TCP/HTTP/2/스트리밍 옵션. [릴리즈 이력](docs/HISTORY.ko.md)

Claude Code를 실행하면서 호환되는 Sonnet 요청을 **Local LLM, OpenRouter 또는 CLI 모델 백엔드**로 보내는 로컬 라우터입니다. 원본 Claude Sonnet을 프록시 없이 실행하는 선택지도 제공합니다.

일반 라우팅은 미지원 요청을 Claude Plan으로 보낼 수 있습니다. Local·OpenRouter·CLI 장애는 Plan으로 전환하지 않고 오류를 반환합니다. CLI를 선택해도 전면 실행과 도구 사용은 Claude Code가 담당합니다. Claude Code의 모든 기능이 외부 모델에서도 동일하게 작동한다고 보장하지는 않습니다. [호환성 및 검증 범위](docs/HARNESS-COMPATIBILITY-REVIEW.ko.md) · [요구사항 점검표](docs/REQUIREMENTS-AUDIT.ko.md) · [Claude Code 버전 기준선과 릴리스 체크리스트](docs/CLAUDE-CODE-COMPATIBILITY.ko.md)

| 실행 선택 | 용도 | 확인할 점 |
| --- | --- | --- |
| **Local LLM** | 직접 운영하거나 공유하는 모델 서버 사용 | 서버 주소·모델·필요한 인증 설정 |
| **OpenRouter** | OpenRouter가 제공하는 모델 사용 | API 키와 별도 사용 요금 |
| **CLI** | 지원되는 Claude Code·Codex·Grok CLI를 모델 백엔드로 사용 | 설치·로그인과 지원 버전 확인. Grok 1.0.77 검증(웹검색·서브에이전트 차단, 파일시스템·터미널 도구 제거, 구조화 출력) |
| **원본 Sonnet** | Claude Code를 프록시 없이 직접 실행 | Claude 로그인과 계정 정책 적용 |

## 빠른 시작

Claude Code, Node.js, Python을 설치하고 [준비 사항](#2-준비와-설정-파일-위치)을 확인합니다. 일반 라우팅 모드에는 Claude 로그인이 필요합니다. **Local LLM만 사용하면 OpenRouter 설정은 필요하지 않습니다.**

1. 저장소 폴더에서 설정 화면을 엽니다. 이 터미널은 대시보드 서버 실행용으로 유지합니다.

   ```powershell
   .\OpenClaude.ps1 -Setup
   ```

2. 브라우저의 **설정**에서 사용할 서비스와 모델·접속 정보를 저장합니다. CLI를 사용할 때는 CLI 종류와 모델도 선택합니다. **모델 확인**은 목록 조회이며 추론 성공을 뜻하지 않습니다. 저장은 대시보드가 자동으로 여는 **`#token=…` 주소(write_url)에서만** 됩니다. 일반 주소(`http://127.0.0.1:8787/`)로 직접 연 탭은 읽기 전용이라 `token_required`로 거부됩니다. 모델 목록은 **API 키만 입력해도 조회**되며(모델 미선택이면 `model_not_selected`), 조회 전에 네트워크가 막혀 있으면 `connection_failed`로 표시됩니다.
3. **기본 실행 서비스**에서 사용할 서비스를 골라 **기본값 저장**을 누릅니다. 기존 서비스 설정을 삭제할 필요는 없습니다. 원본 Sonnet은 이 단계에서 바로 선택할 수 있습니다.
4. **새 터미널**의 저장소 폴더에서 실행합니다. 이후에는 `-Provider`를 매번 입력하지 않아도 됩니다.

   ```powershell
   .\OpenClaude.ps1
   ```

5. 대시보드에서 경로와 모델을 확인합니다. AI나 별도 터미널에서는 다음 진단 API를 사용할 수 있습니다. 세션이 없으면 `session_not_running`, 외부 연결을 검사하지 않았으면 `upstream_not_checked`가 표시될 수 있습니다.

   ```powershell
   Invoke-RestMethod http://127.0.0.1:8787/api/diagnostics | ConvertTo-Json -Depth 8
   ```

어느 폴더에서나 `OpenClaude`·`OpenClaudeDashboard`로 실행하려면 **체크아웃 폴더에서 한 번만** 설치합니다. 포워더 생성과 사용자 PATH 등록이 자동으로 처리되고, 이후에는 `installLauncher`로 어느 폴더에서나 갱신할 수 있습니다([전역 실행기 설치와 자동 PATH](#전역-실행기-설치와-자동-path)).

```powershell
cd D:\AI-Codding\Claude\claude-cheat
.\installLauncher.cmd
```

설정 변경은 **다음 새 세션부터** 적용됩니다. 일반 모드의 Plan 우회와 별도로 [Local 전용 모드](#claude-cli-local-전용-모드) 및 [Local 호환 모드](#claude-cli-local-호환-모드)를 제공합니다.

> **요구사항 원장**: 사용자가 명시한 동작은 [`docs/USER-REQUIREMENTS.ko.md`](docs/USER-REQUIREMENTS.ko.md)에 기록되며, 코드·테스트·문서가 이를 어기면 해당 변경을 되돌립니다. 대표적으로 **R1 — 옵션 없이 OpenRouter로 라우팅**이 회귀 감시 테스트로 고정되어 있습니다.

## 목차

자주 찾는 항목: [요구사항 원장](docs/USER-REQUIREMENTS.ko.md) · [전역 실행기와 자동 PATH](#전역-실행기-설치와-자동-path) · [상세 문서 3종](#5-상세-문서)

1. [어떤 요청이 어디로 가나요?](#1-어떤-요청이-어디로-가나요)
2. [준비와 설정 파일 위치](#2-준비와-설정-파일-위치)
3. [OpenRouter 모델 시작하기](#3-openrouter-모델-시작하기)
4. [Local LLM으로 전환하기](#4-local-llm으로-전환하기)
5. [상세 문서](#5-상세-문서) — 동작 원리·문제 해결·검증(분리본)
6. [무중단 재시작 (프록시 교체)](#6-무중단-재시작-프록시-교체)
7. [파일 구성과 라이선스](#7-파일-구성과-라이선스)
## 1. 어떤 요청이 어디로 가나요?

```text
Claude Code — 기존 Plan 로그인·사용자 설정
  └─ 이 PC의 로컬 라우터
       ├─ 호환되는 Sonnet 요청 → 선택한 Local LLM · OpenRouter · CLI 모델
       ├─ 외부 요청이 재시도 정책에 따라 실패 → 기존 Plan Sonnet
       └─ Opus·Haiku·Fable / 외부 미지원 기능 → 기존 Plan 경로
```

| 선택 또는 상황 | 실제 처리 경로 | 인증과 사용량 |
| --- | --- | --- |
| Sonnet + OpenRouter 모드 + 호환 요청 | `OPENROUTER_MODEL`로 지정한 모델 | OpenRouter 키와 별도 API 사용량 |
| Sonnet + Local 모드 + 호환 요청 | 직접 지정한 서버·모델 | 해당 서버 전용 인증과 사용 정책 |
| Sonnet + CLI 모드 + 호환 요청 | 설정한 CLI 모델 백엔드 | CLI 로그인과 해당 서비스 정책 |
| 원본 Sonnet 직접 실행 | 프록시를 거치지 않는 Claude Code | Claude 로그인과 계정 정책 |
| Opus·Haiku·Fable | Anthropic | 저장된 Plan OAuth와 계정 사용량 |
| Sonnet에 외부 미지원 기능·이력이 포함됨 | 원본 Plan Sonnet | Plan 계정 사용량 |
| Local 장애·회로 차단·대기열 초과 | Local 오류 반환, 복구 후 재시도 | Plan 자동 사용 없음 |
| OpenRouter·CLI 장애 | 선택한 제공자의 오류 반환 | Plan 자동 사용 없음 |

- Fable을 별도로 차단하거나 크레딧 OFF 입력을 요구하지 않습니다. 모델 접근 권한과 추가 사용 크레딧은 Claude 앱·계정 설정에 따릅니다.
- Anthropic용 유료 API 키로 자동 전환하는 경로는 없습니다. 다만 **계정에서 허용한 추가 사용 크레딧까지 라우터가 차단하는 것은 아닙니다.**
- 외부 제공자에는 Plan OAuth·계정 헤더를 보내지 않고 제공자 전용 인증을 새로 구성합니다.
- 제공자 장애 시 선택한 Local·OpenRouter·CLI를 다른 제공자나 Plan으로 자동 전환하지 않습니다. Opus·Haiku·Fable 및 외부 미지원 기능은 Plan을 사용할 수 있습니다.
- 기존 MCP·플러그인·HUD·권한 설정을 사용합니다. 기존 서버의 인증 실패나 연결 장애를 자동으로 복구하는 기능은 아닙니다.

## 2. 준비와 설정 파일 위치

별도로 설치한 **Claude Code, Node.js, Python**이 필요합니다. 일반 라우팅 모드는 기존 **Claude Plan 로그인**도 확인하며, 현재 인증 검사는 `subscriptionType="pro"`인 계정을 대상으로 합니다. Max 등 다른 플랜의 지원을 보장하지 않습니다. Plan을 사용하지 않는 Local 전용·호환 모드와 OpenRouter 호환 모드는 각 절의 조건을 따릅니다. 이 소스에 필요한 추가 npm·pip 패키지는 없습니다.

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

일반 실행에서 Claude Code 로그인이 필요하다는 안내가 나오면 같은 터미널에서 `claude auth login`을 실행해 로그인을 완료하세요. `claude auth status`로 상태를 확인한 뒤 `OpenClaude.ps1`을 다시 실행하면 됩니다. 이 과정은 Local LLM 설정을 변경하지 않습니다. 세션이 시작되지 않은 경우 자동화 프로그램에는 종료 코드 `1`이 전달됩니다.

**설정 파일은 현재 작업 폴더가 아니라 `openrouterAdvisor.py`가 있는 폴더에서 읽습니다.**

| 실행 방식 | 읽는 설정 위치 |
| --- | --- |
| 내려받은 저장소에서 `.\OpenClaude.ps1` 실행 | 해당 저장소의 `openclaude.config.yaml` (예전 `.env.openrouter` / `.env.local-llm`은 읽기 전용 폴백) |
| 기존 PC의 전역 `OpenClaude.ps1` 실행 | 전역 실행기가 가리키는 원본 실행기 폴더 |

전역 실행기를 따로 설치했다면 그 대상 폴더에서 설정을 읽습니다. 저장소를 clone해도 전역 명령이 자동 설치되거나 기존 실행기의 대상이 바뀌지 않습니다. 새 checkout은 해당 폴더의 실행기를 명시적으로 실행하세요.

> 실행 스크립트는 **제품명 형태**입니다: `OpenClaude.ps1`(라우팅 세션), `OpenClaudeDashboard.ps1`(대시보드). 이전 이름 `startClaudeOpenRouter.ps1`·`startOpenClaude.ps1`·`startClaudeDashboard.ps1`·`startDashboard.ps1`은 **호환 shim**으로 남아 있어 기존 바로가기·별칭이 그대로 동작합니다(새 이름으로 인자를 넘기고 안내만 출력). 실행 정책이 서명을 요구하는 터미널에서는 같은 동작의 `OpenClaude.cmd`·`OpenClaudeDashboard.cmd`를 쓰면 됩니다.

**어느 폴더에서나 이름만으로 실행하려면** 먼저 **체크아웃 폴더에서** 한 번 설치합니다.

```powershell
cd D:\AI-Codding\Claude\claude-cheat
.\installLauncher.cmd            # 실행 정책과 무관. 포워더 생성 + 사용자 PATH 자동 등록
.\installLauncher.cmd -Remove    # 설치한 포워더 전부 삭제
.\installLauncher.cmd -NoPath    # PATH는 건드리지 않고 포워더만 설치
```

정책이 허용되는 터미널이면 `.\installLauncher.ps1`도 같은 동작입니다. **첫 설치만 체크아웃 폴더에서** 실행하면 되고, 그 다음부터는 어느 폴더에서나 다음 명령을 쓸 수 있습니다.

```powershell
OpenClaude                 # 라우팅 세션
OpenClaudeDashboard        # 대시보드
installLauncher            # 설치·갱신을 어느 폴더에서나 다시 실행
OpenClaude -Help           # 세션을 시작하지 않고 옵션 확인
```

### 전역 실행기 설치와 자동 PATH

```powershell
.\installLauncher.ps1                      # 포워더 설치 + 사용자 PATH 자동 등록
.\installLauncher.ps1 -Remove              # 포워더 삭제 (PATH 항목은 유지)
.\installLauncher.ps1 -NoPath              # PATH는 건드리지 않고 설치만
.\installLauncher.ps1 -Target D:\tools\bin # 다른 폴더에 설치 (이때는 PATH 자동 등록 안 함)
```

설치 스크립트가 자동으로 하는 일:

| 단계 | 동작 | 조건·실패 시 |
| --- | --- | --- |
| 포워더 생성 | `%USERPROFILE%\.local\bin`에 `OpenClaude.cmd`·`OpenClaudeDashboard.cmd`·`installLauncher.cmd`와 이전 이름 포워더 생성 | 같은 이름의 파일이 이 스크립트가 만든 것이 아니면 덮어쓰지 않고 보존 |
| 구형 정리 | 같은 이름으로 남아 있던 생성된 `.ps1` 포워더를 제거 | 사용자가 직접 만든 `.ps1`은 보존 |
| PATH 등록 | 기본 폴더가 **사용자 PATH**에 없으면 자동 추가(관리자 권한 불필요) | `-Target`을 지정했거나 `-NoPath`를 주면 생략 |
| 즉시 반영 | 현재 프로세스 PATH에도 추가하고 새 터미널 안내 | 이미 열려 있던 다른 터미널은 새로 열어야 반영 |

포워더는 `.cmd`라서 PowerShell 실행 정책의 영향을 받지 않고, 내부에서 `-ExecutionPolicy Bypass`로 실제 `.ps1`을 실행합니다. 이전 버전이 설치한 같은 이름의 `.ps1` 포워더는 설치 시 자동으로 제거됩니다(PowerShell이 `.ps1`을 먼저 찾아 다시 막히는 것을 방지). 포워더는 이 체크아웃의 경로를 담으므로, **저장소를 옮기면 다시 설치**하세요.

**명령 해석 규칙** — `OpenClaude`가 인식되지 않을 때 확인하세요:

| 입력 | 결과 | 이유 |
| --- | --- | --- |
| `OpenClaude` | PATH의 `OpenClaude.cmd` 실행 | 확장자 없는 이름을 PATH에서 찾습니다 |
| `OpenClaude.cmd` | 동일 | 명시적 파일명 |
| `OpenClaudeDashboard` | PATH의 `OpenClaudeDashboard.cmd` 실행 | 대시보드 |
| `installLauncher` | PATH의 `installLauncher.cmd` 실행(설치·갱신) | 첫 설치 후에는 어느 폴더에서나 갱신할 수 있습니다 |
| `openClaude.ps1` / `installLauncher.ps1` | **인식 안 됨** | PATH에 `.ps1` 파일이 없습니다. `.ps1`은 체크아웃 폴더에만 있습니다 |
| `.\OpenClaude.ps1` / `.\installLauncher.ps1` | 체크아웃 폴더에서만 실행 | 현재 폴더 기준 상대 경로 |
| `OpenClaudeDashboard -Open` | 대시보드 + 설정 페이지 열기 | 쓰기 토큰이 포함된 `write_url`로 엽니다 |

**동작 확인과 복구:**

```powershell
Get-Command OpenClaude, OpenClaudeDashboard, installLauncher | Select-Object Name, Source
# OpenClaude.cmd          C:\Users\<사용자>\.local\bin\OpenClaude.cmd
# OpenClaudeDashboard.cmd C:\Users\<사용자>\.local\bin\OpenClaudeDashboard.cmd
# installLauncher.cmd     C:\Users\<사용자>\.local\bin\installLauncher.cmd

where.exe OpenClaude
$env:PATH -split ';' | Select-String '\.local\\bin'
```

인식되지 않으면 순서대로 확인하세요.

1. **새 터미널**을 엽니다(PATH 변경은 새 프로세스부터 적용).
2. `$env:PATH`에 `C:\Users\<사용자>\.local\bin`이 있는지 확인합니다.
3. 없으면 체크아웃 폴더에서 `.\installLauncher.ps1`을 다시 실행합니다.
4. 실행 정책 오류가 나면 `.\installLauncher.cmd`를 사용합니다(아래 참고).
5. 회사 GPO가 `AllSigned`를 강제하면 우회가 막히므로 관리자 정책 변경이 필요합니다.

> `-Remove`는 포워더만 삭제하고 PATH 항목은 남깁니다. PATH에서도 빼려면 Windows **환경 변수**에서 직접 제거하세요.

> **실행 정책으로 `.ps1`이 막히면**: 스크립트가 서명되지 않았으므로 `AllSigned`(또는 서명을 요구하는 관리 정책) 터미널에서는 `파일이 디지털 서명되지 않았습니다` 오류가 납니다. 이때는 `.cmd` 진입점을 사용하세요 — 설치(`.\installLauncher.cmd`), 체크아웃 직접 실행(`.\OpenClaude.cmd`·`.\OpenClaudeDashboard.cmd`), PATH의 `OpenClaude`/`OpenClaudeDashboard` 모두 정책과 무관하게 동작합니다. 그래도 막히면 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\installLauncher.ps1`처럼 실행하거나 현재 사용자 범위 정책을 `RemoteSigned`로 두세요(`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, 관리자 권한 불필요). 회사 GPO가 `AllSigned`를 강제하면 `-ExecutionPolicy Bypass`도 무시되므로 관리자에게 문의해야 합니다.

예전 `.env.*` 파일을 계속 읽는 경우 형식은 한 줄에 `KEY=VALUE` 하나입니다. 빈 줄·`#`으로 시작하는 주석·값 전체를 감싼 따옴표는 허용하며, 여러 줄 값·셸 변수 확장은 지원하지 않습니다. 키 뒤에 같은 줄 주석을 붙이지 마세요. 새 설정은 `openclaude.config.yaml`을 사용합니다. 실제 설정 파일은 Git에서 제외하며 예제에는 실제 인증값을 넣지 마세요.

## 3. OpenRouter 모델 시작하기

**처음 한 번은 웹 콘솔에서 설정하는 것을 권합니다.** 콘솔은 라우팅 세션이 없어도 실행되고, 설정이 비어 있으면 화면에 시작 안내가 표시됩니다.

```powershell
.\OpenClaudeDashboard.ps1        # http://127.0.0.1:8787/ (쓰기 주소는 출력된 write_url 사용, 포트 고정)
.\OpenClaude.ps1 -Setup           # 같은 대시보드를 띄우고 설정 페이지를 브라우저로 엽니다
```

`-Setup`은 설정 파일(`openclaude.config.yaml`과 예전 `.env.*`)을 만들거나 고치지 않고 세션도 시작하지 않습니다. 값은 웹 페이지에서 직접 저장합니다. 설정이 없을 때 `OpenClaude.ps1`이 출력하는 `[setup required]` 안내에도 이 명령이 포함됩니다.

상단의 **설정할 서비스** 선택 상자에서 OpenRouter·Local LLM·CLI를 고르면 해당 서비스의 입력란만 표시됩니다(옵션에 `설정됨`/`설정 필요`가 함께 표시되고, 마지막 선택은 브라우저에 기억됩니다). OpenRouter라면 **모델 ID**와 **API 키**를 입력하고 **모델 확인** → **저장**을 누른 뒤 세션을 시작하세요. 모델 확인은 저장 전 값으로도 실행되며, `openclaude.config.yaml`이 없으면 저장 시 `openclaude.config.example.yaml`의 주석을 그대로 가져와 값만 채웁니다. API 키와 서버 주소는 화면에 일부만 표시되고 저장된 원문은 브라우저로 전송되지 않으므로, 바꿀 때만 전체를 입력합니다(비워 두면 유지, 선택 항목인 HTTP 예외·모델 목록 주소는 **지우기**로 삭제). 설정이 없거나 잘못된 상태로 실행기를 시작하면 세션을 열지 않고 콘솔 주소를 안내합니다.

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

`model`에는 [OpenRouter 모델 목록](https://openrouter.ai/models)의 구체적인 모델 ID를 입력합니다. 기존에 키만 설정했다면 이 항목을 추가해야 합니다. 누락·빈 값·예제 플레이스홀더는 시작 오류로 종료하며 다른 모델이나 Plan으로 자동 대체하지 않습니다. 선택적인 `provider`에는 `open-inference/fp4` 같은 OpenRouter 상류 공급자 slug를 입력할 수 있습니다. 비워 두면 Auto로 OpenRouter가 공급자를 선택합니다. 지정하면 `only`로 제한하고 다른 OpenRouter 공급자로의 fallback을 허용하지 않습니다. 해당 공급자가 모델을 지원하지 않거나 사용할 수 없으면 오류를 반환하며 Plan으로 전환하지 않습니다. 설정 화면에서 **OpenRouter 상류 공급자**를 비우면 Auto로 돌아갑니다. 새 세션 또는 takeover부터 적용되며, 모델 목록 확인만으로 특정 공급자의 사용 가능 여부를 증명하지 않습니다.

선택적인 `reasoning`(2026-11-07 추가)은 OpenRouter에 추론 파라미터를 요청합니다. 비우거나 `off`면 요청 본문에 `reasoning`을 넣지 않습니다(이전 동작). `8192` 같은 양의 정수는 추론 토큰 예산(`reasoning.max_tokens`)으로, `low|medium|high|xhigh|max|minimal`은 명명된 effort(`reasoning.effort`)로 전달됩니다. 일반(ordinary) OpenRouter 세션의 외부 요청에만 적용되며, Plan·Local·CLI 경로와 호환 모드에는 영향이 없습니다. 응답의 `reasoning_details`는 검증 없이 그대로 Claude Code로 전달되고, Claude Code가 이를 해석할 수 있다면 표시됩니다. 공급자가 해당 모델의 reasoning을 지원하지 않으면 OpenRouter가 무시하거나 오류를 반환할 수 있으며, 이 경우 `reasoning`을 비우세요. 듀얼 공급자 세션에서도 OpenRouter 쪽에 같은 설정이 적용됩니다.

모델 선택이 가능해도 모든 모델의 도구 호출과 네이티브 기능을 보장하지 않습니다. 선택 모델이 [Messages API](https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-messages?explorer=true)와 필요한 도구 호출을 지원하는지 확인하세요. 응답의 모델 ID를 설정값과 정확히 비교하므로, 다른 ID로 응답하는 동적 별칭·프리셋은 실패할 수 있으며 미검증입니다. API 주소와 재시도 정책은 예제 파일에 고정값으로 구분했습니다.

### ② 실행하고 Sonnet 선택하기

```powershell
# 실행기 폴더에서 OpenRouter를 명시적으로 선택
.\OpenClaude.ps1 -Provider OpenRouter
```

Claude Code에서 `/model`로 **Sonnet**을 선택합니다. 호환되는 요청은 OpenRouter의 `https://openrouter.ai/api/v1/messages`로 보내며, 대체 모델은 `openclaude.config.yaml`의 `openrouter.model`로 지정합니다. 특정 모델을 기본값으로 선택하지 않습니다.

기존 전역 실행기가 설치된 PC에서는 다른 작업 폴더에서도 다음처럼 실행할 수 있습니다.

```powershell
OpenClaude.ps1 -Provider OpenRouter
```

실행기는 해당 Claude 프로세스의 연결 주소만 로컬 라우터로 설정하고 기존 Plan 인증을 검사합니다. 저장된 로그인·사용자 설정을 API용 설정으로 덮어쓰지 않습니다. `/status`의 Plan 로그인 표시는 인증 상태이며 **현재 응답을 OpenRouter 모델이 만들었다는 증거는 아닙니다.**

### 기본 서비스를 저장하고 옵션 없이 실행하기

Claude-Cheat는 프로젝트 이름이며, OpenRouter·Local LLM·CLI는 연결할 서비스입니다. 대시보드에는 **설정할 서비스**와 **기본 실행 서비스**가 구분되어 있습니다. 다른 서비스의 키나 모델을 편집하는 것만으로 기본 실행 대상이 바뀌지는 않습니다.

1. `.\OpenClaude.ps1 -Setup`으로 설정 화면을 엽니다.
2. **설정할 서비스**에서 OpenRouter·Local LLM·CLI 중 하나를 골라 모델·접속 설정을 입력하고 **저장**합니다.
3. **기본 실행 서비스**에서 사용할 서비스를 고르고 **기본값 저장**을 누릅니다.
4. 새 PowerShell 터미널에서 `.\OpenClaude.ps1`을 실행합니다. 이후에는 `-Provider`를 매번 입력할 필요가 없습니다.

기본값은 브라우저가 아니라 실행기 폴더의 `openclaude.config.yaml`에 저장됩니다. 다른 브라우저·터미널에서도 같은 실행기와 설정 파일을 사용하면 같은 기본값을 읽습니다. 저장만으로 Claude 세션을 시작하거나 실행 중인 세션을 전환하지는 않습니다.

| 실행 상황 | 현재 동작 |
| --- | --- |
| `.\OpenClaude.ps1` 실행, 기본 서비스 저장됨 | 저장된 서비스 사용 |
| 기본값이 `auto`이거나 없는 기존 파일, 사용 가능한 서비스가 하나 | 해당 서비스 자동 선택 |
| 기본값이 `auto`이거나 없는 기존 파일, 여러 서비스 사용 가능 | `provider_choice_required` 안내 후 종료 코드 `2`; 기본값 저장 또는 일회성 옵션 필요 |
| `-Provider OpenRouter`, `-Provider Local`, `-Provider CLI` 지정 | 저장된 기본값보다 우선; 해당 실행에만 적용 |
| `.\OpenClaude.ps1 -Takeover` | 기본값을 바꿨어도 기존 세션의 서비스를 유지하며 프록시 교체 |
| 대시보드에서 CLI 선택·저장 | Claude Code를 유지하고 선택한 CLI를 모델 백엔드로 사용; 새 세션부터 적용 |

세 서비스의 설정은 함께 보관할 수 있습니다. 기본값을 저장하기 위해 다른 서비스 설정을 지울 필요는 없습니다. 서비스 설정을 먼저 저장해야 기본값으로 지정할 수 있으며, CLI도 기본 실행 서비스로 저장할 수 있습니다. CLI 종류와 모델을 먼저 저장하세요.

```powershell
# 저장된 기본 서비스로 새 세션 시작
.\OpenClaude.ps1

# 이번 실행만 OpenRouter 사용
.\OpenClaude.ps1 -Provider OpenRouter

# 이번 실행만 Local LLM 사용
.\OpenClaude.ps1 -Provider Local

# 설정 화면 열기 (세션을 시작하지 않음)
.\OpenClaude.ps1 -Setup

# 옵션·실행 예제·종료 코드 확인 (세션을 시작하지 않음)
.\OpenClaude.ps1 -Help
```

직접 설정할 때는 다음 항목을 사용합니다. 허용값은 `auto`, `openrouter`, `local`, `cli`입니다.

```yaml
routing:
  default_provider: "local"
```

저장된 서비스의 설정이 잘못되면 다른 서비스로 자동 변경하지 않고 오류를 안내합니다. 기본값 자체가 잘못된 경우 설정 화면에서 올바른 값을 다시 저장하거나 **자동 선택**으로 돌릴 수 있습니다. `-LocalMode`·`-LocalCompat`·`-OpenRouterCompat`은 기존대로 명시적인 `-Provider`가 필요합니다.

`startOpenClaude.ps1 is now OpenClaude.ps1` 경고는 예전 명령을 새 명령으로 연결했다는 뜻입니다. 새 명령을 사용하면 이 경고는 나오지 않습니다. `provider_choice_required`는 서비스 선택이 필요하다는 안내이며, 연결 실패나 API 키 오류를 뜻하지 않습니다. `-Provider`는 새로 실행할 세션에 적용되며 이미 실행 중인 세션을 전환하지 않습니다.

서비스 선택 API는 `POST /api/default-provider`에 `{"provider":"local"}` 형태로 요청합니다. `GET /api/config`의 `default_provider`와 `default_provider_error`로 저장 상태를 확인할 수 있습니다. 읽기·저장에는 페이지의 세션 토큰이 필요하며, 저장에는 기존 Origin·Host·요청 크기·횟수 제한도 적용됩니다.

이번 실행 흐름과 문서 개선은 Cursor의 [CLI 설계 지침](https://github.com/cursor/plugins/blob/main/cli-for-agent/skills/cli-for-agents/SKILL.md)과 [문서 신뢰성 검토 지침](https://github.com/cursor/plugins/blob/main/agent-compatibility/agents/docs-reliability-review.md)을 참고했습니다. 명시적 옵션·실행 예제·조치 가능한 오류·문서와 동작의 일치를 적용 대상으로 삼았습니다. Claude-Cheat는 Cursor 플러그인이나 해당 저장소 전체의 이식판이 아니며, 해당 저장소 전체를 이식했다고 주장하지 않습니다.

일반 OpenRouter·Local·CLI 실행은 원본 Claude Code의 beta·advisor 설정을 임의로 끄지 않습니다. 지원되는 요청만 외부 경로로 보내고 미지원 네이티브 기능은 Plan으로 보냅니다. 명시적인 호환·제한 모드는 별도 정책을 적용합니다.

Local 단독 일반 세션에서는 이 두 비활성화 스위치를 설정하지 않으며, SSH/MCP·도구 검색·플러그인·hooks·사용자/프로젝트 권한 설정을 유지합니다. 알려진 MCP 지연 로딩·CLI 제어 필드는 기존 호환성 검증을 거쳐 Local로 전달합니다. 원본 CLI의 `tool_addition` 선언은 대화 내용과 도구 결과를 유지하면서 Local 도구 목록으로 변환합니다. 충돌하는 도구 재정의·알 수 없는 형식은 원본 대화와 도구 정의를 유지한 Plan 경로로 보냅니다. CLI가 도구 이력에 넣는 `provider_specific_fields: null`만 값 없음으로 정리하며, 실제 객체 데이터나 서명은 이 규칙으로 제거하지 않습니다. 캐시 경계는 서버 형식에 맞게 바뀔 수 있습니다. 해당 요청은 JSON 응답을 검증한 뒤 필요한 경우 CLI용 SSE로 바꿉니다. 지원하지 않는 네이티브 기능과 Opus/Haiku/Fable 요청에는 기존 Plan 경로를 유지하며 Local 실패는 오류를 반환합니다. Local 추론 서명이 Plan 이력과 충돌하지 않도록 일반 모드의 외부 thinking 비활성화 정책도 유지합니다.

일반 Local 실행의 기본 출력 예산은 8,192토큰입니다. 원본 Sonnet의 큰 출력 한도로 Local 문맥 한도를 초과하는 문제를 줄이며, 명시적인 `CLAUDE_CODE_MAX_OUTPUT_TOKENS` 양의 정수값은 그대로 사용합니다. 이 값만 실행별 설정으로 전달하고 사용자·프로젝트 설정 파일은 수정하지 않습니다. 큰 입력·명시적인 큰 출력 예산은 여전히 모델 한도를 초과할 수 있습니다. `-LocalMode`는 MCP 등을 차단하는 별도 제한 모드이므로 원본 Claude Code의 연동 기능을 유지하려면 일반 실행을 사용하세요.

### OpenRouter 호환 모드: Plan 전환 차단

```powershell
.\OpenClaude.ps1 -Provider OpenRouter -OpenRouterCompat
```

Claude Code 2.1.304 이상에서 `openclaude.config.yaml`의 같은 OpenRouter 키와 `openrouter.model`을 사용합니다. 주 모델·빠른 모델·하위 에이전트의 Claude 모델 선택을 모두 지정한 같은 OpenRouter 모델로 전달하며, Plan 로그인과 Plan fallback은 사용하지 않습니다. 기존 일반 실행과 Local 모드는 유지합니다.

**일반 OpenRouter 세션도 옵션 없이 동작합니다.** `OpenClaude`(또는 `-Provider OpenRouter`)로 시작한 일반 세션은 지원되는 요청을 설정한 OpenRouter 모델로 라우팅합니다. `-OpenRouterCompat`은 여기에 더해 **네이티브 예외의 Plan 전환도 차단**할 때 사용합니다.

> 과거 `e075542`(CLI 백엔드 통합)에서 이 처리가 호환 모드에만 남도록 좁혀져 일반 OpenRouter 세션이 모든 요청을 Plan으로 보내는 회귀가 있었습니다. 기본 동작으로 복원했으며, 테스트가 다시 회귀하지 않도록 고정합니다.

LocalCompat과 같은 호환 처리로 플러그인·스킬·MCP·hooks·하위 에이전트 및 thinking·추론 강도 설정을 허용합니다. 사용자·프로젝트 설정과 기본 승인·거절 규칙을 유지하고, API 키 helper는 끕니다. 실행 설정으로 프록시 주소와 임시 인증을 고정하며 제공자 키는 프록시에만 전달합니다. 알려진 CLI 제어 필드만 변환하고, 지원하지 않는 beta·필드·토큰 계산 API는 명시적으로 거절합니다.

응답은 JSON 전체를 검증한 뒤 CLI용 SSE로 전달합니다. 요청당 120초·외부 시도 1회·오류 후 30초 재요청 대기를 적용하며 OpenRouter의 제공자 fallback도 차단합니다. 선택 모델의 Messages API·도구·thinking 지원과 정확한 응답 모델 ID가 필요합니다. 추론 강도 전달이 Sonnet과 동일한 추론 효과를 보증하지는 않습니다.

### 실행 전 연결 점검

모든 대화형 실행은 선택 제공자의 설정으로 최소 추론을 1회 점검한 뒤 결과를 출력합니다. 제한은 15초·출력 512토큰이며 OpenRouter 사용료가 발생할 수 있습니다. `connection_check`, `provider`, `inference_checked`, `model_confirmed`, `reply_confirmed`, `reason`과 필요한 HTTP `status`만 기록하고 주소·키·본문은 출력하지 않습니다.

점검 실패도 CLI 진입을 계속합니다. 일반 실행에서 제공자 장애는 Plan으로 fallback하지 않지만 네이티브 기능과 Opus·Haiku·Fable은 Plan을 사용할 수 있습니다. `-LocalMode`·`-LocalCompat`·`-OpenRouterCompat`은 Plan을 차단합니다. 실패 시 `provider_connection_unavailable`의 `action`은 `continue`, `plan_fallback`은 장애 fallback 정책을 표시합니다. 이후 요청에서 제공자 연결을 다시 시도할 수 있습니다. 설정 누락·잘못된 설정·CLI 실행 불가 같은 시작 오류는 별도로 종료합니다.

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
| `LOCAL_LLM_HTTP_URL` | 선택 항목이며 기본값은 비어 있음. `LOCAL_LLM_URL`과 정확히 같은 HTTP Messages 주소를 지정한 경우에만 평문 HTTP 예외 허용. 실제 주소는 Git에서 제외되는 설정 파일에 두고 HTTPS 사용을 우선 |
| `LOCAL_LLM_MODELS_URL` | 같은 origin의 `/v1/models`; 모델 목록 점검에만 사용 |

같은 이름의 **프로세스 환경변수가 파일보다 우선**합니다. `localhost` 대신 허용되는 주소 리터럴 `127.0.0.1` 또는 `[::1]`을 사용하세요. 정규화 전 경로가 정확히 `/v1/messages`여야 하며 사용자 정보는 허용하지 않습니다. 원격 HTTP는 `LOCAL_LLM_HTTP_URL`이 `LOCAL_LLM_URL`과 정확히 일치할 때만 허용하며, 이 경우 프롬프트와 `LOCAL_LLM_API_KEY` Bearer 키가 암호화 없이 전송됩니다. 이 예외는 주소가 사설망인지 검증하지 않으므로 신뢰할 수 있는 대상에만 설정하세요. 그 외에는 HTTPS 또는 별도로 준비한 loopback 터널이 필요합니다.

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

다시 OpenRouter를 쓰려면 세션을 종료하고 `.\OpenClaude.ps1 -Provider OpenRouter`로 재실행합니다. 실행 중인 세션에서 `/model`은 기본적으로 Claude 모델을 선택하는 메뉴이고 OpenRouter/Local 제공자를 바꾸지 않습니다(아래 옵트인은 예외).

`-Provider`의 기본값은 **`Auto`** 입니다. 먼저 저장된 기본 실행 서비스를 사용합니다. 기본값이 없거나 `auto`이면 설정된 서비스가 하나일 때 자동 선택하며, 둘 다 유효하면 아래처럼 선택을 안내하고 종료합니다(종료 코드 2). 매번 옵션을 입력하는 대신 [기본 실행 서비스를 저장](#기본-서비스를-저장하고-옵션-없이-실행하기)할 수 있습니다.

```text
Both OpenRouter and Local settings are configured. Choose one explicitly:
  OpenClaude.ps1 -Provider OpenRouter
  OpenClaude.ps1 -Provider Local
```

설정 화면의 **설정할 서비스** 선택 상자에는 서비스별 설정 여부가 표시됩니다. **기본 실행 서비스** 영역에는 저장된 기본값과 현재 세션의 서비스가 표시됩니다(`/api/status`·`/api/snapshot`의 `providers`, `active_session`). `-Takeover`는 `-Provider`를 생략하면 저장된 기본값 대신 인수인계 대상 세션의 서비스를 그대로 따릅니다.

### 한 세션에서 두 서비스 함께 쓰기 (옵트인)

`openclaude.config.yaml`에 `routing: dual_provider: true`를 넣고 **두 서비스가 모두 설정돼 있으면**, 일반 세션이 Claude Code의 `/model` 목록에 다른 쪽 서비스를 항목 하나로 더 보여 줍니다(모델 id `claude-route-local` 또는 `claude-route-openrouter`, 예: `Local (qwen3-coder:latest)`).

- 세션의 기본 provider(`-Provider`로 고른 쪽)는 그대로 기본값입니다. `/model`에서 다른 항목을 고른 **그 요청만** 다른 provider로 갑니다.
- 네이티브 기능은 Plan(구독)으로 갈 수 있습니다. Local·OpenRouter 장애는 Plan으로 fallback하지 않습니다. 실패는 **그 provider의 서킷 브레이커**만 열고, 한쪽이 죽어도 다른 쪽 요청에는 영향이 없습니다.
- 다른 provider의 자격증명은 **프록시 프로세스에만** 전달되고 CLI 환경변수에는 절대 들어가지 않습니다. 라우팅 로그의 `route_selected`/`circuit_open`은 어느 provider로 갔는지 `provider` 필드로 알려 줍니다.
- 대체 Local에도 일반 Local의 MCP·인라인 도구 호환 변환을 적용합니다. OpenRouter는 검증 가능한 인라인 도구 선언만 변환해 전송하며, 변환할 수 없는 네이티브 요청은 기존처럼 Plan을 사용할 수 있습니다.
- `-LocalMode`·`-LocalCompat`·`-OpenRouterCompat` 같은 특수 모드는 요청을 재작성하고 Plan fallback을 끄므로 이 기능을 무시합니다(항목도 만들지 않습니다).
- `/model` 항목은 세션 시작 시 환경변수(`ANTHROPIC_CUSTOM_MODEL_OPTION*`)로 주입되므로, 옵션을 바꾸면 세션을 다시 시작해야 보입니다. 항목 표시는 CLI 버전에 좌우되고(2.1.310에서 훅 확인), 프록시의 모델 id → provider 디스패치는 단위 테스트로 검증됩니다.
- `-Takeover`로 세션을 넘길 때는 인수인계 기록에 이 구성(provider·별칭·모델 id, 자격증명 없음)이 함께 저장되고, 새 프록시가 같은 별칭을 계속 받아들일 수 없는 설정(기능 해제·대체 서비스 변경·provider 전환)이라면 드레인 전에 실행을 거부합니다. 실행 중인 CLI의 항목이 갑자기 403이 되는 일을 막기 위함이며, 대체 서비스 자격증명은 항상 현재 설정에서 다시 읽습니다.

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

## 5. 상세 문서

상세 내용은 README가 길어지는 것을 막기 위해 분리해 두었습니다. 필요할 때만 열어 보세요.

- [동작 참조 — 라우팅·회로·재시도·fallback·대시보드·사용량·첫 토큰 지연](docs/PROXY-REFERENCE.ko.md)
- [문제 해결 — 오류와 일반 Claude 복귀](docs/TROUBLESHOOTING.ko.md)
- [검증 방법과 남은 한계 — 벤치·시뮬레이션·성능 수치](docs/VALIDATION.ko.md)

## 6. 무중단 재시작 (프록시 교체)
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
$env:CLAUDE_CHEAT_LIVE = '0'               # 기본 검증에서는 실제 CLI·제공자 추론을 비활성화
node --test tests/*.test.mjs
python -m unittest discover -s tests -p 'test_*.py' -v
python openrouterAdvisor.py --self-test
node --check claudeRouteProxy.mjs
node tests/simulate.mjs --soak 3000        # 합성 트래픽·장기 세션·메모리
node tests/fuzz.mjs --iterations 2000      # 프록시 퍼징(결정적 시드)
python -m unittest discover -s tests -p test_fuzz_dashboard.py  # 대시보드 퍼징
python tests/live_smoke.py --dry-run       # 설정 존재 여부만 확인(무과금)
```

현재 저장소 기준 회귀 검사 수는 **Node 726개**(대시보드 DOM 99개 포함), **Python 712개**(11개는 환경 의존 검사로 skip)입니다. 테스트 수는 릴리스마다 달라질 수 있으므로 위 명령으로 직접 확인하세요.

- Windows 프로세스 트리 테스트(`test_local_lifecycle`)와 동시성 스트레스(`test_dashboard_concurrency`)는 **기본 스위트에서 건너뜁니다**. 실제 프로세스 트리를 띄우거나 서버에 12스레드를 걸기 때문에 느리고 부하에 민감합니다. 필요할 때만 `$env:CLAUDE_CHEAT_SLOW = '1'`로 켜세요.
- 프로세스 트리 테스트는 **Git Bash**가 필요합니다. `C:\Windows\System32\bash.exe`(WSL)는 Windows 파이썬 경로를 실행하지 못해 자동으로 건너뜁니다.
- 대시보드는 **Host 허용목록**(127.0.0.1·localhost·::1)으로 DNS 리바인딩을 막고, `nosniff`·`no-referrer`·`frame-ancestors DENY`(X-Frame-Options) 헤더를 보냅니다. 쓰기 API는 세션 토큰 + 동일 출처를 함께 요구합니다.
- 실제 제공자 추론은 기본 스위트에서 제외되어 있습니다. 설정 변경 후 한 번 확인하려면 `python tests/live_smoke.py`를 사용할 수 있으며, 실제 호출과 모델 요금 정책에 따라 비용이 발생합니다.
- 실행기에는 **비대화형 `--print "프롬프트"`** 모드가 있습니다(기본 JSON 출력, `--print-text`로 사람용). `python openrouterAdvisor.py --print "Reply with exactly OK"` 또는 `$env:CLAUDE_CHEAT_LIVE = '1'; python -m unittest discover -s tests -p test_cli_integration.py`는 실제 CLI·제공자를 호출하며 비용이 발생할 수 있습니다.
- `node --test tests/cli_smoke.test.mjs`는 합성 CLI·모의 제공자로 실험용 스모크 하네스만 검증합니다. 별도 `node tests/cli_smoke.mjs`는 사용자 설정과 실제 CLI·제공자를 사용하는 **실험용 유료 가능 검사**이며 실제 호환성은 미검증입니다. Windows의 `.cmd` 셸 래퍼 실행은 지원하지 않으며, 시간 초과 시 직접 자식 프로세스만 종료하므로 손자 프로세스 정리는 보장하지 않습니다.

검사 대상은 요청·응답 보존, 인증 격리, 도구 결과 왕복, 최초 요청과 재시도 3회, Plan fallback, 회로 복구, 취소·타임아웃·빈 응답 처리입니다. 합성 테스트 통과를 실제 서비스나 모든 Claude Code 기능의 통과로 해석하면 안 됩니다.

### 시뮬레이션과 성능 측정

모의 업스트림으로 트래픽을 만들어 처리량·지연·메모리·라우팅 결과를 측정합니다. 외부 모델을 호출하지 않습니다.

측정 기준선·실험 기록·다음 개선 후보는 [`docs/PROXY-PERFORMANCE-MONITORING-RESEARCH.ko.md`](docs/PROXY-PERFORMANCE-MONITORING-RESEARCH.ko.md)에 계속 누적합니다.

```powershell
node tests/simulate.mjs                                   # 혼합 트래픽 + 장기 세션 + 회로 차단
node tests/simulate.mjs --requests 4000 --concurrency 48 --json
node tests/bench_proxy.mjs --quick                        # 새 프로세스 반복 측정 + 정확성 게이트
node tests/bench_proxy.mjs --soak-seconds 10              # 시간 기준 소크(자식 프록시 RSS·GC 후 heap)
node tests/bench_proxy.mjs --drift-json .bench\이전-결과.json --drift-max-ratio 1.4   # 워크로드 p90 드리프트 게이트
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
| 회로 | 측정 당시: 실패 4회 → Plan fallback, 쿨다운 중 Plan, 복구 후 외부 복귀. 현재 제공자 장애는 오류를 반환 |
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
- 프록시의 추론 경로는 POST `/v1/messages`, `/v1/messages/count_tokens`(선택적 `?beta=true`)입니다. 같은 세션 토큰이 필요한 HEAD `/api/hello`, GET `/api/metrics`, POST `/api/handover`도 제공합니다. 그 밖의 프록시 경로는 구독으로 우회하지 않고 거부합니다. 대시보드의 `/api/diagnostics` 등은 별도 대시보드 포트에서 제공합니다.
- `CLAUDE_CONFIG_DIR`가 미설정·빈 값·과거 전용 `~/.claude-openrouter`이면 자식 환경에서 제거해 원본 Claude Code의 설정 탐색을 유지합니다. 사용자가 명시한 경로는 `~/.claude`를 포함해 그대로 보존합니다. `~/.claude`를 강제로 지정하면 개인 MCP 설정을 읽는 `.claude.json` 위치까지 바뀌므로 기본값처럼 주입하지 않습니다([공식 MCP 저장 위치 안내](https://code.claude.com/docs/en/mcp-quickstart#find-your-configuration-on-disk)). 폴더·설정·인증 파일을 복사·이전·삭제하지 않습니다.
- Local·compat 격리 세션의 임의 토큰(nonce)은 Claude Code 프로세스의 `--settings` 인자와 환경변수에 들어갑니다. 같은 사용자 권한의 프로세스(명령줄을 수집하는 EDR·감사 도구 포함)가 읽을 수 있고, 유출되면 해당 세션 동안 로컬 프록시를 통해 OpenRouter/Local 사용량을 소비할 수 있습니다. 세션마다 새로 생성하며 `127.0.0.1`에만 바인드합니다. Plan 접근 권한은 주지 않습니다.
- 외부 서비스(Local·OpenRouter·CLI)는 모두 백엔드 응답의 모델 ID·콘텐츠·종료 사유를 검증합니다. OpenRouter에는 `id`·`role=assistant`·토큰 `usage`도 요구하며, SSE `event:` 이름과 JSON `type`이 다르면 거부합니다. 일반 Local과 Local 호환 모드는 OpenRouter 전용 메타데이터 요구를 적용하지 않지만 공통 검증은 유지합니다. Local 전용 모드에서 JSON을 CLI용 스트림으로 변환할 때는 메타데이터도 요구합니다. 2026-10-24 실호출 결과는 당시 검증한 모델의 기록이며 다른 모델의 호환성을 보장하지 않습니다.
- `claude`가 npm 설치의 `.cmd`/`.bat` shim으로 해석되면 격리 모드(Local·OpenRouter compat)는 경고를 출력합니다. cmd.exe가 shim을 거치는 인자의 따옴표·`&`·`%`를 잘못 해석할 가능성이 있으나 실제 재현은 하지 못했습니다. 네이티브 `claude.exe` 설치를 권장합니다.
- HTTP 429가 발생하면 해당 계정의 Usage 화면과 응답을 직접 확인해야 합니다. 특정 계정의 한도·초기화 일정은 저장소에서 관리하지 않습니다.

일반적인 검증 절차는 [연결·장애 복구 점검](docs/LIVE-VALIDATION.ko.md)을 참고하세요.

## 10. 파일 구성과 라이선스

| 파일 | 역할 |
| --- | --- |
| [OpenClaude.ps1](OpenClaude.ps1) | 제공자 선택과 Python 실행기 호출 |
| [OpenClaudeDashboard.ps1](OpenClaudeDashboard.ps1) | 대시보드·설정 화면 실행 |
| [OpenClaude.cmd](OpenClaude.cmd)·[OpenClaudeDashboard.cmd](OpenClaudeDashboard.cmd) | 실행 정책이 서명을 요구하는 터미널용 `.cmd` 진입점(내부에서 `-ExecutionPolicy Bypass`) |
| [installLauncher.ps1](installLauncher.ps1) | 다른 폴더에서 사용할 전역 실행기 설치·갱신 + 사용자 PATH 자동 등록(`-NoPath`로 생략) |
| [installLauncher.cmd](installLauncher.cmd) | 실행 정책이 `AllSigned`인 터미널에서도 설치·갱신(내부에서 `-ExecutionPolicy Bypass`) |
| [openrouterAdvisor.py](openrouterAdvisor.py) | Plan 인증 검사·환경 격리·프록시 수명 관리·Local 목록 점검 |
| [routeConfig.py](routeConfig.py) | 통합 설정 읽기·검증·원자적 저장·백업 |
| [routeDashboard.py](routeDashboard.py) | 상태 화면·설정·모델 목록·AI 진단 API |
| [localProcessTree.py](localProcessTree.py) | Windows Local 전용 진입점의 Job 소유권·프로세스 트리 종료 감시 |
| [claudeRouteProxy.mjs](claudeRouteProxy.mjs) | 모델별 라우팅·응답 검증·재시도·fallback·복구 |
| [openclaude.config.example.yaml](openclaude.config.example.yaml) | 키가 없는 통합 설정 예제 (OpenRouter·Local) |
| [.env.local-llm.example](.env.local-llm.example) | 키가 없는 Local 설정 예제 (폐기 예정, 위 파일로 대체) |
| [tests/claudeRouteProxy.test.mjs](tests/claudeRouteProxy.test.mjs) | 프록시 합성 회귀 검사 |
| [tests/localLlmIntegration.test.mjs](tests/localLlmIntegration.test.mjs) | loopback HTTP 서버를 사용하는 합성 통합 검사 |
| [tests/test_local_config.py](tests/test_local_config.py) | Local 설정과 메타데이터 점검 검사 |
| [tests/test_route_dashboard.py](tests/test_route_dashboard.py) | 설정·진단 API·모델 중복 제거·실제 로컬 프록시 연동 검사 |
| [tests/dashboard_dom.test.mjs](tests/dashboard_dom.test.mjs) | 실제 생성 페이지의 JavaScript 동작 검사 |
| [tests/metrics.test.mjs](tests/metrics.test.mjs) | 프록시의 로드 모델·메트릭·민감값 제외 검사 |
| [tests/test_local_lifecycle.py](tests/test_local_lifecycle.py) | Windows 합성 CLI·실행기 강제 종료 후 프록시·연결 정리 검사 |
| [docs/HISTORY.ko.md](docs/HISTORY.ko.md) | 이전 구현·진단 기록; 현재 정책과 구분 |
| [docs/DASHBOARD-MONITORING-AUDIT.ko.md](docs/DASHBOARD-MONITORING-AUDIT.ko.md) | 모니터링 정밀 감사와 반영 상태(P0/P1) |
| [docs/DASHBOARD-MENU-AUDIT.ko.md](docs/DASHBOARD-MENU-AUDIT.ko.md) | 네 메뉴의 구조·완성도·문구 정확성 감사 |
| [docs/CLAUDE-CODE-COMPATIBILITY.ko.md](docs/CLAUDE-CODE-COMPATIBILITY.ko.md) | Claude Code 버전 기준선과 릴리스 체크리스트 |

`openrouterAdvisor.py`라는 이름은 이전 구현에서 유지한 이름입니다. 이 실행기는 별도 MCP 상담 도구가 아니라 라우팅 세션을 관리하며, 지원되는 원본 Sonnet 실행에서 Claude Code의 네이티브 Advisor 옵션을 전달합니다. 개인 진단 스크립트·로그인 파일·실제 API 키·개인 설정은 배포하지 않습니다.

소스와 문서는 [MIT License](LICENSE)를 적용합니다. 저작권·허가 고지를 유지하는 조건으로 사용·수정·배포할 수 있습니다. 추적 소스 검토에서 Node.js·Python 표준 라이브러리와 별도 설치 Claude CLI를 사용하며, 번들 외부 패키지나 별도 소스 라이선스 고지는 발견하지 못했습니다. 선택 범위와 Apache-2.0 비교는 [라이선스 검토](LICENSE-REVIEW.md)에 기록했습니다.

Claude Code·Node.js·Python·외부 서비스와 모델 자체는 배포 대상이 아니며 각각의 라이선스와 이용 조건이 적용됩니다. MIT는 계정 접근 권한·모델 사용권·요금 정책을 변경하지 않습니다.

### 기능 보존 보완 (2026-10-29)

일반 모드는 사용자 Claude Code 환경을 유지합니다. Local/OpenRouter 호환 모드도 명시적으로 설정한 Agent Teams·subagent·작업 목록·백그라운드·IDE·플러그인 탐색·skill 동기화 선호를 보존하도록 보완했습니다. 설정하지 않은 기능을 자동 활성화하지 않으며 제공자 인증·라우팅 격리와 제한형 LocalMode 정책은 유지합니다. 계정 동기화 기능에는 원본의 로그인 조건이 그대로 적용됩니다.

`attach`, `logs`, `respawn`, `stop`/`kill`, `rm`, `purge`, `gateway`, `setup-token`, `upgrade`는 모델 설정이나 연결 검사 없이 원본 Claude 관리 명령으로 전달합니다. 삭제·인증 등의 실제 동작과 확인 절차는 원본 명령의 책임입니다. 이 명령 전달 지원이 `--bg`로 시작한 라우팅 세션의 프록시 수명까지 보장한다는 뜻은 아닙니다.

Plan 경로의 HTTP-date `Retry-After`는 클라이언트가 읽을 수 있는 초 단위로 정규화합니다. 일반적인 기능 거부 오류 본문과 재시도 헤더 보존을 회귀 검사하며, 자격증명 마스킹과 오류 크기 제한은 유지합니다. `external_attempt_failed.validation_detail`은 빈 응답을 `missing_body`, `empty_body`, `empty_content`, `content_without_usable_output`, `stream_without_usable_content`로 구분합니다. 응답 본문·reasoning·도구 인자를 기록하지 않습니다.

**남은 경계:** Remote Control은 사용자 지정 API base URL에서 비활성화되는 Claude Code 자체 제약이 있습니다. `--bg`는 전면 프로세스 종료 이후 프록시가 살아 있어야 하므로 현재 동기 실행 수명과 별도로 설계·검증해야 합니다. 실제 Agent Team·SSH/MCP·plugin 전체 조합을 통과했다고 주장하지 않습니다. [상세 근거와 검증 상태](docs/HARNESS-COMPATIBILITY-REVIEW.ko.md)

핵심 라우팅의 성능·기능 공동 개선 연구는 [호환성 분석 문서](docs/HARNESS-COMPATIBILITY-REVIEW.ko.md)의 마지막 절에 정리했습니다. 운영 관측과 제안을 구분하며, 재시도·전체 시간 예산·스트리밍 변경은 아직 적용하지 않았습니다.


### 라우팅 병목 진단 (P0)

새 프록시는 기존 지표에 `attemptCounts`와 `routingTimingMs`를 추가합니다. 실제 업스트림 호출 수와 논리적 요청 수를 구분하고, Local 자리 대기·외부 호출/응답 검증·재시도 대기·Plan 호출·전체 처리 시간을 각각 확인합니다. 대시보드의 **실행 상태와 진단**에서 볼 수 있으며 AI 점검용 `/api/diagnostics`에도 검증된 숫자만 제공합니다. 이전 프록시에서 보고하지 않은 값은 0으로 추정하지 않습니다.

각 시간은 최근 최대 512개 구간의 표본입니다. 외부 호출에는 응답 읽기·검증이 포함되며 순수 모델 추론 시간이 아닙니다. 전체 시간은 요청 본문을 읽고 검증한 이후부터 종료까지이고, 실패·취소도 포함합니다. 구간별 p95를 더해 전체 p95로 해석하지 마세요. 기존 `externalAttempts`는 외부 처리에 최초 진입한 요청 수이며 실제 총 시도 수와 다릅니다.

빈 응답 진단은 허용된 종료 사유·응답 형식·블록 종류와 제한된 사용량 수치만 기록하며 응답 본문·프롬프트·도구 인자는 저장하지 않습니다. 추가 모델 호출이나 폴링 없이 기존 요청에서 계측합니다. Local 최대 60초 대기, 재시도 횟수, 회로 차단, Plan 우회 정책은 유지합니다. 새 프록시 지표는 실행기 재시작 후 적용됩니다.

### 메뉴 감사와 OpenRouter 상류 공급자 (2026-11-01)

OpenRouter 설정에 `provider`(상류 공급자 slug) 항목이 추가되었습니다. 지정하면 OpenRouter에서 `only`로 제한하고 다른 OpenRouter 공급자로의 fallback을 허용하지 않으며, 비우면 Auto입니다. 새 세션 또는 `-Takeover`부터 적용되고, 모델 목록 확인만으로 특정 공급자의 사용 가능성을 증명하지는 않습니다. Grok 백엔드(2026-11-07)는 1.0.77 침투 테스트 후 활성화됐습니다: `-p <프롬프트>` 단일 턴, 웹 검색·서브에이전트 차단, `--disallowed-tools`로 파일시스템·터미널 빌트인 도구 15종 제거(쓰기·터미널 시도가 "cannot"으로 거부됨을 실측), JSON 구조화 출력. Grok은 `--safe-mode`·stdin 프롬프트가 없어 프롬프트가 argv에 실립니다(짧은 백엔드 프롬프트만), 그리고 `--always-approve`·`--permission-mode bypassPermissions` 같은 우회 플래그는 어댑터가 절대 붙이지 않습니다.

네 화면(대시보드·설정·사용량·블랙박스)의 구조와 문구를 실제 API 응답과 대조해 점검했습니다([메뉴 감사](docs/DASHBOARD-MENU-AUDIT.ko.md)). 프록시 이벤트가 없는 구간은 0건이 아니라 "최근 60분 이벤트 없음"으로 표시하고, 헤더·범례·지표 이름의 "외부 서비스"를 "외부 모델"로 통일했으며, 도움말 HTTP API 목록을 UI가 실제 호출하는 13개 엔드포인트로 보완했습니다. 설정 화면은 저장 429(`rate_limited`)와 CLI 백엔드 응답 코드를 한국어로 안내하고, 블랙박스는 기록 시작 시각과 폐기 건수를 표시합니다. 모니터링 지표의 계산 기준과 반영 상태는 [모니터링 감사](docs/DASHBOARD-MONITORING-AUDIT.ko.md)에 정리했습니다.

검증은 Node 700개, Python 661개 회귀 검사와 실행 중 대시보드의 페이지·API 응답(토큰 필요 여부 포함)으로 확인했습니다. 실제 외부 모델 품질과 Claude Code 전체 기능 동등성은 여전히 별도 검증 대상입니다.