# 대시보드 모니터링 정밀 감사

- 대상: `routeDashboard.py`(4130줄)와 입력원 `claudeRouteProxy.mjs`, `openrouterAdvisor.py`, `usage_ledger.py`
- 기준: 커밋 `249b85b` + 2026-10-31 작업 트리
- 방법: 정적 분석 + 읽기 전용 재현 스크립트(임시 파일/모의 세션, 네트워크 없음) + 실행 중 대시보드의 읽기 전용 API 확인 + `tests/test_route_dashboard.py` 220개 실행 + Node 스위트 확인
- 원칙: 재현하지 못한 것은 "정적 추정"으로 표시한다. 이 감사는 코드를 수정하지 않았다.

## 결론

단일 세션·정상 경로에서 표시되는 수치는 대체로 정확하고 라벨·근거·미보고 처리도 잘 되어 있다. 그러나 **모니터링의 핵심인 이상 상태 감지와 수치의 진실성에서 신뢰가 부족**하다.

1. **표시값이 0으로 단정되는 경우가 있다.** CLI가 표시 제공자일 때(세션 없음 포함) 사용량을 0으로 그리는데, 이는 페이지 스스로 세운 "미보고를 0으로 추정하지 않는다" 규칙과 충돌한다.
2. **"실패율"의 정의가 화면마다 다르다.** 세션 카드의 실패율은 취소를 포함하지만 라벨·힌트·README·추이 카드는 취소 제외라고 말한다.
3. **다중 세션을 구분하지 못한다.** 모든 세션이 같은 `.route.log`에 쓰고 대시보드는 마지막 `proxy_start`·단일 handoff만 보므로 다른 세션의 모델·실패·회로가 현재 세션 것으로 표시된다.
4. **장기 세션·로그 회전 후 "코드가 오래됨" 감지가 사라진다.** 회전된 파일에 마커가 없고 cold read는 4 MiB만 보므로 `proxy_stale=None`이 되고 경고·비정상 종료 코드가 사라진다.
5. **서버 측 갱신이 멈춰도 화면이 알리지 않는다.** 헤더가 검사하는 알림 코드가 등록되어 있지 않아 분기가 도달 불가능하다.

성격상 **능동 모니터링이 아니라 수동 관찰 뷰**다. 물어보면 대체로 정확히 답하지만, 상태가 이상해졌을 때 먼저 알려주지 않는다.

## 데이터 흐름

```text
.claude/projects/*.jsonl ──→ scan_sessions() ──→ 오늘 토큰·비용(추정)
.route.log ───────────────→ parse_route_log() ──→ 5/10/60분 카운트·실패·전환
.session-handoff.json ────→ _live_session() ──→ 프록시 /api/metrics (인증)
.usage-stats/instances/* ─→ usage_ledger ────→ 사용량 페이지(요청 단위)
OpenRouter /api/v1/key, /models ─→ account/pricing (TTL 60s/3600s)
```

## High

### H1. CLI 표시 제공자일 때 사용량 0을 "측정값"처럼 표시

- `snapshot()`은 `usage = _empty_usage() if provider == "cli" else usage_snapshot(...)`로 계산한다(`routeDashboard.py:2484`). `status_report`도 같다(`:4014-4018`).
- `provider`는 `_display_service`(`:1703-1720`)가 정한다: 라이브 세션 → 저장된 `ROUTE_DEFAULT_PROVIDER` → 마지막 라우팅 → 유일하게 설정된 제공자. 따라서 **세션이 없어도** `cli`가 반환될 수 있다(기본 제공자 cli, cli만 설정, 마지막 라우팅이 cli).
- 반면 UI는 `data.active_session?.provider === 'cli'`일 때만 "CLI 보고 여부" 설명 카드를 보여준다(`${JS_DASHBOARD}` tokensCard, `:2709`). 세션이 없으면 일반 카드가 0을 그린다.
- 재현(모의): `_live_session→(None, None)`, routing 기본 cli, `scan_sessions`에 비0 센티널 → `snapshot()`이 `today` 전부 0, `today_cost_usd=None`, `session_count=0`을 반환하고 `scan_sessions`를 호출하지 않았다. 실제 transcript에는 사용량이 있어도 화면은 "외부 0 / Plan 0 / 추정 비용 $0.000", "최근 24시간 기록 없음", "0개 세션"으로 표시된다.
- 영향: CLI 세션이 끝난 직후에도 발생한다(handoff 포트 종료 → 세션 없음 → provider cli). `--status --usage`도 0과 `cost.status="included"`를 내보내 스크립트가 "지출 없음"으로 결론낼 수 있다.
- 테스트: `SnapshotTests`는 openrouter만 사용하고(`tests/test_route_dashboard.py:2497`), DOM 테스트는 `active_session`이 있는 CLI만 다룬다(`tests/dashboard_dom.test.mjs:975`).

