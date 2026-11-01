# 프록시 성능 시뮬레이션 결과 (2026-11-01)

합성(mock) 업스트림으로 측정했습니다. 실제 외부 모델·Local 서버·CLI 추론은 호출하지 않았고 비용도 발생하지 않았습니다. 즉 이 수치는 **라우터·프록시 자체의 처리 경로** 성능이며, 모델 품질이나 실제 제공자 지연이 아닙니다.

## 환경

| 항목 | 값 |
| --- | --- |
| CPU | Intel Core Ultra 7 258V, 8 logical CPUs |
| RAM | 31.5GB |
| OS | Windows |
| Node | v24.15.0 |
| Python | 3.14.4 |
| 기준 커밋 | `c4d0eb5` |

측정 중 다른 작업이 함께 도는 PC이며, 값은 실행마다 달라질 수 있습니다.

## 1. 혼합 트래픽 (`node tests/simulate.mjs`)

기본 프로필(1,200요청·동시성 24, 시나리오별 균등):

| 항목 | 값 |
| --- | --- |
| 처리량 | **854.2 req/s** (1.4s) |
| 지연 | p50 19.4ms · p95 69.5ms · p99 77.4ms · max 94ms |
| RSS | 67.7MB → 267.4MB (+199.7MB, 1MB 본문 포함 혼합 부하의 순간 증가) |

| 시나리오 | 요청 | 평균 | 최대 |
| --- | --- | --- | --- |
| simple | 240 | 28ms | 94ms |
| stream | 240 | 16.5ms | 26.4ms |
| tools | 240 | 15.7ms | 25.1ms |
| retry | 240 | 21.1ms | 75ms |
| large (1MB) | 240 | 56.9ms | 82ms |

장기 세션(60턴·본문 240.9KB): 첫 요청 14.5ms → 마지막 15.9ms로 증가폭이 작았습니다. 회로 시나리오는 bypass 200 · fallback 200 · cooldown 200 모두 기대대로 동작했습니다.

## 2. 고동시성 (`--requests 4000 --concurrency 48 --json`)

| 항목 | 값 |
| --- | --- |
| 처리량 | **1,121.2 req/s** (3.57s) |
| 지연 | p50 22.4ms · p95 121.4ms · p99 128.1ms · max 134.7ms |
| RSS | 68MB → 264.2MB (+196.3MB) |
| 라우팅 | external 4,000 / plan 0, streams 1,600, attempts 4,800, retries 800 |
| 회로 | replacement 4,000 · plan 4 |

동시성을 2배로 올리면 처리량은 약 31% 늘고 p95는 69.5 → 121.4ms로 상승했습니다(8 logical CPU에서의 포화 구간). `violations`는 비어 있었습니다.

## 3. 메모리 소크 (`--soak 3000 --concurrency 16`)

| 항목 | 값 |
| --- | --- |
| 처리량 | 784.1 req/s (1.53s) |
| 지연 | p50 13ms · p95 51.4ms · max 74.4ms |
| 소크 | 3,000요청 · **peak RSS 228.9MB · drift +28MB** |
| 창별 중앙 지연 | 15.6, 10.4, 10.6, 12.1, 9.8, 19.5, 27, 16.4, 15.6, 20.8, 23, 20, 14.7, 16, 16.8ms |

drift(증가폭)가 판정 기준이며, 3,000요청에서 누수 징후는 없었습니다. 창별 지연의 변동은 같은 PC에서 다른 작업이 함께 돈 영향입니다.

## 4. 분리 프로세스 부하 하네스

`node tests/bench_proxy.mjs --quick` (새 프로세스 2개 × 6라운드, 워밍업 1회):

| 워크로드 | 본문 | 중앙값 | p90 | 프로세스 중앙값 편차 |
| --- | --- | --- | --- | --- |
| 16KB | 15.7KB | 1.84ms | 4.27ms | 1.31x |
| 1MB | 1,023.7KB | **10.80ms** | 23.10ms | 1.02x |
| 긴 이력 120턴 | 247.2KB | 3.73ms | 4.37ms | 1.01x |

재시도 정확성(시도 횟수 단정):

| 시나리오 | 중앙값 | 호출/요청 | 판정 |
| --- | --- | --- | --- |
| 첫 시도 실패 | 19.79ms | 2 | OK |
| 두 번 실패 | 37.55ms | 3 | OK |
| 항상 실패 | 50.24ms | 4 | OK |

스트리밍 패스스루 OK(6 프레임·718바이트·본문 정확 일치), 정확성 게이트 **58응답 검증·실패 0**, verdict **PASS**.

