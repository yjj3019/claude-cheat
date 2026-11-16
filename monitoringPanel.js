// Operational evidence uses runtime models and timestamped samples.
const renderWithGatewayEvidence = render;
render = function(data) {
  const expanded = new Set(Array.from(document.getElementById('gateway-evidence')?.querySelectorAll('details[open]') || []).map(item => item.dataset.section));
  renderWithGatewayEvidence(data);
  let panel = document.getElementById('gateway-evidence');
  if (!panel && typeof document.createElement === 'function') {
    panel = document.createElement('section'); panel.id = 'gateway-evidence'; panel.className = 'card col-12';
    document.getElementById('grid')?.appendChild(panel);
  }
  if (!panel) return;
  const metrics = data.proxy_metrics || {};
  const local = metrics.localAdmission || {};
  const shared = metrics.sharedAdmission || {};
  const n = value => typeof value === 'number' ? num(value) : '—';
  const startup = (data.startup_history || []).slice(0, 5);
  const requests = (metrics.recentRequests || []).slice(-10).reverse();
  const rows = (data.sessions || []).map(row => `<tr><td>${esc(row.project || row.instance_id || '')}</td>`
    + `<td>${esc(row.model || '미확인')}</td><td>${row.stale ? '수집 지연' : '정상'} · ${n(row.age_seconds)}초 전</td>`
    + `<td>${n(row.local_active)} / ${n(row.local_concurrency)}</td><td>${n(row.local_queued)}</td></tr>`).join('');
  panel.innerHTML = `<header class="card-h"><h2>Gateway 진단</h2></header><div class="card-b">`
    + `<p>현재 실행 모델: <b>${esc(data.model || '미확인')}</b> · 다음 실행 설정: ${esc(data.next_model || '미설정')}`
    + `${data.configuration_pending ? ' · 새 세션부터 적용' : ''}</p>`
    + `<p>세션 Local 실행 ${n(local.active)} · 대기 ${n(local.queued)} · 거절 ${n(local.rejected)}</p>`
    + (shared.enabled ? `<p>같은 서버 전체 실행 ${n(shared.active)} / ${n(shared.concurrency)} · 대기 ${n(shared.queued)} / ${n(shared.queueLimit)}`
      + ` · 관측 ${esc(shared.observedAt || '대기 중')} · ${shared.stale ? '수집 지연 — 마지막 관측값' : '2초 간격 조회'}</p>` : '<p>Local 제한 범위: 세션별</p>')
    + `<details data-section="sessions"><summary>세션별 수집 상태</summary><table><thead><tr><th>세션</th><th>실행 모델</th><th>수집</th><th>Local 실행/상한</th><th>대기</th></tr></thead><tbody>${rows}</tbody></table></details>`
    + `<details data-section="startup"><summary>최근 시작 결과</summary><table><thead><tr><th>시각</th><th>서비스</th><th>단계</th><th>결과</th><th>종료 코드</th></tr></thead><tbody>`
    + startup.map(row => `<tr><td>${esc(row.at || '')}</td><td>${esc(row.provider || '')}</td><td>${esc(row.stage || '')}</td><td>${esc(row.code || '')}</td><td>${n(row.exit_code)}</td></tr>`).join('')
    + `</tbody></table></details><details data-section="requests"><summary>실패·5초 이상 요청 — 최근 10건</summary><p>프롬프트와 응답 내용은 수집하지 않습니다. 단계 시간은 중첩될 수 있어 합산하지 않습니다.</p>`
    + `<table><thead><tr><th>추적 ID</th><th>상태</th><th>대기</th><th>외부 시도</th><th>재시도 대기</th><th>전체 ms</th></tr></thead><tbody>`
    + requests.map(row => `<tr><td>${esc(row.traceId || '')}</td><td>${esc(row.outcome || '')} (${n(row.status)})</td><td>${n(row.phasesMs?.queueWait)}</td><td>${n(row.phasesMs?.externalAttempt)}</td><td>${n(row.phasesMs?.retryWait)}</td><td>${n(row.phasesMs?.total)}</td></tr>`).join('')
    + `</tbody></table></details></div>`;
  for (const detail of panel.querySelectorAll('details')) detail.open = expanded.has(detail.dataset.section);
};
