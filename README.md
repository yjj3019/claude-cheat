# Claude Code · Sonnet 대체 모델과 Pro fallback

**한국어** | [English](README.en.md)

[MIT License](LICENSE) · [라이선스 선정 근거 / License review](LICENSE-REVIEW.md)

기존 Claude Code Pro 로그인과 설정을 유지하며, 호환되는 Sonnet 요청을 OpenRouter DeepSeek 또는 Local LLM으로 보내는 로컬 라우터입니다. 실행 코드·합성 회귀 테스트를 포함하며 API 키·OAuth·개인 설정·개인 진단 기록은 포함하지 않습니다.

**최신 상태: 재시도·Pro fallback·복구·Local 선택 구현. 모든 기존 기능과 실제 Sonnet 대체의 동시 보장은 미완료.**

- 기록 기준: 2026-10-20 16:14 KST

## 현재 동작과 실행

이 checkout에서 직접 실행하려면 Node.js·Python·Claude Code와 기존 Pro 로그인이 필요합니다. OpenRouter 키는 checkout 루트의 `.env.openrouter`에 `ANTHROPIC_AUTH_TOKEN=본인의_OpenRouter_키` 형식으로 설정합니다. 이 키는 라우터만 사용하며 Claude의 구독 로그인을 API 키로 바꾸지 않습니다. `.env*` 파일은 Git에서 제외합니다.

```powershell
# checkout 루트에서 실행
.\startClaudeOpenRouter.ps1
# 외부 API 호출 없는 합성 테스트
node --test tests/claudeRouteProxy.test.mjs
python openrouterAdvisor.py --self-test
```

아래의 경로 없는 실행 명령은 기존 PC에 설치된 전역 shim을 사용합니다. clone만으로 전역 shim을 설치하거나 현재 실행 환경을 전환하지 않습니다.

```powershell
# 기본: OpenRouter DeepSeek
startClaudeOpenRouter.ps1
# Local Messages API: 아래 로컬 설정 후 실행
startClaudeOpenRouter.ps1 -Provider Local
```

- 선택한 제공자에 최초 요청과 재시도3회, 총4회 시도합니다. 각 시도 제한30초, 재시도 간격1초입니다. 모두 실패하면 원본 Sonnet 모델의 Pro OAuth 경로로 한 번 보냅니다. Pro 실패 시 다른 API로 재전환하지 않습니다.
- fallback 뒤30초 동안 Pro를 사용합니다. 그 뒤 호환되는 실제 요청 하나만 복구 확인에 사용하고, 성공 시 외부 제공자로 복귀합니다. 별도 백그라운드 유료 진단은 없습니다. 동시에 들어온 다른 요청은 Pro를 사용합니다.
- 외부 응답은 정상 완료·모델·도구 입력 JSON을 검증할 때까지 보관합니다. 실패한 응답의 텍스트·도구 호출은 CLI에 전달하지 않습니다. 표시 지연과 재시도의 중복 추론 비용이 생길 수 있습니다.
- Pro 요청·응답과 오류는 원형 전달합니다. 기존 advisor·실험 beta를 강제로 비활성화하지 않습니다. 외부가 처리하지 못하는 advisor·서버 도구·inline 도구·서명된 thinking 등은 원본 Pro 요청으로 우회합니다. 이 우회는 장애 fallback이나 외부 복구 성공으로 세지 않습니다.
- Opus·Haiku·Fable은 기존 Pro 인증 경로입니다. 모델 접근성과 크레딧은 앱·계정 설정이 결정하며 별도 Fable 차단·OFF 요구는 없습니다. 외부 제공자에는 Pro OAuth를 전달하지 않습니다.

Local 설정은 **실행기 파일이 있는 폴더**의 `.env.local-llm` 또는 해당 프로세스 환경변수에서 읽습니다. 작업 디렉터리와 다른 폴더일 수 있습니다. 서버 주소와 모델 ID는 나중에 실제 서버에 맞게 지정합니다. 현재는 Anthropic 호환 `/v1/messages`만 지원하며 OpenAI 전용 `/v1/chat/completions` 변환은 구현하지 않았습니다.

