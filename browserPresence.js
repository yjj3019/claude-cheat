// One command goes to one live tab. Draft controls survive configuration reloads.
(() => {
  const tab = globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2) + Date.now();
  const dirty = new Map();
  let handled = null, navigation = null, pending = false, timer, polling = false, active = true;
  const remember = event => {
    const field = event.target;
    if (field?.id && /^(cfg-|default-provider)/.test(field.id))
      dirty.set(field.id, { value: field.value, checked: field.checked });
  };
  document.addEventListener('input', remember, true);
  document.addEventListener('change', remember, true);
  const acknowledge = async id => {
    const response = await fetch('/api/browser', {method:'POST',
      signal:globalThis.AbortSignal?.timeout?.(2000),
      headers:{'x-dashboard-token':TOKEN,'content-type':'application/json'},
      body:JSON.stringify({action:'heartbeat',tab,page:location.pathname,
        focused:document.hasFocus?.() === true,visible:!document.hidden,ack:id})});
    return response.ok && (await response.json()).acknowledged === true;
  };
  const finishNavigation = async () => {
    // Keep the intent if the acknowledgement response is lost. The server keeps
    // receipts, so the next poll can confirm it without executing a stale command.
    if (await acknowledge(navigation)) {
      handled = navigation;
      navigation = null;
      location.assign('/settings');
    } else navigation = null;
  };
  async function heartbeat() {
    if (pending || !TOKEN) return;
    pending = true;
    try {
      if (navigation) { await finishNavigation(); return; }
      const response = await fetch('/api/browser', { method: 'POST', cache: 'no-store',
        signal:globalThis.AbortSignal?.timeout?.(2000),
        headers: {'x-dashboard-token': TOKEN, 'content-type': 'application/json'},
        body: JSON.stringify({ action:'heartbeat', tab, page: location.pathname, focused: document.hasFocus?.() === true, visible: !document.hidden, ack: handled }) });
      if (!response.ok) return;
      const command = (await response.json()).command;
      if (!command) return;
      if (command.id === handled) { await acknowledge(handled); return; }
      if (command.page === 'settings' && location.pathname !== '/settings' && dirty.size === 0) {
        // Acknowledge before navigation, so it does not repeat in another tab.
        navigation = command.id;
        await finishNavigation();
        return;
      }
      for (const [id] of dirty) {
        const field = document.getElementById(id);
        if (field?.dataset?.provider && typeof DIRTY !== 'undefined' && !DIRTY.has(`${field.dataset.provider}:${field.dataset.name}`)) dirty.delete(id);
        else if (id === 'default-provider' && typeof CONFIG !== 'undefined' && field?.value === CONFIG?.default_provider) dirty.delete(id);
      }
      if (typeof loadConfig === 'function') {
        await loadConfig();
        for (const [id, draft] of dirty) {
          const field = document.getElementById(id);
          if (field) { field.value = draft.value; field.checked = draft.checked; }
        }
      }
      if (typeof tick === 'function') await tick();
      if (dirty.size && typeof toast === 'function') toast('info', '최신 정보를 불러왔습니다. 입력 중인 설정은 유지했습니다.');
      handled = command.id;
      await acknowledge(handled);
    } catch (_) { /* retry after server restart */ }
    finally { pending = false; }
  }
  const poll = async () => {
    if (polling || !active) return;
    polling = true;
    try { await heartbeat(); }
    finally {
      polling = false;
      if (active) timer = setTimeout(poll, document.hidden ? 10000 : 1000);
    }
  };
  document.addEventListener('visibilitychange', () => { clearTimeout(timer); poll(); });
  window.addEventListener('pagehide', () => {
    active = false;
    clearTimeout(timer);
    if (TOKEN) fetch('/api/browser', {method:'POST', keepalive:true,
      headers:{'x-dashboard-token':TOKEN,'content-type':'application/json'},
      body:JSON.stringify({action:'close',tab})}).catch(() => {});
  });
  window.addEventListener('pageshow', () => { active = true; clearTimeout(timer); poll(); });
  poll();
})();
