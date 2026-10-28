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