### H2. 세션 실패율에 취소가 포함됨 (라벨·힌트·README와 모순)

- 프록시는 취소 시 `metrics.aborted += 1; metrics.failed += 1`(`claudeRouteProxy.mjs:1346`), 본문 idle 타임아웃은 `requests` 증가 전에 `failed`를 올린다(`:1265-1266` vs `:1303`).
- 대시보드 세션 카드는 `failed/(ok+failed)`를 "실패율 · 완료 요청 기준"으로 표시하고, 같은 카드에 "중단 요청"을 따로 나열하며 힌트로 "실패율은 완료된 성공·실패 응답 기준이고 취소는 별도입니다"라고 쓴다(JS `:2709`). 그러나 분자에 취소가 들어 있다.
- 추이 카드는 `history.failed`를 쓰는데 여기서는 취소가 `failed`에 더해지지 않아 "성공·실패 기준 · 취소 제외"가 실제로 맞다(`:1358-1359`). README도 "실패율은 … 취소는 제외"(README:585)라고 명시한다.
- 재현 시나리오: 완료 10건 중 3건 취소 → 세션 카드 실패율 30%, 추이 카드 0%. 본문 타임아웃이면 `requests=0, failed=1`이 되어 "세션 요청 0"과 "(1/1건)"이 모순된다.
- 영향: 사용자가 취소만 해도 "실패 응답 누적" 인사이트와 진단 경고가 뜬다. 건강 신호로 쓸 수 없다.
- 테스트: `tests/stream_ownership.test.mjs:157-159`가 `aborted⇒failed=1`을 의도적으로 고정하고, `tests/monitoringHistory.test.mjs:134-135`는 반대 의미를 고정한다. 대시보드 라벨을 검증하는 테스트는 없다.

### H3. 다중 세션 오귀속 (재현)

- `openrouterAdvisor.py:680-683`은 세션마다 `instance_id`를 만들고 프록시에 넘기지만, 라우팅 로그 이벤트에는 instance id가 없고(`claudeRouteProxy.mjs:1495,1659`) 모든 세션이 같은 `ROUTE_LOG_PATH`를 쓴다(`openrouterAdvisor.py:691`).
- 대시보드는 마지막 `proxy_start`를 세션 경계로 삼고(`routeDashboard.py:361-362`), handoff의 `started` 이후 이벤트를 모두 현재 세션 것으로 센다(`:2411-2427`). handoff는 단일 파일(last-writer-wins, `:1657-1691`).
- 재현: 세션 A(local, 11:00 시작) + B(11:10 시작)의 마커와 B의 선택/실패 이벤트를 넣으면 A의 대시보드가 `provider=openrouter`, `model=B-model`, `counts.external=1`, `failures=[("500",1)]`, `proxy_hash=B의 해시`를 반환했다.
- 영향: 헤드라인 모델 오표시, Plan 비중·회로 경고 오탐, 재시작 판정 오류. 사용량 페이지만 instance 단위로 분리되어 있다(`_usage_live_sessions`, `:2548`).

### H4. 회전·cold read 후 stale 감지 신호 소실 (재현)

