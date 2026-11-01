# DSH Superagent Prototype 전수 조사 — 기능 개선점

조사 기준: [`deepseek-ai/dsh-superagent-prototype`](https://github.com/deepseek-ai/dsh-superagent-prototype) `6337fc8` (clone, read-only). 216개 파일, 9개 설계 문서, 13개 prompt, 2개 skill, 35개 source 모듈을 조사했습니다. 라이선스는 **MIT**([LICENSE-REVIEW](../LICENSE-REVIEW.md)).

> 이 저장소는 스스로 **“research prototype for reference only”**라고 밝힌 실험 코드입니다. 도메인 개념과 운영 원칙을 참고하되, 코드를 그대로 이식하지 않고 우리 제품 범위에 맞게 재설계해야 합니다.

## 1. 핵심 철학

> **Take as little human attention as possible, and give as much value as possible.**

모든 메시지·알림·질문·설정·패널 요소가 **사람의 주의(attention)를 비용으로 지불**한다고 보고, 그보다 큰 가치가 없으면 제거합니다.

- 요청이 있거나 **팀이 작성한 규칙이 허용할 때만** 행동
- 사람이 행동해야 하거나 결과를 쓸 수 있을 때만 발화
- 건강한 상태는 공간을 차지하지 않음
- 한 번에 **구체적인 질문 하나**
- 초기에는 좁게 시작하고, 소음이 많으면 prompt를 줄이고, 결과에 책임
- 불확실한 작업은 **기록만 하는 모드**부터

## 2. 도메인 모델

| 개념 | 역할 | 우리 제품에 주는 시사점 |
| --- | --- | --- |
| Source | GitHub/Feishu 같은 입력·출력 연결 | 모니터링 입력을 plugin source로 확장 가능 |
| Trigger | Source가 받은 메시지/이벤트 | route 실패·경고·세션 이벤트를 trigger로 모델링 |
| Thread | Trigger의 시간순 묶음, Eval 동시 1회 | 사건/세션별 이력 타임라인 |
| Eval | gate + assessment 2계층 판단, 무상태 | 알림/라우팅 정책 평가를 분리 |
| Decision | 시작/계속/중단 결정 | “왜 이 요청이 Plan으로 갔는가”를 구조화 |
| Mandate | accept가 부여한 **versioned authority**(scope·strength·rule) | 설정 변경·자동화 권한을 버전으로 고정 |
| Ticket | 승인된 작업 단위, open↔ended | 경고/장애를 **작업 항목**으로 승격 |
| Assignment | Ticket↔Executor 실행 결합 | 재시도/재할당 이력 |
| Subscription | Ticket↔Thread 다대다, 후속 대화 유입 | 알림 후속 조치·재개 추적 |
| Channel | 승인/답변/PR 등 쓰기 경로 | 알림 전달 채널 |
| Policy | versioned 규칙 + source별 admission | **정책 버전·우선순위·승인 목록** |
| Report/Attention | Executor 결과·인간 답변 대기 | “지금 사람이 필요한가” 상태 |

### Authorization의 두 경로

1. **Eval 경로**: 새 작업을 시작할지 판단. gate(누가 요청해도 되는가) + assessment(무엇을 할지)를 분리하고, policy version이 바뀌면 진행 중 Eval을 폐기.
2. **Subscription 경로**: 승인된 Ticket의 후속 메시지는 Eval 없이 Mandate 아래 재개. 단 사람이 stop하면 차단.

**시사점:** 우리 R1/라우팅 요구사항을 “정책”으로 두고, 설정 변경은 새 Policy version으로 기록하며, Live Session에는 변경된 정책을 **재검증**한 뒤 적용하는 구조가 안전합니다.

## 3. 패널 설계 — 세 가지 질문

패널은 열자마자 다음 순서로 답합니다.

1. **지금 내가 필요한 게 있나?** (person stage가 먼저)
2. **시스템이 건강하고 올바르게 동작하나?**
3. **한 작업의 이력은 어떠한가?** (Trigger → Eval → Ticket)

추가 원칙:
- **Overview는 상세 뷰가 아니라 요약·링크**; 상세는 Source/Eval/Execution 탭으로.
- **숫자는 링크한 목록 길이와 일치**, 상태끼리 모순 없음.
- **건강한 상태는 공간을 차지하지 않음**; 문제는 행동 가능해야 함.
- 일회성 네트워크 오류는 경고가 아님.
- 행에서 바로 action 제공, 파괴적 action은 확인.
- 검색, 레코드 상세(내부 ID는 접힌 영역), 설정 그룹.
- 설정은 **변경이 있을 때만 저장 바** 표시, 저장 실패/성공을 정직하게.

**우리 대시보드 개선:** 현재 한눈에 보기/세션/차트/설정 구조를 “**지금 필요한 것 → 건강 상태 → 이력**” 순으로 재배치하고, 경고를 단순 배너가 아니라 **조치 가능한 작업 항목**으로 승격하는 것이 가장 큰 UX 개선입니다.

## 4. 실행·복구에서 얻을 점

- Ticket stage를 **사람이 필요한 상태 vs 시스템 작업 중 상태**로 명확히 분리.
- Worker slot·background job 수명·30분 제한·handoff 시 해제.
- 입력 전달 실패는 **2초 → 지수 백오프(최대 1분)** 재시도; 성공/새 Assignment/Ticket 종료가 해제.
- Executor 장애 시 Assignment 재시도·재시작, 보존된 입력.
- **RetryHealth**: 세션 이벤트 스트림에서 model budget/retry 상태를 관측하되 로그·정책은 변경하지 않음.
- 문제가 3회 연속 실패해야 시스템 문제로 보고(일시 오류와 구분).
- `status`가 4가지 exit code(정상/재시작 필요/설정 문제/셋업 대기)로 자동화와 사람 모두에게 동일한 계약 제공.

**우리 제품 적용:** 다중 세션 모니터링에 **세션별 retry health**(예: rate-limit/예산/백오프)와 **연속 실패 임계값**을 추가하고, `/api/status` exit code 계약을 문서화하면 운영 자동화가 쉬워집니다.

## 5. 설정·정책·버전

- `Versions`: 모든 설정 저장을 numbered snapshot으로 남기고 current pointer만 전진 → **전체 이력 보존, 활성 레코드 중복 없음**.
- `SettingsHistory`: revision 간 **구조화 diff**(rule added/renamed/instruction/enabled 등)와 panel 렌더링.
- Policy version이 바뀌면 live Eval 폐기·Ticket Mandate 재검증.
- Limits를 host schema와 panel form이 공유.

**우리 제품 적용:** 대시보드 설정(provider, routing, CLI, dashboard token 외)을 **revision + diff + rollback**으로 저장하고, 변경 적용 전 `policy check`를 통과시키는 기능이 유용합니다.

## 6. Outbox·알림·소음 제어

- 알림/답장을 outbox에 넣고 **재시도·백오프·상태** 보존.
- 성공/전달 실패/멱등성/중복 방지.
- 시스템 문제는 3회 연속 실패 후에만 노출.
- 건강 상태는 공간을 차지하지 않음.

**우리 제품 적용:** 현재 desktop notification은 fire-and-forget입니다. **알림 outbox + 전달 상태 + 재시도**를 추가하고, 경고를 3회 임계값으로 debounce하면 소음이 줄어듭니다.

## 7. Source plugin과 skill

- Source interface: config schema, fields, health, message→Trigger 변환, scope, reply Channel, thread context, admission, executor variables, plugin loop, tests.
- GitHub/Feishu skill로 운영 절차를 문서화.
- Prompt를 versioned config로 관리하고 팀이 문장으로 행동을 조정.

**우리 제품 적용:** route log·transcript·webhook·Feishu/Slack을 **monitoring source plugin**으로 추상화하고, source별 admission(누가 알림을 발생시킬 수 있는가)을 둘 수 있습니다. 단 개인용 proxy에서는 우선순위가 낮습니다.

## 8. 우리 제품에 적용할 우선순위

| 우선순위 | 개선 | 근거 | 기대 효과 |
| --- | --- | --- | --- |
| **P0** | **Attention-first 운영 패널**: “지금 필요한 것” 큐, 경고→작업 항목, 건강 상태 접힘, 이력/검색 | 패널 3질문, person stage, quiet by default | 소음 감소, 실제 조치 누락 방지 |
| **P0** | **Versioned 설정·정책**: revision, diff, rollback, 변경 전 정책 검증 | Versions, SettingsHistory, Policy/Mandate | 설정 사고·회귀 추적, R1 같은 요구 고정 |
| **P1** | **세션별 RetryHealth**: rate-limit/예산/백오프/연속 실패, 3회 임계값 | RetryHealth, retry stage, 3회 규칙 | 장애 원인·다음 재시도 가시화 |
| **P1** | **알림 outbox**: 재시도·백오프·전달 상태·중복 제거 | outbox, delivery stage | 알림 유실/중복 감소 |
| **P1** | **셋업·상태 계약**: setup 요구사항, status exit code 4종, 관리 API | SETUP.md, deployment status | 자동화·온보딩 개선 |
| **P2** | **Monitoring source plugin**와 source별 admission | Source plugin, admission matrix | webhook/채팅 연동 확장 |
| **P2** | **Ticket 이력 상세**: Trigger→Decision→실행→결과의 접힌 상세·검색 | Thread/Ticket/Report | 감사·디버깅 |
| **P2** | **Prompt/skill 버전 관리** | Prompts/Versions | 운영 문구 변경 추적 (현재 proxy엔 우선순위 낮음) |
| 제외 | PR/merge, worker slot, per-ticket directory, Feishu/GitHub source, tool permission | 실행 agent 고유 영역 | 현 proxy/launcher 범위 밖 |

## 8.1 구현 현황 (2026-11-02)

- P0 **Attention-first 운영 큐**: 구현됨 — `POST /api/attention`, ack/snooze/resolve/reopen, 자동 해결 이력, 건강한 상태 접힘.
- P1 **세션별 RetryHealth**: 부분 구현됨 — rate-limit/재시도/회로를 세션 표에 표시하고 속도 제한을 주의 큐로 승격.
- P0 **Versioned 설정·정책**: 아직 미구현 — 다음 작업 후보.
- P1 **알림 outbox**, **settings diff/rollback**: 아직 미구현.

## 9. 라이선스

Superagent Prototype은 **MIT**입니다. 이번 조사는 개념 참고이며 코드·문장을 복사하지 않았습니다. 향후 코드를 실질적으로 복사·번역하면 MIT 저작권·허가 고지를 `LICENSE`/`THIRD_PARTY_NOTICES`에 추가해야 합니다.

## 10. 결론

가장 값어치 있는 이식은 **기능 추가**가 아니라 **운영 철학**입니다.

1. 대시보드를 “지표 나열”에서 **“사람이 필요한 것 우선”**으로 바꾼다.
2. 경고를 일회성 메시지가 아니라 **상태·이력·조치가 있는 작업 항목**으로 만든다.
3. 설정과 정책을 **버전·diff·재검증**으로 다룬다.
4. 알림·재시도·건강 상태를 **3회 임계값과 outbox**로 조용하게 만든다.

이 네 가지가 우리 `claude-cheat` 대시보드에 가장 자연스럽게 확장되는 개선점입니다.
