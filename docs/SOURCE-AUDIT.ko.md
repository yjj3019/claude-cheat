# 전체 소스 점검 보고서 (2026-11-15)

- 커밋 기준: `9d1b2f0` (2026-11-12), 작업 트리 clean
- 감사 방식: 6개 영역 병렬 감사(프록시 / 대시보드 / 런처·CLI / 지원 레이어 / 스크립트·패키징 / 테스트·문서) + 전체 테스트 스위트 + 정적 문법 검사
- 기준선 확인: **Python 854 tests OK (skipped=10)**, **Node 811 pass (0 fail)**, `node --check`/`py_compile` 전부 통과
- 수정 후 (2026-11-15): **Python 874 tests OK (skipped=10)**, **Node 811 pass (0 fail)** — [§6 마감](#6-마감-2026-11-15) 참고
- 심각도 범례: **[CRITICAL]** 데이터 손실/보안/확정적 장애 / **[MAJOR]** 실질적 영향이 있는 명백한 결함 / **[MINOR]** 엣지·견고성·이식성 / **[INFO]** 문서·일관성
- 요약: **CRITICAL 0건, MAJOR 1건, MINOR 12건, INFO 10건** (그중 2건은 리뷰어 2명이 독립적으로 동일 지점을 지목해 교차 확인됨)
- **[UNVERIFIED]** 표기는 정적 분석으로 확정할 수 없어 실기 검증이 필요한 항목

---

## 1. 주요 발견 (MAJOR)

### M1. 런처의 Local admission env 상한 미검사 → "잘못된 값은 기본값 적용" 보장이 거짓 (2명 교차 확인)

- 런처: `openrouterAdvisor.py:962-970` — `ROUTE_LOCAL_CONCURRENCY` / `ROUTE_LOCAL_QUEUE_LIMIT` / `ROUTE_LOCAL_QUEUE_WAIT_MS` / `ROUTE_LOCAL_ATTEMPT_TIMEOUT_MS`를 `value.isdigit() and int(value) >= minimum` 만 검사하고 전달. **상한 검사 없음** (959-961 주석: "typo cannot fail the proxy at startup").
- 프록시: `claudeRouteProxy.mjs:1282-1285` — `localConcurrency > 64`, `localQueueLimit > 256`, `localQueueWaitMs > 600000` 이면 **throw `'Invalid Local admission limits'`** (다른 env는 1272-1281에서 clamp).
- 결과: `ROUTE_LOCAL_CONCURRENCY=65`, `ROUTE_LOCAL_QUEUE_LIMIT=257`, `ROUTE_LOCAL_QUEUE_WAIT_MS=600001`을 설정하면 런처가 값을 그대로 전달 → 프록시가 시작 직후 죽음 → `ready.get(timeout=15)` 공백 → 세션 "Local routing proxy did not start"로 세션 전체 실패. README.md:546의 "잘못된 값은 기본값이 적용됩니다"와 `docs/HISTORY.ko.md:59`, `docs/AGENT-TEAMS-THINKING-RESEARCH.ko.md:56`의 "런처가 올바른 값만 전달" 문서화와 모순. 실제 경로의 동작이 문서화된 안전 보장과 다름.
- 영향: Agent Teams/느린 로컬 서버 튜닝 기능(이번 릴리스의 핵심 기능)에서 범위 초과 숫자는 흔한 입력 — 예방하겠다는 주석과 반대로 가장 흔한 실패 경로를 만든다.
- 수정 제안: 파이선 게이트에 프록시와 동일한 상한(64/256/600000) 미러링 또는 Python 쪽 clamp + `local_admission_env_ignored` 로그. 런처 경유 시나리오 E2E 테스트 추가 (`tests/localAdmission.test.mjs`는 `createProxy` 직접 호출에 대해서만 throw를 검증 — 런처 전달 경로는 테스트 부재).

---

## 2. MINOR 발견

### Python / JS / 스크립트

1. **유니코드 숫자가 `isdigit()` 게이트를 양방향으로 뚫음** — `openrouterAdvisor.py:967`. `"²"`(위첨자)는 `isdigit()==True`지만 `int("²")`가 `ValueError` → 게이트 루프 안에 try가 없어 세션이 통째로 중단. `"６５"`(전각)는 `int()`가 통과해 원문 그대로 전달 → Node `Number("６５")`는 NaN → `claudeRouteProxy.mjs:1282` throw → 프록시 기동 실패(=M1의 크래시). 수정: `value.isascii() and value.isdigit()` + 상한 미러링.
2. **`_SHIM_RE`가 레거시 cmd-shim 미매칭** — `routeCli.py:22`는 현행 npm cmd-shim v6 형태(`"%dp0%\..."`)만 인식. npm≤8 시대/레거시 배치(`"%~dp0\..."`)는 `unsupported_shim`으로 오분류, 대시보드 연결 테스트가 오해 소지의 죽은 길. 수정: `'"%(?:~)?dp0%\\..."'`.
3. **버전 허용목록이 prerelease/접미사 수용** — `routeCli.py:21` `\d+\.\d+\.\d+` + `search()`(비앵커). `0.167.0-dev`/`0.167.0+local`이 검증된 `0.167.0`으로 통과. fail-closed 방향(미지 버전 → `backend_version_unverified`)은 정상. 수정: 전체 문자열 정확 일치(fullmatch).
4. **`connection_test`가 버전 검사 없이 "ready" 보고** — `routeCli.py:346-385`: `OK` 프로브만으로 ready. 차기 codex 0.168.0에서 대시보드는 "ready"인데 모든 세션은 `backend_version_unverified`로 중단 → 진단이 사용자를 정확히 오도. 수정: 비허용 버전이면 `"unsupported_version"` 반환.
5. **job attach 전 자식 즉사 시 실패 진단 가림** — `cliBackend.py:352-354`: `Popen`→`create_job_for` 사이에 자식이 죽으면 `process_tracking_unavailable`가 먼저 던져져 스테이지 진단(로그인 실패 등)이 영영 실행 안 됨. 수정: `poll() is not None`이면 tracking 오류를 건너뛰고 드레인·진단 경로로.
6. **클라이언트 백프레셔 정체가 30초 침묵 윈도우를 소비** — `claudeRouteProxy.mjs:926-930`+`2267-2268`+`2339`: `drainIfNeeded()`는 타이머가 없어 클라이언트가 30초 이상 멈추면 라이브 업스트림이 `stream_broken`으로 중단됨(제한적·자기한정 — TCP keep-alive 15s → close → drain 해소). [UNVERIFIED 일부: Node 런타임 동작 의존]. 수정: drain 재개 시점에 arm 재장전.
7. **`ROUTE_LOCAL_ATTEMPT_TIMEOUT_MS`와 형제 env의 비대칭 처리** — `claudeRouteProxy.mjs:1160-1163`: `=abc` → NaN → 조용한 기본 120000(주석은 "fails loudly"라고 주장하지만 1266 검사는 clamp 이후라 무력). 직접 실행 시 `ROUTE_LOCAL_QUEUE_LIMIT=''` → `Number('')=0` → **빈 값 typo가 조용히 큐 비활성**. 수정: 숫자 전용 정규식 파싱 후 4개 모두 통일 규칙.
8. **`ROUTE_HANDOVER_TIMEOUT_MS=-5` 미클램프** — `claudeRouteProxy.mjs:1180`: 비양수가 `|| 120000`을 우회해 즉시 drain→revert(409). 수정: `Number.isFinite ? clamp : 기본`.
9. **`localProcessTree.py:138-142`** — taskkill 비정상 종료(0이 아닌 exit)에도 `outcome="tree"` 보고, docstring(124-126)의 "failed → 'root'로 폴백"와 모순. 실제 fallback 동작은 유지되므로 결과 라벨만 왜곡.
10. **`routeConfig.py:372`** — `update_text`의 BOM 비감지(모든 내부 호출자는 `utf-8-sig` 경유라 현재 도달 불가; `parse`의 272행과 비대칭). 수정: 매칭 전 `line.lstrip("\ufeff")`.
11. **`routeConfig.py:539-541`** — `write_provider`가 문서 레벨 오류만 검사, 섹션 레벨 오류는 validate 콜백 없이는 저장 후 읽을 수 없는 상태로 영속(현재 호출자는 전부 validate 전달 — 도달 불가). 수정: `doc.first_error(provider)` 사용.
12. **`modelQualityProbe.py:75-76` + `routeDashboard.py:3117`** — 동일 TimeoutError가 독립 실행에선 `configuration_or_request_failed`, 대시보드에선 `probe_timeout`으로 다른 라벨.
13. **세션 간 handoff 등록 스톰프** — `openrouterAdvisor.py:508-535`: 두 번째 세션이 첫 번째의 `.session-handoff.json`을 무조건 덮어씀(삭제 쪽은 proxy_pid 일치 검사 있어 안전). `--takeover`가 최신 작성자를 노림.
14. **`installLauncher.ps1:111,134-137`** — 포워더 소유 판정이 **체크아웃 경로를 포함한 바이트 비교** → 저장소를 옮기면 자기 포워더를 "본 설치자가 만든 파일 아님"으로 오분류, 갱신·삭제 영영 불가(마커 라인으로 소유 판정하도록 수정 권장).
15. **`installLauncher.ps1:44`** — `TrimEnd('\','/')`가 `-Target C:\`를 `C:`(드라이브 상대경로)로 변형 → 다른 드라이브 작업 디렉터리면 엉뚱한 곳에 설치.
16. **`OpenClaude.ps1:54-57` + `OpenClaudeDashboard.ps1:7`** — 대시보드 실패가 throw로 붕괴되어 `exit $LASTEXITCODE` 경로를 우회, 호출자 exit 1.
17. **`OpenClaudeDashboard.ps1:2`** — 포트 `[ValidateRange(1,65535)]` 부재($Port 0/음수 허용).
18. **대시보드**: `aggregateStatus` 부분 분기(`${MAX_USAGE_INSTANCES}` 리터럴 표시, :4930), `model_pricing`의 `rate()`가 누락 가격을 가짜 $0으로(:1623-1627, "never fake $0" docstring 위반), 스윕/핸드오프 경로의 심링크 추종(:3956/:3986, 손상 범위 제한적), `connection_test` 버전 미검사(위 4번과 동일 계열).
19. **테스트**: `tests/localAdmission.test.mjs:50`의 고정 5.2초 sleep, `tests/metrics.test.mjs:259`의 1.4초 실시간 sleep, `/api/models-search`·`/api/provider-endpoints`의 429 분기 엔드포인트 수준 테스트 부재(카운터 자체는 직접 테스트됨).

---

## 3. INFO / 문서 정합성

- **env 파서 중복 표류**: `routeVersion.py:54-58` / `routeDashboard.py:6294` / `openrouterAdvisor.py:183-185` / JS의 `=== 'true'` 전용 비교가 false-set 어휘가 서로 다름(현재 실버그 없음 — 런처가 정규화). 파서 1개로 통일 권장. `parseRetryAfter`(JS)의 Python 대응물 부재.
- **`routeVersion.py:320`** 고정 `.tmp` 명 + 2개 프로세스 경쟁(자기복구, 손상 불가), **`usage_ledger._save_usage_states`** 고정 `.tmp`(:932-934, `_attention_write`는 유일 이름 사용).
- **`routeDashboard.py:3018`** 로컬 데드클래스 `NoRedirect`(전역 `_NoRedirect` 사용), **`HELP_SECTION` 들여쓰기**(:209).
- **`cliBackend.py:473`** codex `model_reasoning_effort=` 인용 문자 유무 실기 미확인 [UNVERIFIED]; **`check_cli_backend` 실패에도 cli 세션 계속**(:936-939, cli는 Plan 대체 수단이 없어 진행형 장애) — openrouter용 "continue"가 cli에선 오작동 의미.
- **SSE `TextDecoder` EOF flush 부재**(`claudeRouteProxy.mjs:922,976`, 실질 손상 경로는 비현실적).
- **README.md:604** 테스트 수(726/712) 주장은 실행 없이 재현 불가 [UNVERIFIED]; **`docs/PROXY-REFERENCE.ko.md:151`** "10ms 비동기 배치"의 10ms 상수가 코드에서 미발견 [UNVERIFIED].
- `-Remove`가 PATH 항목을 남김, `start*.ps1`이 `-Help`에도 경고, `python` 스텁 대체 부재 — UX/이식성 노트.
- `get-바치`(installLauncher) PS7 미설치 호스트라 미실기 [UNVERIFIED].

---

## 4. 확인된 안전 장치 (OK — 회귀 위험 낮음)

- **라우팅/큐**: `localPermit` released 래치 + `finish`의 `indexOf<0` 가드로 이중 해제 불가, FIFO 해제, 큐 대기는 attempt 타임아웃을 소비하지 않음(입장 후 측정), 중단/실패 전 경로에서 전부 해제.
- **thinking 처리**: 일반 로컬 강제 `thinking:{type:'disabled'}`, 로컬 thinking 응답 → 503, compat은 thinking 기록 복사본에서만 제거(원본 비변형), Plan 서명 기록은 로컬 전송 전 제거, `redactDiagnostic` 경유 비밀값 마스킹.
- **보안**: 상태변경 라우트는 전부 POST 전용 + 토큰(`secrets.compare_digest`) + Host/Origin + 30/min + BYTES_CAP 65536; `/api/test` 저장값 전용·`tool_roundtrip`은 `{provider,mode}` 단독; `/api/models-search` POST 전용·저장 키와 결합 불가·6/min; CSP `default-src 'none'` + nosniff + DENY; subprocess 전부 `shell=False` + `CREATE_NO_WINDOW` + 트리 킬 + mkstemp/`os.replace` 원자 기록.
- **업데이트**: fast-forward 전용 + 마커 배타 생성 + 비정상 종료 마커는 절대 자동 제거 안 함, git 프롬프트 금지, 더티 트리/활성 세션/비upstream 스킵.
- **문서 정합성**: VERSION=1.0.7=README=HISTORY(날짜·커밋 대응 OK), `.env` 예제 변수명·기본값 전부 코드와 일치, 런타임 파일 12종 전부 `.gitignore` 커버(`git check-ignore -v` 확인), `$PSScriptRoot` 전판 사용(Start-Process 부재 → 인용 버그 클래스 원천 부재).

---

## 5. 권장 조치 순위

1. **M1 + §2-1**: 런처 게이트에 상한 미러링(64/256/600000) + `isascii()` + 런처 경유 테스트 1건 추가 → 이번 릴리스 문서화된 보장을 실제와 일치시킴.
2. §2-4/§2-5: CLI 버전·진단 경로 정합(connection_test 버전 검사, 사망 직전 child 처리) — 차기 codex 릴리스 시 현실화되는 오도.
3. §2-2/§2-3: `_SHIM_RE`/`_VERSION_RE` 엄밀화(레거시 shim, 정확 일치).
4. §2-6~8: 프록시 타이머/드레인 재장전 + env 파싱 통일 + handover clamp.
5. §2-14~17: 스크립트 견고성(포워더 소유 판정, 드라이브 루트, exit 코드, 포트 범위).
6. §2-19: 고정 sleep 제거(주입 가능 상수), 429 분기 테스트 추가.
7. [UNVERIFIED] 3건은 실기 검증(PS7, codex reasoning-effort) 후 결정.

---

## 6. 마감 (2026-11-15, 전면 수정 라운드)

| # | 항목 | 상태 | 처리 |
|---|---|---|---|
| M1 | 런처 상한 미검사 → "잘못된 값은 기본값" 거짓 | **수정됨** | 런처 `_local_admission_env`(`openrouterAdvisor.py`): ASCII 숫자 + 상한(1-64 / 0-256 / 0-600000) 미러링. set-but-invalid(유니코드 `²`·`６５`·빈 값·비수치 포함) → `local_admission_env_ignored` 진단 후 무시 → 프록시 기본값. 프록시 `localAdmissionEnv`(`claudeRouteProxy.mjs`): 직접 실행 시 잘못된 값 → `Invalid Local admission limits` 시작 실패. README:546 문구 정정. `tests/test_local_admission_env.py` 신규 8건 + `localAdmission.test.mjs` 전면 개정 |
| 2-1 | 유니코드 `isdigit()` 이중 뚫림 | **수정됨** | `value.isascii() and value.isdigit()` + 상한, 과장 자릿수(4300자리 제한) 견고화 |
| 2-2 | `_SHIM_RE` 레거시 `%~dp0%` 미매칭 | **수정됨** | `'%(?:~)?dp0%\\...'` — npm v6·레거시 둘 다 인식, 고정 테스트 |
| 2-3 | `_VERSION_RE` search 비앵커 | **수정됨** | 꼬리 토큰 `fullmatch` + 앞뒤 고정(`0.167.0-dev`·`0.167.0+build` → `version_failed`), fail-closed 유지 |
| 2-4 | `connection_test` 버전 미검사 "ready" | **수정됨** | 라운드 트립 후 검증 버전 미포함 → `unsupported_version` |
| 2-5 | job attach 전 즉사 → `process_tracking_unavailable` 가림 | **수정됨** | 자식이 이미 종료면 tracking 오류를 건너뛰고 실제 사유 드레인·진단 |
| 2-6 | 백프레셔 정체 → 30초 창 소비 (일부 [UNVERIFIED]) | **부분 수정** | 고정 sleep 5.2s→1.5s로 단축. drain 재장전(arm)은 미수용 — 자기한정·TCP keep-alive 15s 자동 해소 확인 유지 [UNVERIFIED 유지] |
| 2-7 | `ROUTE_LOCAL_ATTEMPT_TIMEOUT_MS` 비대칭 | **수정됨** | 4개 env 전부 동일 규칙(ASCII 숫자 + 범위 1-64/0-256/0-600000/30s-15min), `''`·`²`·`６５`·범위 밖 → throw/무시 테스트 |
| 2-8 | `ROUTE_HANDOVER_TIMEOUT_MS` 미클램프 | **수정됨** | 0·음수·비수치 → 120초, 600초 초과 → 600초. decode에서 소비 후 제거 목록 5종 추가(유출 차단) |
| 2-9 | `localProcessTree` rc≠0에도 `"tree"` | **수정됨** | rc==0일 때만 `"tree"`, 그 외 `"root"` — docstring과 일치 |
| 2-10 | `update_text` BOM 비대칭 | **수정됨** | `line.lstrip("\ufeff")` 대칭 매칭 + BOM 테스트 |
| 2-11 | `write_provider` 섹션 오류 침묵 | **수정됨** | 저장 시 `doc.first_error(provider)`로 표면화(validate 콜백 유지) |
| 2-12 | TimeoutError 라벨 2종 | **수정됨** | 독립 실행 `probe_timeout` 전용 except + 테스트 |
| 2-13 | 세션 간 handoff 스톰프 | **유지** | 설계 유지: takeover는 항상 최신 작성자를 대상, 삭제는 proxy_pid 일치 검사로 안전, 실오작동 없음(시간차만 존재) |
| 2-14 | 포워더 바이트 비교 소유 판정 | **수정됨** | 생성 마커 줄 기반 판정 — checkout 이동 후 갱신·제거 가능, 외래 파일 보호 유지(failure-closed) |
| 2-15 | `-Target C:\` → `C:` 변형 | **수정됨** | 드라이브 루트 분리자 보존 + 테스트 |
| 2-16 | 대시보드 실패 throw 붕괴 → exit 1 | **수정됨** | `-Setup`/대시보드 경로 exit 정규화, 포워더 테스트 갱신 |
| 2-17 | `OpenClaudeDashboard.ps1` 포트 미검증 | **수정됨** | `-Port` 1–65535 / `-Refresh` 1–86400 `[ValidateRange]` |
| 2-18 | 대시보드 4건 + env 삭제 누락 | **수정됨** | `${MAX_USAGE_*}` 템플릿 리터럴 → 실제 보간, `_optional_rate` 통합(전부 None → `model_not_found`), 자유/심링크 스윕 차단(`follow_symlinks=False`·정션 필터), `.tmp` → 유일 임시 이름 + `os.replace`, `_http_json` 데드클래스 제거, HELP 들여쓰기 정렬, auto-reload false-set 정렬, `/api/models-search` 실측 카운터 429 테스트(+`_session_cost` None 가드) |
| 2-19 | 고정 sleep·분기 테스트 부재 | **부분 수정** | 5.2s 고정 sleep 1.5s로 단축. 추가: 429 실측 카운터(`/api/models-search`)·레거시 shim·BOM·드라이브 루트·이동 체크아웃·유니코드 6종. 유지: `metrics.test.mjs` 실시간 sleep 1건·`routeVersion` 고정 `.tmp`(자기복구 설계) |
| INFO | env 파서 중복 표류 | **부분 수정** | auto-reload false-set 정렬 완료, 파서 1개 통일 자체는 미수용(실버그 없음) |
| INFO | cli 세션 계속(continue) | **수정됨** | `check_cli_backend` 실패 → `local_cli_backend_unavailable` 시작 중단(첫 턴 죽음 대신 시작 진단) |
| INFO | README 테스트 수 | **갱신됨** | Node 811 / Python 854 → **Node 811 / Python 874**로 정정 |
| INFO | [UNVERIFIED] 3건 | **유지** | PS7 Get-BatchEncoding은 코드에서 CodePages 공급자 등록으로 해결(실기 확인은 여전히 [UNVERIFIED]), codex reasoning-effort 인용·백프레셔 타이밍은 실기 대기 |

수정 후 전체 스위트: **Python 874 tests OK (skipped=10)**, **Node 811 pass (0 fail)**. VERSION·README·HISTORY·이 문서 기준선 갱신 완료.