- 프록시는 8 MiB에서 `.route.log`를 `.route.log.1`로 rename하고(`claudeRouteProxy.mjs:144-156`), `proxy_start` 마커는 프로세스 시작 때만 쓴다(`:176-179`). 새 파일에는 마커가 없다.
- 대시보드 cold read는 마지막 4 MiB만 읽고(`routeDashboard.py:145,220-221`), `proxy_started_at`/`proxy_code`/`proxy_hash`는 남은 이벤트에서만 얻는다(`:421-438`). 셋 다 없으면 `proxy_stale=None`(`:1415-1433`) → 경고 없음, `status_exit_code` 0(`:4063-4072`).
- 재현: 회전 전 5개 이벤트(마커 포함) → 회전 후 1개 이벤트(마커 없음). `proxy_started_at=None`, external 카운트 0.
- 영향: 세션이 끝난 뒤 "그 세션이 옛 코드였나"를 확인하는 시점에 판정이 사라진다. 실행 중에는 `startedAt`으로 대체되지만 내용 해시 대신 mtime 비교로 약화된다.

### H5. 갱신 정지가 화면에 보이지 않음

- `ALERT_CODES`(`:1486-1491`)에 `snapshot_stale`/`snapshot_refresh_failed`가 없는데 헤더 JS(`:2709`)는 정확히 그 두 코드를 검사한다 → 도달 불가 분기.
- `cached_snapshot()`은 `snapshot_error`를 붙이지만(`:2514`) 이를 읽는 JS가 없고 `building`(`:2524`)도 렌더링되지 않는다. payload에 snapshot 나이도 없다.
- 영향: `_refresh_loop`(`:2680-2693`)가 실패하면 마지막 스냅샷이 계속 그려지고 이전 상태(예: 초록 "외부 서비스 사용 중")가 남는다. `/api/diagnostics`의 `snapshot_stale`만이 유일한 단서다.
- 테스트: `tests/test_route_dashboard.py:27-39`는 예외 문구 비노출만 검증한다.

## Medium

### M1. 라이브 지표가 최신 결정의 사유·모델을 덮어씀

- `_current_routing:2429-2454`는 `metrics.last`가 더 최신이면 로그 기반 last를 교체한다. Plan일 때 `model=None`을 넣고 `detail`은 `metrics.last`에 없는 필드라 사라진다(프록시 `claudeRouteProxy.mjs:1410-1415`). fallback만 `fallbackFailure`로 복원된다.
- 재현: 마지막 요청이 `native_feature/plan_model`이면 `routing.last={reason:"native_feature", detail:None, model:None}`인데 `plan_reasons`에는 `native_feature:plan_model`이 남는다. "현재 Plan 경로" 알림은 사유만 말하고 `REASON_HELP`에 없는 키가 된다. "최근 처리 모델"도 "보고되지 않음"이 된다.
- dual provider(`configuration.alternate`)면 provider/model이 모두 None이 되어 헤더는 "상태 미확인"인데 요약은 "정상 처리 중"이라 서로 모순된다.

### M2. 창 기반 경고·종료 코드가 현재형으로 단정

- `routing_state`는 최신 `proxy_start` 이전 이벤트까지 창에 포함한다(`:376-388`). `build_alerts:1443-1451`은 "외부 시도가 실패해 Plan으로 우회 중입니다"라는 critical을, `status_exit_code`는 exit 3을 낸다.
- 재현: 옛 세션이 t−4분에 회로를 열고 t−1분에 새 `proxy_start`, 라이브 세션 없음 → `state="unknown"`인데 critical 경고 + exit 3.
- 영향: 재시작·세션 종료 후 최대 10분간 거짓 critical.

### M3. `totals`/`cost_usd`가 문서상 7일 창과 다름

- 후보 파일은 mtime ≥ 7일로 거르지만(`:1023`), 합계·비용은 파일 전체 상태를 합산한다(`:1115-1137`). 주석은 "retained window (7 days)"(`:1184`), README도 7일 창이라고 안내한다.
- 재현: 11일 전 턴 1개 + 오늘 턴 1개가 든 파일(mtime 오늘) → `totals.external=[1,000,010,2,0,0]`, `cost_usd≈$1.0`. `/resume`으로 몇 주씩 이어 쓴 세션 파일에서 실제로 발생한다.

### M4. 시계 오차를 반대로 해석 (재현)

