"""Browser-only dashboard behavior; orchestration and action routes stay unchanged."""

DASHBOARD_SCRIPT = r"""
(() => {
  const $ = id => document.getElementById(id);
  const read = (key, fallback) => { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } };
  const save = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
  let interacting = false, audio = null, sound = false, polling = false;
  let volume = Number(read('symphony.volume', '50'));
  if (!Number.isFinite(volume)) volume = 50;
  volume = Math.max(0, Math.min(100, volume));
  $('alert-volume').value = volume;
  $('volume-value').textContent = `${volume}%`;
  const pauseRefresh = () => {
    interacting = true;
    $('refresh-status').textContent = 'Page refresh paused · alerts still checked';
  };
  document.addEventListener('input', pauseRefresh);
  document.addEventListener('symphony:reading', pauseRefresh);
  document.addEventListener('submit', pauseRefresh);
  document.querySelectorAll('[data-review-form]').forEach(form => {
    const key = 'symphony.review-draft:' + form.getAttribute('action');
    const fields = ['reviewer_identity', 'source_url', 'comments'];
    const status = form.querySelector('[data-review-status]');
    const draftStatus = form.querySelector('[data-review-draft]');
    const preview = form.querySelector('[data-review-preview]');
    const button = form.querySelector('button[type="submit"]');
    const clear = form.querySelector('[data-review-clear]');
    let submitting = false, submitted = false;
    const values = () => Object.fromEntries(fields.map(name => [name, form.elements[name].value]));
    const updatePreview = () => { preview.textContent = form.elements.comments.value || 'No feedback entered yet.'; };
    const removeDraft = () => { try { sessionStorage.removeItem(key); } catch {} };
    try {
      const draft = JSON.parse(sessionStorage.getItem(key) || 'null');
      if (draft && typeof draft === 'object') {
        fields.forEach(name => { if (typeof draft[name] === 'string') form.elements[name].value = draft[name]; });
        form.closest('details').open = true;
        pauseRefresh();
        draftStatus.textContent = 'Draft restored in this tab.';
      }
    } catch { draftStatus.textContent = 'Draft storage unavailable. Keep this tab open until submission succeeds.'; }
    updatePreview();
    form.addEventListener('input', () => {
      pauseRefresh(); updatePreview();
      try { sessionStorage.setItem(key, JSON.stringify(values())); draftStatus.textContent = 'Draft saved in this tab.'; }
      catch { draftStatus.textContent = 'Draft could not be saved. Keep this tab open until submission succeeds.'; }
    });
    clear.addEventListener('click', () => {
      if (submitting || submitted) return;
      form.elements.comments.value = ''; form.elements.source_url.value = '';
      removeDraft(); updatePreview(); status.textContent = '';
      draftStatus.textContent = 'Draft cleared.';
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (submitting || submitted) return;
      pauseRefresh(); submitting = true; button.disabled = true; clear.disabled = true;
      const payload = values();
      fields.forEach(name => { form.elements[name].readOnly = true; });
      status.textContent = 'Submitting review…';
      try {
        const response = await fetch(form.action, {
          method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
          body: JSON.stringify(payload), signal: AbortSignal.timeout(30000)
        });
        let data;
        try { data = await response.json(); } catch { throw new Error('The server returned an unreadable response.'); }
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Review could not be submitted.');
        if (!data.human_review?.id) throw new Error('The response did not identify the queued review.');
        submitted = true; removeDraft();
        draftStatus.textContent = 'Review saved to Symphony.';
        status.textContent = data.replayed ? 'This review was already received. Open its progress below.' :
          'Review received. Symphony will check its scope, apply corrections, and review the result.';
        const link = form.querySelector('[data-review-result]');
        link.href = '/reviews/' + encodeURIComponent(data.human_review.id); link.hidden = false;
        button.textContent = 'Review submitted';
      } catch (error) {
        status.textContent = `${error.message} Your feedback is still here. Retrying the same submission will not create duplicate work.`;
      } finally {
        submitting = false; button.disabled = submitted; clear.disabled = submitted;
        fields.forEach(name => { form.elements[name].readOnly = submitted; });
      }
    });
  });
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.matches('form[action$="/question"]')) return;
    event.preventDefault();
    pauseRefresh();
    const button = form.querySelector('button[type="submit"]');
    const answer = form.querySelector('[data-question-answer]');
    button.disabled = true;
    answer.textContent = 'Codex is answering… Your case remains unchanged.';
    try {
      const response = await fetch(form.action, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({question: form.elements.question.value}),
        signal: AbortSignal.timeout(70000)
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Question could not be answered.');
      answer.textContent = payload.answer;
    } catch (error) {
      answer.textContent = `${error.message} Your case has not resumed. You can retry the question.`;
    } finally { button.disabled = false; }
  });
  document.addEventListener('click', event => { if (event.target.closest('summary')) pauseRefresh(); });
  document.addEventListener('keydown', event => {
    if (event.target.closest('summary') && ['Enter', ' '].includes(event.key)) pauseRefresh();
  });
  function chime() {
    if (!sound || !audio || audio.state !== 'running') return;
    [660, 880].forEach((frequency, index) => {
      const tone = audio.createOscillator(), gain = audio.createGain();
      const start = audio.currentTime + index * 0.23;
      tone.frequency.value = frequency;
      gain.gain.setValueAtTime(0, start);
      gain.gain.linearRampToValueAtTime(volume / 100 * 0.2, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, start + 0.3);
      tone.connect(gain); gain.connect(audio.destination);
      tone.start(start); tone.stop(start + 0.32);
      tone.onended = () => { tone.disconnect(); gain.disconnect(); };
    });
  }
  $('sound-toggle').addEventListener('click', async () => {
    try {
      if (!audio) audio = new (window.AudioContext || window.webkitAudioContext)();
      await audio.resume();
      sound = !sound;
      $('sound-toggle').textContent = sound ? 'Mute sound' : 'Enable sound';
      $('sound-toggle').setAttribute('aria-pressed', String(sound));
      if (sound) { pauseRefresh(); chime(); poll(); }
    } catch { $('alert-status').textContent = 'Sound is unavailable in this browser.'; }
  });
  $('alert-volume').addEventListener('input', event => {
    volume = Number(event.target.value);
    save('symphony.volume', String(volume));
    $('volume-value').textContent = `${volume}%`;
  });
  $('alert-volume').addEventListener('change', chime);
  const desktop = $('desktop-toggle');
  let desktopEnabled = read('symphony.desktop', 'false') === 'true';
  if (!('Notification' in window)) {
    desktop.disabled = true;
    desktop.textContent = 'Desktop alerts unavailable';
  } else {
    desktop.addEventListener('click', async () => {
      try {
        if (desktopEnabled && Notification.permission === 'granted') {
          desktopEnabled = false;
          save('symphony.desktop', 'false');
          desktop.textContent = 'Enable desktop alerts';
          return;
        }
        const permission = await Notification.requestPermission();
        desktopEnabled = permission === 'granted';
        save('symphony.desktop', String(desktopEnabled));
        desktop.textContent = desktopEnabled ? 'Disable desktop alerts' : 'Desktop alerts blocked';
        poll();
      } catch { desktop.textContent = 'Desktop alerts unavailable'; }
    });
    if (desktopEnabled && Notification.permission === 'granted') desktop.textContent = 'Disable desktop alerts';
  }
  let seen;
  try { seen = new Set(JSON.parse(read('symphony.alerted', '[]'))); } catch { seen = new Set(); }
  function processState(state) {
    const runs = new Map((state.all_runs || []).map(run => [run.id, run]));
    document.querySelectorAll('[data-business-summary-run]').forEach(element => {
      const run = runs.get(element.dataset.businessSummaryRun);
      if (run) {
        if (element.textContent !== (run.business_summary || '')) {
          element.textContent = run.business_summary || '';
        }
        element.hidden = !run.business_summary;
        const row = element.closest?.('tr[data-case]');
        if (row) row.dataset.case = `${run.issue_identifier || ''} ${run.business_summary || ''}`.toLowerCase();
      }
    });
    filterCases();
    try { JSON.parse(read('symphony.alerted', '[]')).forEach(id => seen.add(id)); } catch {}
    // Use server actionability, not every historical blocked attempt.
    const waiting = (state.blocked_issues || []).filter(run => run.human_input_actionable);
    const fresh = waiting.filter(run => !seen.has(run.id));
    $('attention-summary').textContent = waiting.length
      ? `${waiting.length} case${waiting.length === 1 ? ' needs' : 's need'} your input: ${waiting.map(run => run.issue_identifier).join(', ')}`
      : 'No human input needed right now';
    $('attention-banner').classList.toggle('needs-attention', waiting.length > 0);
    document.title = waiting.length ? `(${waiting.length}) Input needed · Symphony` : 'Symphony Jira';
    if (fresh.length) {
      let delivered = false;
      if (sound && audio?.state === 'running' && volume > 0) { chime(); delivered = true; }
      if (desktopEnabled && 'Notification' in window && Notification.permission === 'granted') {
        try {
          const notification = new Notification('Symphony needs your input', {
            body: fresh.map(run => `${run.issue_identifier}: ${run.blocked_phase === 'planning_approval' ? 'approve plan' : 'feedback needed'}`).join('\n'),
            tag: 'symphony-human-input'
          });
          notification.onclick = () => { window.focus(); notification.close(); };
          delivered = true;
        } catch {}
      }
      if (delivered) {
        fresh.forEach(run => seen.add(run.id));
        save('symphony.alerted', JSON.stringify([...seen].slice(-200)));
      }
    }
  }
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const response = await fetch('/api/v1/state', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error('State unavailable');
      processState(await response.json());
      $('alert-status').textContent = `Alerts checked at ${new Date().toLocaleTimeString()}`;
    } catch { $('alert-status').textContent = 'Cannot check alerts · reconnecting automatically'; }
    finally { polling = false; }
  }
  const rows = [...document.querySelectorAll('tr[data-case]')];
  const allowedFilters = ['all', 'attention', 'running', 'queued', 'completed', 'failed'];
  const requestedFilter = typeof URLSearchParams !== 'undefined' ? new URLSearchParams(window.location.search || '').get('filter') : null;
  $('case-search').value = requestedFilter ? '' : read('symphony.case-search', '');
  const savedFilter = requestedFilter || read('symphony.case-filter', 'all');
  $('case-filter').value = allowedFilters.includes(savedFilter) ? savedFilter : 'all';
  function filterCases() {
    const query = $('case-search').value.trim().toLowerCase(), filter = $('case-filter').value;
    let count = 0;
    rows.forEach(row => {
      const matches = row.dataset.case.includes(query) && (filter === 'all' || row.dataset.filter === filter);
      row.hidden = !matches;
      if (matches) count++;
    });
    $('case-count').textContent = `${count} of ${rows.length} recent cases`;
    $('filter-empty').hidden = count !== 0 || rows.length === 0;
    save('symphony.case-search', $('case-search').value);
    save('symphony.case-filter', filter);
  }
  $('clear-filters').addEventListener('click', () => {
    $('case-search').value = ''; $('case-filter').value = 'all'; filterCases();
  });
  $('case-search').addEventListener('input', filterCases);
  $('case-filter').addEventListener('change', () => { pauseRefresh(); filterCases(); });
  filterCases(); poll();
  setInterval(poll, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  setInterval(() => { if (!interacting && !document.hidden) window.location.reload(); }, 60000);
})();
"""
