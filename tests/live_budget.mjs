// Opt-in diagnostic helper: this limits only calls made through this wrapper.
// It does not restrict the CLI process, its descendants, or provider-side behavior.
export function singleOpenRouterRequest(send) {
  let sent = 0;
  let blocked = 0;
  const fetchImpl = (url, options = {}) => {
    if (url !== 'https://openrouter.ai/api/v1/messages' || options.method !== 'POST' || sent !== 0) {
      blocked++;
      throw new Error('Diagnostic outbound request blocked');
    }
    // Reserve synchronously, including when send throws or its promise rejects.
    sent++;
    return send(url, { ...options, redirect: 'error' });
  };
  return { fetchImpl, counts: () => ({ sent, blocked }) };
}