- `_age_seconds`는 미래 시각을 `0`으로 자른다(`:265-272`) → 죽은 프록시가 "0초 전", state external, `--status` 0.
- 반대로 `_in_session`은 미래/역행을 배제한다(`:332-335`) → 시계가 뒤로 가면 살아 있는 세션이 "기록 없음"(exit 4)이 된다.
- VM 스냅샷 복원, NTP 보정, 수동 시계 변경 후 오판 가능.

### M5. 60분 창이 조용히 잘림 (재현)

- `routing_state`는 2000개(`:64,357-358`), 파서는 5000개(`:188`), cold read는 4 MiB만 본다.
- 재현: 60분 안에 `route_selected` 3000 + `request_shape` 3000 → counts external 1000(실제 3000). 잘렸다는 표시가 없다. 요청당 2개 이상 이벤트이므로 분당 약 16요청 이상에서 상한에 닿을 수 있고, 회전 시에는 창이 0부터 다시 시작한다.

### M6. fallback 1건이 "Plan 우회 급증" 경고 (재현)

- `:1446-1451`은 결정 비율로 경고한다. 실패 요청 1건은 external 1 + plan 1 = 2결정이라 항상 50%다.
- 재현: 실패 1건 → `warn: Plan 우회 급증`, 요약 "최근 10분 Plan 50%". 문구의 "결정 2건 중 1건"은 정확하지만 제목·요약은 트래픽 비율로 읽힌다. fallback 사유 detail은 label에서 빠진다(`:396`, 의도된 동작).

### M7. 실패 카운트가 "요청"이 아니라 "시도"

- `external_attempt_failed`는 재시도마다 기록된다(`claudeRouteProxy.mjs:1847`). 실패 1건이 4회 재시도되면 "500: 4건"으로 표시되고 카드 라벨은 "외부 실패"뿐이다(`:401-406`). 알림/인사이트는 요청 단위 `metrics.failed`를 쓰므로 두 표면이 일치하지 않는다.

### M8. 단가 조회 실패가 1시간 고정 (재현)

- `model_pricing`은 실패도 `PRICING_TTL=3600`으로 캐시한다(`:99,1348,1395`). 계정 조회는 60초다.
- 재현: t=1000에서 DNS 실패 → t=1600에도 네트워크 호출 0회, 여전히 `connection_failed`. 1시간 동안 "단가 조회 실패" 알림과 unknown 비용이 유지된다.

### M9. 세션 신원이 nonce 하나뿐

- `_live_session`은 handoff의 `port`/`nonce`로 200 응답만 확인하고(`:1667-1691`) `proxy_pid`/`launcher_pid`(`openrouterAdvisor.py:401`)는 검증하지 않는다. 나이·TTL도 없다. 단일 handoff라 다중 세션이면 마지막 writer 기준으로 "현재 세션" 표시·계정/단가/사용량 귀속이 결정된다.

### M10. 사용량이 두 파이프라인에서 따로 계산됨

- 메인 페이지 토큰/비용은 transcript 스캔, 사용량 페이지는 프록시 ledger에서 나온다. 두 수치의 일치 검증·상호 표시가 없다. 직접 Plan 세션·미보고·실패 응답의 포함 규칙이 달라 불일치가 생길 수 있다.

### M11. 비용 추정이 "현재 설정 모델" 단가 하나

- 모든 세션 토큰을 현재 `pricing` 한 벌로 계산한다(`:1026-1030,1118,1173`). 모델을 바꿨거나 dual provider 대체 모델을 썼다면 과거·대체 트래픽도 현재 단가로 추정된다.
- 단, 이 한계는 README:603에 이미 문서화되어 있다. 문제는 화면이 "추정"만 표시하고 모델 혼합 여부를 알리지 않는 점이다.

## Low / 잠재

