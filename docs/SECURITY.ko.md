# 보안 모델과 잔여 위험

이 문서는 `claude-cheat`의 실행기·프록시·대시보드가 **무엇을 신뢰하고 무엇을 방어하는지** 정리합니다. 코드가 바뀌면 이 문서도 함께 갱신하세요.

## 1. 신뢰 경계

| 구성 요소 | 위치 | 신뢰 |
| --- | --- | --- |
| 라우팅 프록시 | `127.0.0.1:<임시 포트>` | 로컬 사용자만 접근. `X-Local-Route-Token`(세션마다 생성) 없으면 403 |
| 대시보드 | `127.0.0.1:8787` (고정) | 로컬 사용자만 접근. 읽기는 공개, 쓰기는 세션 토큰 필요 |
| 설정 파일 | 실행기 폴더의 `.env.openrouter` / `.env.local-llm` | 사용자 소유 파일. Git 제외(`.env.*`) |
| 로그·메트릭 | `.route.log`, `.proxy-metrics.json` | 진단용. 키·프롬프트·본문은 기록하지 않음 |

프록시와 대시보드는 **모두 루프백에만 바인딩**합니다. 외부 인터페이스로 열리는 경로는 없습니다.

## 2. 인증·인가

| 대상 | 통제 |
| --- | --- |
| 프록시 `/v1/messages` | `X-Local-Route-Token`(32바이트 랜덤, 상수시간 비교) + OAuth Bearer(`sk-ant-oat…`) 필요. Plan 경로는 추가로 first-party 로그인 확인 |
| 프록시 `/api/metrics` | 같은 세션 토큰 필요 |
| 프록시 `POST /api/handover` | 같은 세션 토큰 필요. 새 연결을 멈추고 진행 중인 턴만 마친 뒤 종료합니다(포트 재사용 목적) |
| 대시보드 쓰기(`POST /api/config`, `/api/test`) | **세션 토큰**(페이지에 내장, `x-dashboard-token`) **+ 동일 출처(Origin)** 검사 + **분당 30회 제한**(초과 시 429) |
| 대시보드 읽기(`/`, `/api/status`, `/api/snapshot`) | 인증 없음. 루프백 한정 + **Host 허용목록** |
| 비밀값 | 응답에 절대 포함하지 않음(`sk-or-…ccd0` 마스킹). 저장 시 빈 값이면 기존 키 유지 |

### CSRF를 별도 토큰 없이 막는 이유

- 세션 쿠키를 **쓰지 않습니다**. 인증은 페이지에 심긴 값 + 커스텀 헤더(`x-dashboard-token`)입니다.
- 커스텀 헤더가 필요한 요청은 브라우저가 **프리플라이트**를 보내므로, 다른 출처에서는 헤더를 붙일 수 없습니다.
- 추가로 `Origin`이 `127.0.0.1`/`localhost`/`[::1]` 중 하나일 때만 쓰기를 허용합니다.
- 따라서 별도 CSRF 토큰은 중복 방어이며, 추가하지 않았습니다.

### DNS 리바인딩

- 브라우저가 `evil.example`을 127.0.0.1로 해석해도, 요청의 `Host`가 `127.0.0.1`/`localhost`/`[::1]`이 아니면 **403 `host_rejected`**.
- 읽기 응답에는 CORS 헤더를 보내지 않으므로 다른 출처의 JS가 본문을 읽을 수 없습니다.

## 3. 헤더·전송

- 모든 응답: `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`.
- TLS는 사용하지 않습니다(루프백 전용). 외부 구간(OpenRouter/Anthropic)은 프록시가 HTTPS로 통신합니다.
- 인증서·프록시 설정을 신뢰하지 않기 위해 `ProxyHandler({})`로 시스템 프록시를 우회합니다.

## 4. 입력 검증

| 입력 | 검증 |
| --- | --- |
| 모델 ID | `[A-Za-z0-9~@][A-Za-z0-9._:/@+~-]{0,255}` |
| OpenRouter 키 | `sk-or-[A-Za-z0-9_-]+` |
| Local URL | `http(s)`, `/v1/messages` 경로, 루프백(또는 명시 허용한 HTTPS/사내 HTTP 예외) |
| 모델 목록 URL | 같은 출처 또는 명시 설정, `http(s)` |
| 요청 본문 | `Content-Length` 0~64KB, 음수 거부, JSON 객체만 |
| 프록시 본문 | 최대 32MiB, 엄격한 필드 화이트리스트(알 수 없는 필드는 Plan 우회) |

`LOCAL_LLM_*` URL은 사용자가 직접 설정하는 값이며, 형식 검증만 수행합니다. **신뢰할 수 없는 출처의 URL을 넣지 마세요**(SSRF 표면).

## 5. 비밀값 취급

- 키는 `.env.*`(Git 제외)에만 존재하고, 자식 프로세스 env로 전달된 뒤 **프록시 시작 시 env에서 삭제**됩니다.
- 로그에는 키·프롬프트·요청 본문을 남기지 않습니다(`route_selected`, 실패 코드, 바이트 수만).
- 설정 저장은 **원자적 교체**(임시 파일 → rename) + 이전 내용 `.bak`. 읽기 실패 시 파일을 건드리지 않습니다.
- `installLauncher.ps1`의 포워더는 체크아웃 경로만 담고, 자기 자신을 덮어쓰거나 지우지 않습니다(마커 검사).

## 6. 하지 않은 것과 그 이유

| 항목 | 결정 |
| --- | --- |
| 요청 속도 제한(읽기) | 쓰기 API만 분당 30회로 제한합니다. 읽기는 대시보드 자체 갱신(2~10초)이 대부분이라 두지 않았습니다 |
| 별도 CSRF 토큰 | 2절의 이유로 불필요 |
| TLS(로컬) | 루프백 전용. 인증서 관리 비용 대비 이득 없음 |
| 의존성 취약점 스캔 | 런타임 의존성 **0개**(Node 표준 라이브러리, Python 표준 라이브러리) |
| 명령 실행 샌드박스 | 이 도구는 라우팅만 담당하며, 도구 실행 권한은 Claude Code 세션 설정을 따릅니다 |

## 7. 잔여 위험

1. **로컬 사용자 신뢰**: 같은 PC의 다른 사용자·프로세스는 루프백 포트에 접근할 수 있습니다. 토큰이 없으면 읽기(대시보드)는 가능하니, 공용 PC에서는 대시보드를 띄우지 마세요.
2. **세션 토큰 노출**: 토큰은 페이지 HTML에 포함됩니다. 브라우저 확장·화면 공유로 노출될 수 있습니다.
3. **SSRF**: `LOCAL_LLM_*` URL은 사용자가 넣은 값입니다. 신뢰할 수 없는 값을 넣지 마세요.
4. **감사 범위**: 정적 검토 + 회귀 테스트이며, 퍼징·침투 테스트는 수행하지 않았습니다.

## 8. 확인 방법

```powershell
# 외부 Host 거부
Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/status' -Headers @{ Host = 'evil.example' }

# 토큰 없는 쓰기 거부
Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/config' -Method Post -Body '{}' -ContentType 'application/json'

# 프록시 토큰 없는 접근 거부
Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/models?provider=openrouter'
```

자동 검증: `python -m unittest tests.test_route_dashboard`(Host·헤더·토큰), `node tests/claudeRouteProxy.test.mjs`(프록시 인증·격리).
