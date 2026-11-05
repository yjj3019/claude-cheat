# 검증 방법과 남은 한계 (Validation)

이 문서는 [README.md](../README.md)에서 분리한 검증 절입니다.
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