- L1. 이벤트 유형 미집계: `parent_disconnected`, `client_body_timeout`, `local_admission_rejected`, `local_mode_rejected`, `route_failed`는 "최근 이벤트 15건"에만 나오고 카운트·알림이 없다.
- L2. `parse_route_log(limit=)`가 공유 tail 버퍼를 영구히 줄인다(`:250`). 현재 호출부는 모두 기본값이라 잠재.
- L3. `stat` 실패 시 tail 상태를 초기화한다(`:197-201`) → 다음 갱신이 cold read가 되고 H4와 겹친다.
- L4. `proxy_stale` 계산이 갱신마다 프록시 소스(127KB)를 여러 번 해시한다(`:1464,1530,2501,4023,4046`).
- L5. 페이지에 2초 갱신을 광고하지만(`:1762`) 스냅샷 재생성은 `--refresh`(기본 3초)다(`:3744`). payload에 나이가 없어 구분 불가.
- L6. `.dashboard.pid`가 단일이라 다른 포트의 두 번째 대시보드는 `--stop`으로 도달할 수 없다(`:3758`).
- L7. `--host ::1`은 검증을 통과하지만 서버는 AF_INET이라 바인딩 실패 메시지가 오해를 부른다(`:4122`).
- L8. 폴백 사유 집계 누락: 카드에는 `fallback`만 남고 `rate_limit`/`transient` 등은 "지금 상태"·진단에만 있다(`:396` vs `:2446-2448`, `:3990`).
- L9. `validation_detail`·failure class가 로그에만 있고 화면은 `200:empty_response`로 뭉갠다(`claudeRouteProxy.mjs:1849-1854` vs `:401-406`).
- L10. 최신 전환이 항상 "진행 중": `route_transitions`는 following이 없으면 ongoing=True이고 idle 확인이 없다(`:304-307`). 2시간 전 결정에도 "진행 중" 필.
- L11. `cost.cache_write_1h_rate` 필드에 비율이 아니라 `"explicit"`/`"fallback"` 문자열이 들어간다(`:1194`, 테스트 `:495/:503`).
- L12. `_classify`가 `msg_01` 접두사만으로 Plan 판정(`:449-451`). `msg_01`을 되돌려주는 Anthropic 호환 로컬 서버는 Plan으로 오분류되어 비용이 $0 포함으로 계산된다.
- L13. 캐시 인사이트 비율: "오늘 입력 X 중 캐시 재사용 Y%"는 서로 배타적인 축(cache_read/input)을 나눈 값이고 게이트도 200k 신선 입력 기준이다(`:1567-1597`).
- L14. 요청 거부 알림은 400/413/422만 보지만 프록시는 404도 요청 수준으로 처리한다(`:1452-1457` vs `claudeRouteProxy.mjs:1859`).
- L15. 테스트 오염: 실제 `.route.log`에 23:08경 `launcher start ... proxy=<Mock name='mock.pid' ...>` 6줄, `.usage-stats/instances/` 아래 빈 디렉터리 45개가 남아 있다. 파서는 무시하지만 테스트 격리가 완전하지 않다.

## 정상 동작으로 확인된 것

- 로그 tail: (dev, ino) + 선두 256바이트 digest + offset 검사로 rotate/truncate/부분 줄 처리.
- transcript 증분 스캔: (device, inode)+offset 커서와 head/tail 지문으로 재작성 감지, 턴 exactly-once 클레임/중복 제거.
- 60/10분 경계 산술, idle 300초, `circuit_open` 1회/개방, 시도·재시도 라벨, 지연 p50/p95 512건 상한, 진단 CLI 필드 allowlist.
- 대시보드 pid 신원(pid+생성 시각) 검증과 시그널 거부.
- 지난 세션 지표의 "지난 실행 기록 · N초 전" 라벨, stale 시 지연 인사이트 미계산, CLI 미보고를 0으로 추정하지 않는 카드(`active_session`이 CLI일 때).
- 비밀값 마스킹, 스냅샷 예외 문구 비노출, 알림/인사이트 코드 중복 제거, 비용 상태(estimated/included/unknown) 구분.
- 무인증 API(`/api/snapshot`, `/api/status`, `/api/diagnostics`)는 loopback+Host 검사 뒤에서 모델 ID·비용·경로를 노출하지만 비밀값은 없다. CORS 헤더가 없어 브라우저 교차 출처 읽기는 불가.
- 재실행: `tests.test_route_dashboard` 220개 통과, Node 697개 통과(직전 확인).

