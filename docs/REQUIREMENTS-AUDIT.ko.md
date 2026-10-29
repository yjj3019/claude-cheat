# Claude-Cheat 요구사항 점검

기준: 2026-10-28. CLI의 의미는 사용자가 확정한 **Claude Code 유지 + CLI를 모델 백엔드로 호출**입니다. 테스트 통과와 실제 사용자 환경의 연결 성공을 구분합니다.

| 요구사항 | 반영 내용 | 검증·제한 |
| --- | --- | --- |
| 저장소 최신 여부 | origin fetch 후 HEAD와 origin/main 대조 | 점검 시 양쪽 `3190db9`, ahead/behind 0/0. 작업 중 변경은 별도입니다. |
| 프로젝트 명칭 | Claude-Cheat와 제공자 이름 구분 | OpenRouter는 제공자 중 하나입니다. |
| 브라우저 연결 중단 | 헤더·본문 쓰기의 연결 종료 예외 처리 | 중단된 연결에 500 응답을 재전송하지 않습니다. |
| 기본값 저장 | OpenRouter·Local·CLI 3종 선택·저장 | 새 세션은 저장값 사용, 실행 중 세션은 유지합니다. |
| CLI 하위 종류 | Claude Code·Codex·Grok 선택 | 검증된 버전만 실행합니다. Grok 격리는 미검증입니다. |
| 원본 기능 유지 | 전면 Claude Code 및 사용자 설정·MCP·Skills·Plugins·hooks·권한 유지 | 네이티브 관리·도움말 및 실행 인자 전달을 지원합니다. 모델별 기능 품질의 동일성은 보장하지 않습니다. |
| SSH | 전면 MCP가 SSH 도구 실행 | 기존 Local 세션에서 SSH MCP 도구 호출·결과 왕복을 확인했습니다. 최신 런처 전체 실사용 재검증은 전면 Claude 인증이 필요합니다. |
| Local 우선 사용 | 저장된 Local 기본값 유지, OpenRouter 무조건 연결 검사 제거 | 실제 사용자 설정을 CLI로 바꾸지 않았습니다. |
| 현재 모델 | 설정·선택 모델과 실제 응답 모델 분리 | 보고되지 않은 모델을 추측하지 않습니다. |
| 모델 중복 | 정확한 모델 ID 기준 중복 제거 | 대소문자·태그가 다른 ID는 임의 병합하지 않습니다. |
| 대시보드 상태 | 현재 세션·최근 라우팅·실패·오래된 프록시 판정 | 과거 세션 로그와 현재 실행을 구분합니다. 오래된 실행기는 재시작이 필요합니다. |
| AI 진단 API | 읽기 전용 `/api/diagnostics`, 연결 테스트 분리 | 상태 조회는 유료 추론을 유발하지 않습니다. 인증·CLI 원문은 노출하지 않습니다. |
| 컴포넌트 연동 | 저장→런처→프록시→백엔드→모델/도구→진단 연결 | Python·Node·DOM 및 인자 전달 회귀 검사로 확인합니다. |
| 오류·취소 | 분류된 오류, 시간 제한, CLI 단일 시도, 취소 정리 | 실패 시 일반 모드는 Plan 경로를 사용합니다. |
| 비용 표시 | OpenRouter만 계정·단가 조회 | Local·CLI의 미보고 토큰·비용을 실측값처럼 표시하지 않습니다. |
| Cursor 저장소 참고 | 비대화형 실행, 명확한 옵션·오류, 문서와 동작 일치 적용 | 전체 저장소의 기능을 이식했다는 의미는 아닙니다. README에 참고 원문 링크가 있습니다. |
| 문서 | 한국어·영문 README의 CLI·기본값·기능 정책 현행화 | 구현 및 검증 범위를 명시합니다. |
| 커밋·푸시·배포 | 검증 후 실제 수행 결과로 판단 | 이전 배포와 이번 작업 중 변경을 혼동하지 않습니다. 실행 중 사용자 세션은 임의 종료하지 않습니다. |

## 2026-10-29 Agent Teams·병렬 기능 보존 재점검

원본 Claude Code를 실행하고 설정을 유지하는 것은 전체 기능 동등성의 증명이 아닙니다. Agent Teams의 생성·메시지·완료, 병렬 subagent와 SSH/MCP·Skills·Plugins·hooks의 조합을 최신 실행에서 전수 검증하지 않았습니다. 일반 Local 모드의 프록시 동시2/대기8/5초 정책은 정상 병렬 작업도 Plan으로 우회시킬 수 있습니다. 명시적 LocalMode의 background 제한과 compat 모드의 환경·모델 재설정도 구분해야 합니다.

