# 라이선스 검토 / License review

검토일 / Reviewed: 2026-10-20. Source baseline: `e1f46501770bd33b82ba6a044f64f57c9b86d81d`.

## 한국어

**선정: MIT (SPDX: `MIT`).** 코드와 문서에 적용하며, 원문은 [LICENSE](LICENSE)에 둡니다.

### 전체 소스 검토 범위

| 파일 | 확인한 의존성·범위 |
| --- | --- |
| `claudeRouteProxy.mjs` | Node.js 내장 `node:http`, `node:events`, `node:url` 및 내장 fetch·stream API |
| `openrouterAdvisor.py` | Python 표준 라이브러리; 별도 설치된 `node`·`claude` 실행; 자체 검사 |
| `startOpenClaude.ps1` | PowerShell 기본 기능으로 Python 실행 |
| `tests/claudeRouteProxy.test.mjs` | Node.js 내장 assert·events·test, 프로젝트 프록시; 합성 인증값과 모의 응답 |
| 후속 Local 연동 파일 | `tests/localLlmIntegration.test.mjs`의 Node 내장 HTTP 서버, `tests/test_local_config.py`의 Python 표준 unittest·HTTP 서버·mock; 실행기의 urllib 메타데이터 조회도 표준 라이브러리 사용 |
| `README.md`, `.gitignore` | 문서·패턴; 기존 별도 라이선스 고지 없음 |

검토한 추적 파일에서 외부 패키지·vendored 구현·기존 저작권 또는 다른 라이선스 고지를 발견하지 못했습니다. 소스는 이 저장소의 최초 코드 반영 커밋에 함께 추가됐습니다. 후보 저장소의 문서 링크와 비교 기록 자체를 코드 포함의 증거로 보지는 않습니다. 이 검토는 해당 소스·고지·Git 이력의 확인이며, 이력에 남지 않은 외부 출처나 모든 권리 관계를 독립적으로 입증한 것은 아닙니다.

### 선정 이유와 적용 경계