## 개선 제안 (우선순위)

**P0 — 수치 진실성·모니터링 신뢰도**

1. CLI 표시 제공자여도 세션이 없으면 `_empty_usage` 대신 실제 transcript 사용량을 계산하고, 불가능하면 "미보고"로 표시한다(H1). UI 게이트를 `active_session`이 아니라 표시 제공자 기준으로 바꾼다.
2. 세션 실패율에서 취소를 분리하거나(권장: `failed - aborted`), 라벨·힌트·README를 실제 정의에 맞춘다(H2). 본문 타임아웃 요청은 `requests`에도 반영한다.
3. 라우팅 이벤트에 `instance_id`를 추가하고 대시보드가 필터/분리 표시한다(H3).
4. 회전 시 새 파일에 마커를 이어 쓰거나 `proxy_hash`를 metrics sidecar에 남긴다(H4).
5. 스냅샷 나이·실패를 payload에 넣고 알림 코드 2개를 등록해 헤더 배너를 띄운다(H5).

**P1 — 오탐/오해 제거**

6. live metrics가 덮어쓸 때 로그의 detail/model을 보존한다(M1).
7. 창 기반 회로 경고를 "현재 상태"가 아니라 "최근 발생"으로 쓰고, 현재형 critical은 최신 세션 이벤트에만 적용한다(M2).
8. totals/cost에 행 시각 기준 7일 필터를 적용하거나 라벨을 "보관 합계"로 바꾼다(M3).
9. 음수 age를 unknown으로 처리한다(M4).
10. 창 상한 도달 시 `truncated` 표시(M5), fallback 알림을 요청 단위로(M6), 실패 카드에 "시도(재시도 포함)" 명시(M7), 단가 실패 TTL 60초(M8), handoff pid 검증(M9).

**P2 — 확장·정리**

11. 이벤트 유형별 카운트·알림, 두 사용량 파이프라인 교차 검증, 비용 모델 혼합 표시.
12. 테스트가 실제 `.route.log`/`.usage-stats`를 건드리지 않도록 경로 고정.

## 반영 상태 (2026-11-01)

감사 후 사용자 지시(“권장 사항 순차 반영, 모든 연결점 동일 모니터링”)에 따라 아래 항목을 코드·테스트·문서에 반영했다. 이 절은 감사 시점 기록을 지우지 않고 이후 상태만 덧붙인다.

### P0

| 항목 | 상태 | 반영 내용 |
| --- | --- | --- |
| H1 CLI 사용량 0 단정 | ✅ | `snapshot()`·`status_report()`가 제공자와 무관하게 세션 기록을 스캔한다. payload에 `provider`를 추가하고, CLI 카드는 숨기지 않고 “CLI가 보고한 경우에만 집계·미보고는 0으로 추정하지 않음”을 표시한다. |
| H2 실패율에 취소 포함 | ✅ | 카드·흐름·인사이트·진단이 `failed - aborted`(음수는 0)로 계산하고 라벨을 “완료 응답 기준(취소 제외)”으로 바꿨다. 프록시 카운터 자체는 호환을 위해 취소를 `failed`에 계속 포함한다(README에 명시). |
| H3 다중 세션 오귀속 | ✅ | 프록시 로그 이벤트·시작 마커에 `instanceId`를 넣고, 대시보드는 최신 인스턴스를 기본으로 스코프하며 `instances`·`providers`를 별도 제공한다. 이전(무태그) 로그만 있으면 기존처럼 통합해 읽는다. |
| H4 회전 후 stale 신호 소실 | ✅ | 회전 시 새 파일 첫 줄에 `--- proxy start continued=1 instance=… code=… hash=… ---`를 쓰고, 대시보드는 cold read에서 64 KiB head를 훑어 마커를 복원하며 직전 마커를 이어 받는다. 단, 대시보드가 완전히 재시작하면 회전 이전 세션의 시작 시각은 복구하지 않는다(문서화된 한계). |
| H5 갱신 정지 미표시 | ✅ | payload에 `snapshot_age_seconds`·`snapshot_error`를 넣고 `snapshot_stale`(30초 초과)·`snapshot_refresh_failed` 알림 코드를 등록했다. 배너·카드·`--status` 종료 코드 3에 연결했다. |