```powershell
# checkout 루트에서 기존 설정을 덮어쓰지 않고 예제 복사
if (-not (Test-Path -LiteralPath '.env.local-llm')) {
    Copy-Item -LiteralPath '.env.local-llm.example' -Destination '.env.local-llm'
}
notepad .env.local-llm
# 추론·Pro 로그인 없이 서버 모델 목록과 선택 모델만 점검
.\startClaudeOpenRouter.ps1 -CheckLocal
# 실제 사용 시 기존 Pro 인증 검사 후 시작
.\startClaudeOpenRouter.ps1 -Provider Local
```

```dotenv
LOCAL_LLM_URL=http://127.0.0.1:11434/v1/messages
LOCAL_LLM_MODEL=YOUR_INSTALLED_MODEL_ID
LOCAL_LLM_ALLOW_REMOTE=false
# 인증이 필요한 로컬 서버에만 LOCAL_LLM_API_KEY를 별도로 설정
# 선택: 같은 scheme/host/port의 모델 목록 URL (기본 /v1/models)
# LOCAL_LLM_MODELS_URL=http://127.0.0.1:11434/api/tags
```

기본은 literal loopback(`127.0.0.1`, `::1`) HTTP/HTTPS만 허용합니다. 기존 원격 서버를 쓰려면 `LOCAL_LLM_ALLOW_REMOTE=true`와 `https://서버주소/v1/messages`를 명시해야 합니다. 원격 HTTP·URL 내 자격증명·쿼리·fragment·redirect·잘못된 인증 헤더는 거부하며 TLS 인증서 검증을 끄지 않습니다. 평문 LAN 서버는 HTTPS를 제공하거나 사용자가 별도로 준비한 loopback 터널을 사용해야 합니다. 원격 허용은 지정한 DNS 이름과 그 해석 결과를 신뢰한다는 뜻입니다.

`-CheckLocal`은 같은 origin의 모델 목록에 GET만 보냅니다. 환경 프록시·redirect를 사용하지 않고 `Authorization: Bearer` 헤더에 Local 전용 키만 전달하며, 응답은 1MiB·소켓 타임아웃 5초로 제한합니다. 결과에는 모델 수·선택 모델 존재 여부·고정 오류 분류만 출력합니다. 모델 목록 성공은 추론·도구 호출 성공을 뜻하지 않습니다. 목록 API가 없는 서버에서는 해당 점검을 생략하고 별도로 추론을 검증해야 합니다. 점검 실패가 Pro fallback을 실행하지는 않습니다.

실제 Local LLM은 현재 시스템에 없으며, 사용자가 실서버 테스트를 추후 진행하기로 했습니다. 설치·서비스 기동·실서버 접속·실제 추론은 하지 않았습니다. Local 선택 시 `.env.openrouter` 키를 읽지 않습니다. 실설정 `.env*`는 Git에서 제외하고 값이 없는 `.env.local-llm.example`만 배포합니다.

Local HTTP 통합 검사는 `node --test tests/*.test.mjs`, 설정·점검 검사는 `python -m unittest discover -s tests -p test_local_config.py -v`로 실행합니다. 실제 루프백 HTTP 서버와 합성 응답을 사용하며 Pro 응답도 합성입니다. 기존 계정이나 외부 LLM에는 요청하지 않습니다.

이번 Local 보완 후 독립 검사에서 Node **136/136**, Python **13/13**, Python 자체 검사·Node/Python 문법·PowerShell 파싱이 통과했습니다. 실제 서버·모델·인증·TLS 배포에 대한 검증은 추후 진행합니다.

## 라이선스

이 저장소의 코드와 문서는 [MIT License](LICENSE)를 적용합니다. 재배포 시 저작권·허가 고지를 보존해야 하며, 상업적 사용·수정·배포를 허용합니다. [MIT 공식 원문](https://opensource.org/license/mit).

전체 추적 소스를 검토한 범위에서 Node.js·Python 표준 라이브러리와 별도 설치된 Claude CLI만 사용하며, 배포에 포함된 외부 패키지·별도 라이선스 고지는 발견하지 못했습니다. 작은 독립 라우터의 재사용·수정에 적합한 단순 허용형 라이선스로 MIT를 선정했습니다. 검토 범위와 Apache-2.0 비교는 [선정 근거](LICENSE-REVIEW.md)에 기록했습니다.

Claude Code, Node.js, Python, OpenRouter·Anthropic 서비스와 모델 자체는 이 저장소에 포함되지 않으며 각자의 라이선스·이용 조건이 적용됩니다. 이 저장소의 MIT 라이선스는 서비스 접근 권한이나 사용량·요금 정책을 변경하지 않습니다.