[DeepSeek Harness·OpenCode 정밀 비교](HARNESS-COMPATIBILITY-REVIEW.ko.md)에 고정 커밋, 코드·테스트 근거, 모드별 제약, 우선순위와 합격 기준을 기록했습니다. 보완안은 아직 런타임에 적용하지 않았습니다.

## 남은 확인 사항

- Grok은 자체 도구를 완전히 비활성화하는 실행 프로필이 검증될 때까지 백엔드 실행을 거부합니다.
- 최신 전면 Claude Code 인증 점검은 로그인되지 않은 상태였습니다. 인증 파일은 변경하지 않았으며 사용자 로그인 없이 최신 런처의 실제 전체 대화를 완료했다고 주장하지 않습니다.
- 합성 테스트는 네트워크 오류·취소·모델 보고·도구 왕복의 계약을 확인합니다. 모든 모델과 모든 Claude 고유 기능의 동일성을 증명하지 않습니다.
- 소스 변경 전 시작한 사용자 프록시는 최신 코드가 아닙니다. 대시보드 갱신만으로 해당 프로세스의 코드가 교체되지는 않습니다.

## Integration verification

- Full Node suite: 538 passed, no failures.
- Full Python suite: 514 run, 9 skipped; two process-timing failures were investigated separately. Authentication classification passed on serial rerun. The inherited-handle test now checks successful completion before its deadline and actual descendant cleanup, with a child lifetime exceeding that deadline. Final targeted results are recorded in the completion report.
- Live dashboard: `/settings` and `/api/diagnostics` returned HTTP 200; saved Local provider and selected DeepSeek model matched. Served settings contain CLI, Claude Code, Codex and Grok options.
- Independent static review found no confirmed critical/high-severity defects. Real authenticated CLI-backend conversations remain unverified. Automated Chrome screenshot capture timed out; visual layout verification is incomplete.


## 후속 구현·검증 (2026-10-29)

앞선 표와 5초 대기 설명은 최초 조사 시점의 기록입니다. 사용자 승인에 따라 Local 큐 대기를 기본 60초로 변경했습니다. `ROUTE_LOCAL_QUEUE_WAIT_MS`로 0~60000ms 범위에서 설정하며 동시 실행 2개·대기 8개는 유지합니다. 큐 만료·포화 시 기존 Plan/오류 정책을 적용합니다. 이는 추론과 재시도를 포함한 전체 응답 기한이 아닙니다. 실행 중인 프록시에는 재시작 후 적용됩니다.

호환 실행 경로에서 명시된 Agent Teams·subagent·작업 목록·background·IDE·plugin·skill 환경 선호를 보존합니다. 인증 격리와 명시적 제한 LocalMode는 유지합니다. Claude 자체 관리 명령을 모델 연결 검사 없이 전달하도록 보완했으나, `--bg` 이후 프록시 수명 문제까지 해결한 것은 아닙니다.

Plan의 날짜형 Retry-After를 초로 변환하고 일반적인 기능 거부 오류의 본문·헤더 보존을 검증했습니다. 빈 응답은 본문이나 도구 인자를 기록하지 않고 `validation_detail`의 제한된 분류값으로 구분합니다. 자격증명 마스킹과 오류 크기 제한은 유지합니다.

회귀 검증: 대상 Node 테스트 318개, 전체 Node 테스트 598개 통과. Python 런처 테스트 11개 통과, 설정 테스트 118개 실행 중 1개 건너뜀, 실패 없음. 실제 설치된 Claude Code 2.1.312의 격리된 합성 백엔드에서 하위 에이전트 2개 및 4개가 직접 연결과 프록시 연결 모두 병렬 실행·완료됨을 확인했습니다. 4개 시험에서 프록시는 최대 2개씩 처리했고 Local 6건·Plan 0건으로 고유한 자식 결과 4개와 부모 완료를 확인했습니다. 재현 명령은 `node tests/native_subagents_smoke.mjs 2` 및 `node tests/native_subagents_smoke.mjs 4`입니다. 이는 실제 공유 Local 모델의 추론·도구 수행 능력이나 Agent Teams 전체 과정 검증을 대신하지 않습니다.

남은 주요 경계는 사용자 지정 API URL에서의 Remote Control 제한, 분리 실행(`--bg`)의 프록시 수명, 실제 인증 환경의 Agent Teams·SSH/MCP·Skills·Plugins 통합 검증입니다. 전체 기능 동등성은 아직 입증되지 않았습니다.