### P1

| 항목 | 상태 | 반영 내용 |
| --- | --- | --- |
| M1 live가 detail/model 덮어씀 | ✅ | `_current_routing`이 live 값을 우선하되 비어 있는 `detail`/`provider`/`model`은 로그 값으로 채운다. Plan 결정에도 모델을 기록한다. |
| M2 현재형 critical 오탐 | ✅ | 회로 경고는 현재 `state`가 Plan일 때만 critical, 그 외에는 “지난 발생 기록” warn이며 종료 코드 3도 현재형에만 적용된다. |
| M3 7일 창 불일치 | ⚠️ 부분 | 주석·README를 “보관 폴더 합계, 오늘만 행 시각 기준”으로 정정했다. 행 시각 기준 누적 필터는 비용 재계산 비용이 커서 후속 과제로 남긴다. |
| M4 음수 age | ✅ | 미래 시각(5초 초과)은 `age=None`, 상태 `unknown`으로 처리한다. 테스트로 미래 이벤트가 external로 승격되지 않음을 고정했다. |
| M5 창 잘림 | ✅ | `truncated`를 2,000 이벤트·5,000 파싱 상한에서 계산하고 카드·`/api/status`(`window_truncated`)에 표시한다. |
| M6 fallback 1건 급증 오탐 | ✅ | 1건은 info “Plan 우회 발생”, 2건 이상부터 warn “Plan 우회 급증”이며 “결정은 요청과 다를 수 있음”을 문구에 넣었다. |
| M7 “실패=시도” 혼동 | ✅ | 카드 라벨을 “외부 시도 실패(재시도 포함)”로 명시했다. |
| M8 단가 실패 1시간 고정 | ✅ | 실패 캐시는 60초(`PRICING_ERROR_TTL`) 뒤 재시도, 성공 단가는 1시간 유지. |
| M9 handoff 신원 nonce 단일 | ⚠️ 부분 | handoff의 `instance_id`를 검증해 payload에 노출하고, `proxy_pid` 생존을 확인해 `pid_verified`를 표시한다. 다중 handoff 자체(세션별 조회)는 여전히 마지막 writer 기준이다. |

### 보안·운영 보강 (감사 P0/P1 이외)

- 계정(`/api/v1/key`)·단가(`/api/v1/models`) 요청이 **리다이렉트를 따라가지 않아** 제공자 키가 다른 출처로 전달되지 않는다.
- 계정 한도 캐시를 **API 키별**로 분리해 키 교체 직후 이전 키의 한도를 보여주지 않는다.
- `LOCAL_LLM_MODELS_URL`은 추론 URL과 **같은 출처**일 때만 로컬 키를 받는다(다른 출처면 `models_url_origin_mismatch`).
- 시작 시 JSON의 `read_only: True` 표기를 `mode: local_operator`·`config_writes: True`로 정정하고, README의 “상태 조회는 읽기 전용”을 GET/POST 구분으로 명확히 했다.
- 진단 모델 redaction이 `~/`, `file:` 같은 경로·스킴을 거부한다.

### 검증

- Python `python -m unittest discover -s tests -p 'test_*.py'`: **659개 통과(11 skip)** (신규 11개 포함).
- 대시보드 단위: `tests.test_route_dashboard` **235개 통과**. Node 전체 `node --test tests/*.test.mjs` **698개 통과**.
- Node: `tests/dashboard_dom.test.mjs` 82개, `tests/streaming.test.mjs` 16개, 프록시·로깅 316개(회전 연속 마커 검증 포함) 통과. 전체 스위트는 아래 최종 확인 절 참조.
- 대시보드를 새 코드로 재시작한 뒤 `/api/snapshot`·`/api/status`에서 `provider`, `snapshot_age_seconds`, `connection_counts`, `window_truncated`와 새 UI 카드(연결점별 결정·스냅샷 갱신)를 확인한다.

