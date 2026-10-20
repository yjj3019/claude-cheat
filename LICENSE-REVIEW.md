# 라이선스 검토 / License review

검토일 / Reviewed: 2026-10-20. Source baseline: `e1f46501770bd33b82ba6a044f64f57c9b86d81d`.

## 한국어

**선정: MIT (SPDX: `MIT`).** 코드와 문서에 적용하며, 원문은 [LICENSE](LICENSE)에 둡니다.

### 전체 소스 검토 범위

| 파일 | 확인한 의존성·범위 |
| --- | --- |
| `claudeRouteProxy.mjs` | Node.js 내장 `node:http`, `node:events`, `node:url` 및 내장 fetch·stream API |
| `openrouterAdvisor.py` | Python 표준 라이브러리; 별도 설치된 `node`·`claude` 실행; stdio MCP 구현과 자체 검사 |
| `startClaudeOpenRouter.ps1` | PowerShell 기본 기능으로 Python 실행 |
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

All checked-in source files were reviewed: the Node proxy, Python launcher and consultation helper, PowerShell launcher, and synthetic Node tests. Imports use Node.js built-ins, Python's standard library, and the local project module. The launcher invokes separately installed runtimes and Claude Code. The existing README and ignore patterns were also checked.

The later Local connector adds standard-library urllib metadata checks, Node built-in HTTP integration tests, and Python standard-library unittest/HTTP-server tests. It introduces no bundled external package.

No bundled third-party package, vendored implementation, or pre-existing copyright/license notice was found in the tracked files. The source files entered this repository together in the baseline commit above. Links to comparison repositories are not treated as evidence of embedded code. This is a source, notice, and available-history review, not independent proof of every right or of provenance absent from that history.

MIT permits reuse, modification, commercial use, and redistribution while requiring retention of its copyright and permission notice. Its short permissive terms suit this small standalone router. [Official MIT text](https://opensource.org/license/mit).

Apache-2.0 was considered for its explicit patent grant, patent-litigation termination, and redistribution/modification-notice terms. No included dependency or stated patent policy requires those additional terms here, so MIT was selected for simplicity. MIT does not supply Apache-2.0's explicit patent provisions. [Official Apache-2.0 text](https://www.apache.org/licenses/LICENSE-2.0).

The grant does not relicense Claude Code, Node.js, Python, services, or models, nor does it change subscription access or billing. No separate third-party-notices file was added because the reviewed distribution contains no identified bundled external component needing one. Any future external code or package requires a fresh license and attribution review.
