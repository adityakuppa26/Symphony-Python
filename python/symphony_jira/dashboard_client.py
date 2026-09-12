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
    try { JSON.parse(read('symphony.alerted', '[]')).forEach(id => seen.add(id)); } catch {}
    // Use server actionability, not every historical blocked attempt.
    const waiting = (state.blocked_issues || []).filter(run => run.human_input_actionable);
    const fresh = waiting.filter(run => !seen.has(run.id));
    $('attention-summary').textContent = waiting.length
      ? `${waiting.length} case${waiting.length === 1 ? '' : 's'} need your input: ${waiting.map(run => run.issue_identifier).join(', ')}`
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
  function filterCases() {
    const query = $('case-search').value.trim().toLowerCase(), filter = $('case-filter').value;
    let count = 0;
    rows.forEach(row => {
      const matches = row.dataset.case.includes(query) && (filter === 'all' || row.dataset.filter === filter);
      row.hidden = !matches;
      if (matches) count++;
    });
    $('case-count').textContent = `${count} of ${rows.length} recent cases`;
  }
  $('case-search').addEventListener('input', filterCases);
  $('case-filter').addEventListener('change', () => { pauseRefresh(); filterCases(); });
  filterCases(); poll();
  setInterval(poll, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  setInterval(() => { if (!interacting && !document.hidden) window.location.reload(); }, 60000);
})();
"""