- 작은 독립 라우터를 상업적·비상업적으로 재사용·수정·배포할 수 있게 하면서, 저작권·허가 고지 보존 의무를 명확하게 유지합니다. [MIT 공식 원문](https://opensource.org/license/mit).
- Apache-2.0도 허용형이며 명시적 특허 허여와 특허 소송 시 종료 조항, 수정·NOTICE 관련 조건을 둡니다. 이번 소스에서 Apache 채택을 강제하는 의존성이나 별도의 특허 정책 요구는 확인되지 않아 더 단순한 MIT를 선택했습니다. MIT가 Apache와 같은 명시적 특허 조항을 제공한다는 뜻은 아닙니다. [Apache-2.0 공식 원문](https://www.apache.org/licenses/LICENSE-2.0).
- Claude Code·Node.js·Python 런타임, OpenRouter·Anthropic 서비스와 모델은 배포에 포함하지 않습니다. 이 프로젝트의 MIT 선언으로 이들의 라이선스·이용약관·구독/과금 조건을 다시 허여하거나 변경하지 않습니다.
- 추후 외부 코드·패키지를 포함하면 해당 출처·라이선스·재배포 고지를 다시 검토해야 합니다. 이번에 별도 THIRD_PARTY_NOTICES를 만들지 않은 것은 현재 검토 범위에서 포함할 외부 배포물이 발견되지 않았기 때문입니다.

## English

**Selected license: MIT (SPDX: `MIT`).** It covers this repository's code and documentation; the authoritative project grant is in [LICENSE](LICENSE).

All checked-in source files were reviewed: the Node proxy, Python launcher, PowerShell launcher, and synthetic Node tests. Imports use Node.js built-ins, Python's standard library, and the local project module. The launcher invokes separately installed runtimes and Claude Code. The existing README and ignore patterns were also checked.

The later Local connector adds standard-library urllib metadata checks, Node built-in HTTP integration tests, and Python standard-library unittest/HTTP-server tests. It introduces no bundled external package.

No bundled third-party package, vendored implementation, or pre-existing copyright/license notice was found in the tracked files. The source files entered this repository together in the baseline commit above. Links to comparison repositories are not treated as evidence of embedded code. This is a source, notice, and available-history review, not independent proof of every right or of provenance absent from that history.

MIT permits reuse, modification, commercial use, and redistribution while requiring retention of its copyright and permission notice. Its short permissive terms suit this small standalone router. [Official MIT text](https://opensource.org/license/mit).

Apache-2.0 was considered for its explicit patent grant, patent-litigation termination, and redistribution/modification-notice terms. No included dependency or stated patent policy requires those additional terms here, so MIT was selected for simplicity. MIT does not supply Apache-2.0's explicit patent provisions. [Official Apache-2.0 text](https://www.apache.org/licenses/LICENSE-2.0).

The grant does not relicense Claude Code, Node.js, Python, services, or models, nor does it change subscription access or billing. No separate third-party-notices file was added because the reviewed distribution contains no identified bundled external component needing one. Any future external code or package requires a fresh license and attribution review.

### 2026-11-02 DeepSeek Harness 참고 검토 / DeepSeek Harness reference review

**결론: 코드 복사 없음, 라이선스 호환(MIT), 독자 재설계.** [`deepseek-ai/deepseek-harness`](https://github.com/deepseek-ai/deepseek-harness)는 **MIT**이며 `LICENSE`의 저작권·허가 고지를 보존하면 재사용·수정·배포가 가능합니다. 이번 성능 개선에서는 DSH의 **개념(배치 write-behind, bounded queue, flush 체크포인트, 스트림별 backpressure)** 만 참고했고 DSH 소스 코드·문서 문장을 복사하지 않았습니다. 따라서 DSH 저작권 고지를 이 저장소에 추가할 필요는 없습니다. 향후 DSH 코드를 실질적으로 복사·번역해 포함할 경우 MIT 고지와 저작권 표시를 `LICENSE`/`THIRD_PARTY_NOTICES`에 추가해야 합니다.

**English:** No code copied; MIT-compatible; independently redesigned. The reviewed [`deepseek-ai/deepseek-harness`](https://github.com/deepseek-ai/deepseek-harness) repository is MIT-licensed, so reuse, modification, and redistribution are permitted with its copyright and permission notice retained. This change used only the general design concepts (batched write-behind, bounded queues, flush checkpoints, per-stream backpressure); no DSH source or prose was copied, so no DSH notice is required here. If DSH code is copied or translated substantially in the future, its MIT copyright and permission notice must be added to `LICENSE` or a `THIRD_PARTY_NOTICES` file.

### 2026-11-02 OpenCode 참고 검토 / OpenCode reference review

**결론: 코드 복사 없음, 라이선스 호환(MIT), 독자 재설계.** [`anomalyco/opencode`](https://github.com/anomalyco/opencode)는 **MIT**입니다. 이번 모니터링 성능 개선에서는 OpenCode의 **HTTP 캐시·조건부 요청(ETag/304, HIT/MISS), 짧은 TTL, 요청/사용량 계측** 개념만 참고했고 소스 코드나 문서 문장을 복사하지 않았습니다. 따라서 OpenCode 저작권 고지를 추가할 필요는 없습니다. 향후 코드를 실질적으로 복사·번역해 포함하면 MIT 고지와 저작권 표시를 `LICENSE`/`THIRD_PARTY_NOTICES`에 추가해야 합니다.

**English:** No code copied; MIT-compatible; independently redesigned. [`anomalyco/opencode`](https://github.com/anomalyco/opencode) is MIT-licensed. This monitoring performance change used only general concepts — HTTP caching and conditional requests (ETag/304, HIT/MISS), short TTLs, request/usage instrumentation — with no OpenCode source or prose copied, so no OpenCode notice is required here. Substantial future copying or translation requires its MIT copyright and permission notice in `LICENSE` or `THIRD_PARTY_NOTICES`.

### 2026-11-02 DSH Superagent Prototype 참고 검토 / DSH Superagent Prototype reference review

**결론: 코드 복사 없음, 라이선스 호환(MIT), 독자 재설계.** [deepseek-ai/dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)은 **MIT**(Copyright (c) 2026 DeepSeek Harness Team)이며, 저장소 스스로 **연구용 prototype**이라고 밝힙니다. 이번 조사에서는 attention-first 패널, policy versioning, ticket/assignment 수명주기, retry health, outbox 같은 **도메인·운영 개념**만 참고했고 소스나 문서 문장을 복사하지 않았습니다. 따라서 저작권 고지를 추가할 필요는 없습니다. 향후 코드를 실질적으로 복사·번역해 포함하면 MIT 고지와 저작권 표시를 LICENSE/THIRD_PARTY_NOTICES에 추가해야 합니다.

**English:** No code copied; MIT-compatible; independently redesigned. [deepseek-ai/dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype) is MIT-licensed (Copyright (c) 2026 DeepSeek Harness Team) and describes itself as a research prototype. This review used only domain and operational concepts — attention-first panel, policy versioning, ticket/assignment lifecycle, retry health, outbox — with no source or prose copied, so no notice is required here. Substantial future copying or translation requires its MIT copyright and permission notice in LICENSE or THIRD_PARTY_NOTICES.