`node tests/bench_proxy.mjs --soak-seconds 10` (표준 프로필, 새 프로세스 3개):

| 워크로드 | 중앙값 | p90 |
| --- | --- | --- |
| 16KB | 1.66ms | 2.31ms |
| 64KB | 1.56ms | 2.22ms |
| 256KB | 3.49ms | 6.07ms |
| 1MB | 9.76ms | 12.79ms |
| 긴 이력 120턴 | 3.37ms | 4.95ms |

- 재시도: 16.86ms(2회 호출) / 32.85ms(3회) / 51.99ms(4회) — 모두 기대 횟수와 일치
- 10초 소크: 6,115요청(**611.5 rps**), 시작/중간/끝 10.35/12.53/15.26ms, RSS 45.66MB·heap 증가 0.087MB → **OK**
- 정확성 게이트: **7,464응답 검증·실패 0**, verdict **PASS**

## 5. 대시보드 갱신 비용 (`python tests/bench_dashboard.py`, `PYTHONPATH` 필요)

| 항목 | 값 |
| --- | --- |
| 라우팅 로그 | 10.1MB |
| `parse_route_log` 콜드 전체 읽기 | 78.8ms |
| `parse_route_log` 증분(변화 없음 / 200줄 추가) | 0.5ms / 0.9ms |
| `scan_sessions` 첫 스캔(2,000파일) | 3,806.5ms (백그라운드) |
| `scan_sessions` 워밍 캐시(재시작) | **15.5ms** |
| `scan_sessions` 증분 / 한 파일 추가 | 13.0ms / 15.5ms |
| `snapshot` 첫 계산(콜드) | 117.8ms |
| `snapshot` 입력 변화 없음 | **7.2ms** |
| `snapshot` 새 데이터 직후 첫 갱신 | 46.3ms |

## 이전 문서 기재값과 비교

| 항목 | 이번 측정 | README 기재(이전 측정) | 비고 |
| --- | --- | --- | --- |
| 혼합 처리량 | 854 req/s (동시성 24) | 약 700–980 req/s | 범위 내 |
| 1MB 본문 | 9.76–10.80ms | 7.7–8.9ms | 이번 PC 상태에서 소폭 느림 |
| 긴 세션 120턴 | 3.37–3.73ms | 3.1ms(최적화 전 3.9ms) | 유사 |
| 소크 3,000요청 | peak 228.9MB · drift +28MB | peak 245MB · +5.6MB | drift는 실행 조건에 민감 |
| 10초 분리 소크 | 611.5 rps · heap +0.087MB | 1,253 rps · heap +0.09MB | 다른 작업 동시 실행 영향 |
| 세션 스캔 워밍 | 15.5ms | 20ms | 개선 |
| 스냅샷 무변화 갱신 | 7.2ms | 8ms | 유사 |

## 해석과 한계

- 모든 수치는 **합성 업스트림** 기준입니다. 실제 OpenRouter·Local LLM·CLI 추론의 지연·토큰 속도와 무관합니다.
- 같은 PC에서 다른 작업이 동시에 실행되어 처리량·p95·소크 drift가 흔들릴 수 있습니다. 비교는 절대값보다 같은 조건의 반복 측정 추세로 해야 합니다.
- 정확성 게이트(응답·스트리밍·업스트림 재작성 본문·재시도 횟수)는 모두 통과했습니다. 결과 JSON은 `.bench/`(git 제외)에 저장됩니다.

## 재현 명령

```powershell
node tests/simulate.mjs
node tests/simulate.mjs --requests 4000 --concurrency 48 --json
node tests/simulate.mjs --soak 3000 --concurrency 16
node tests/bench_proxy.mjs --quick
node tests/bench_proxy.mjs --soak-seconds 10
$env:PYTHONPATH='D:\AI-Codding\Claude\claude-cheat'; python tests/bench_dashboard.py
```


> 참고: 이후 프록시에 동시 요청 상한(기본 8)·진행 중 본문 상한이 추가되었습니다. 이 보고서의 처리량 수치는 상한이 없던 시점의 값이며, `tests/bench_proxy.mjs`와 `tests/simulate.mjs`는 라우팅 처리량을 측정하기 위해 하네스에서만 상한을 높여(`ROUTE_MAX_CONCURRENT_REQUESTS=64`·`ROUTE_MAX_CONNECTIONS=1024` 상당) 실행합니다. 상한 자체의 동작은 `tests/claudeRouteProxy.test.mjs`가 검증합니다.
