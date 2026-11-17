"""Shared dashboard styling and small, progressively enhanced interactions."""

DASHBOARD_UX_CSS = """
:root { --ink:#172b4d; --muted:#52657c; --line:#dce4ee; --accent:#3159cf; font-family:Inter,ui-sans-serif,system-ui,sans-serif; }
* { box-sizing:border-box; }
body { margin:0; background:#f6f8fc; color:var(--ink); line-height:1.55; }
main { width:min(1440px,calc(100% - 3rem)); }
header { padding:2rem 0 1.4rem; }
h1 { letter-spacing:-.045em; }
.subtitle { max-width:44rem; }
a { color:var(--accent); text-underline-offset:3px; }
button,a,input,select,textarea,summary { -webkit-tap-highlight-color:transparent; }
button:disabled { opacity:.6; cursor:wait; }
button,.artifact-link { min-height:40px; display:inline-flex; align-items:center; justify-content:center; }
summary { padding:.25rem 0; line-height:1.6; }
summary::marker { color:#75869e; }
.grid { gap:1rem; }
.panel { box-shadow:none; padding:1.1rem 1.25rem; min-height:6.5rem; }
.panel-label a { color:inherit; text-decoration:none; }
.panel-label a:hover { text-decoration:underline; }
.metric { font-variant-numeric:tabular-nums; }
.workflow-guide { background:transparent; border:0; padding:.25rem 0 1.1rem; font-size:.78rem; color:var(--muted); }
.utility-settings { margin:.8rem 0; max-width:none; border:1px solid var(--line); border-radius:12px; background:white; padding:.65rem 1rem; }
.utility-settings > summary { font-size:.82rem; }
.utility-settings .alert-controls { border:0; border-top:1px solid var(--line); border-radius:0; padding:1rem 0 0; }
.case-tools { padding:1rem; border:1px solid var(--line); border-radius:12px; background:#fff; gap:.8rem; }
.case-tools label:first-child { flex:1; min-width:12rem; }
.case-tools input,.case-tools select { min-height:42px; }
.case-count { margin-left:auto; font-size:.8rem; }
.table-shell { box-shadow:0 4px 20px #172b4d06; border-color:var(--line); }
th { padding:1rem .85rem; font-size:.68rem; }
td { padding:1.1rem .85rem; }
.business-summary { min-width:10rem; max-width:20rem; font-size:.84rem; line-height:1.6; }
.issue-key { font-size:1rem; }
.issue-meta { color:var(--muted); }
.details-stack { min-width:0; gap:.65rem; }
.case-details { min-width:12rem; max-width:40rem; }
.case-details > summary { color:var(--accent); }
.case-details[open] { padding:.65rem; border:1px solid var(--line); border-radius:10px; background:#fff; }
.case-primary-link { margin-bottom:.65rem; }
.case-preview { margin:0 0 .65rem; max-width:25rem; font-size:.84rem; line-height:1.6; }
.pipeline { margin-top:.5rem; }
.pipeline > summary { font-size:.75rem; }
.verification-note { font-size:.76rem; line-height:1.65; }
.verification-actions { gap:.5rem; }
.verification-actions button { width:100%; }
.verification-actions button[value="retry"] { background:white; color:var(--accent); border-color:#bdcbe3; }
.handoff-note { max-width:none; background:#f1f8f5; border:1px solid #c9e3d5; border-left:3px solid #229168; border-radius:8px; padding:.9rem 1rem; }
.handoff-note p { margin:.5rem 0; }
.brief-summary { font-size:.84rem; }
.artifact-path { color:var(--muted); }
.artifact-location { display:flex; align-items:flex-start; gap:.5rem; margin-top:.4rem; }
.artifact-location code { flex:1; min-width:0; }
.copy-path { min-height:32px; padding:.2rem .55rem; font-size:.72rem; color:var(--accent); background:white; border:1px solid #cbd5e1; }
.inline-status { font-size:.8rem; line-height:1.5; margin:.4rem 0; }
.filter-empty { margin:1rem 0; padding:2rem; text-align:center; background:white; border:1px dashed #c1ccdc; border-radius:12px; }
.skip-link { position:absolute; top:-5rem; left:1rem; z-index:20; background:white; padding:.7rem; }
.skip-link:focus { top:.5rem; }
.case-page { width:min(1050px,calc(100% - 2rem)); margin:0 auto 3rem; }
.case-page .case-overview { display:flex; flex-wrap:wrap; gap:.75rem; align-items:center; margin:.75rem 0 1.5rem; }
.case-page details,.case-page .brief-summary { max-width:none; }
.case-page .details-stack > details { padding:.75rem 1rem; background:white; border:1px solid var(--line); border-radius:10px; }
.case-page .detail-grid { display:grid; gap:1rem; }
.case-page textarea { width:100%; box-sizing:border-box; }
.case-page form { display:grid; gap:.7rem; margin:1rem 0; }
.case-page label { display:grid; gap:.4rem; }
.case-page input,.case-page textarea { padding:.7rem; border:1px solid #aab8c9; border-radius:7px; font:inherit; }
.case-page button { width:fit-content; padding:.55rem .85rem; border:1px solid var(--accent); border-radius:7px; background:var(--accent); color:white; font:inherit; cursor:pointer; }
.case-page .copy-path { color:var(--accent); background:white; }
.case-page .verification-actions { display:flex; flex-wrap:wrap; align-items:center; }
.case-page .verification-actions button { width:auto; }
.case-page .artifact-link { padding:.35rem .65rem; border:1px solid var(--line); border-radius:7px; text-decoration:none; }
.case-page .badge { padding:.25rem .6rem; border-radius:20px; background:#e9eef8; font-size:.8rem; }
.case-page .handoff-note { margin-bottom:1rem; }
@media(max-width:960px) {
 main { width:calc(100% - 1.25rem); }
 .grid { grid-template-columns:repeat(3,minmax(0,1fr)); gap:.5rem; }
 .panel { padding:.75rem; }
 .panel .chips { display:none; }
 .panel-label { font-size:.65rem; }
 .metric { font-size:1.5rem; }
 .case-tools { align-items:stretch; }
 .case-tools label:first-child { flex-basis:100%; }
 .case-details { min-width:0; max-width:none; }
 .business-summary,.verification-note { max-width:none; min-width:0; }
 .verification-actions { display:flex; flex-wrap:wrap; }
 .verification-actions button { width:auto; }
 .table-shell { border:0; background:transparent; box-shadow:none; }
 tbody tr { display:grid; grid-template-columns:1fr 1fr; background:white; border:1px solid var(--line); border-radius:12px; margin-bottom:1rem; }
 .table-shell td { width:auto; min-width:0; padding:.5rem .25rem; }
 .table-shell td:nth-child(1),.table-shell td:nth-child(4),.table-shell td:nth-child(6) { grid-column:1/-1; }
 .case-count { margin-left:0; }
}
@media(prefers-reduced-motion:reduce) { * { scroll-behavior:auto!important; transition:none!important; } }
"""

CASE_INTERACTIONS = r"""
(() => {
 document.addEventListener('click', async event => {
   const button = event.target.closest('[data-copy-path]');
   if (!button) return;
   try {
     await navigator.clipboard.writeText(button.dataset.copyPath);
     button.textContent = 'Copied';
   } catch { button.textContent = 'Select path to copy'; }
   setTimeout(() => { button.textContent = 'Copy'; }, 2500);
 });
 document.addEventListener('submit', async event => {
   const form = event.target;
   const verify = form.matches('form[data-verification-form]');
   const question = document.body.dataset.casePage === 'true' && form.matches('form[action$="/question"]');
   if (!verify && !question) return;
   event.preventDefault();
   if (form.dataset.busy === 'true') return;
   form.dataset.busy = 'true';
   const buttons = [...form.querySelectorAll('button[type="submit"],button[name="mode"]')];
   const payload = verify ? {mode: event.submitter?.value || 'all'} : {question: form.elements.question.value};
   const status = form.querySelector('[data-action-status], [data-question-answer]');
   buttons.forEach(button => { button.disabled = true; });
   status.textContent = verify ? 'Queueing verification…' : 'Reading the saved evidence…';
   let queued = false;
   try {
     const response = await fetch(form.action, {method:'POST',
       headers:{'Content-Type':'application/json', 'Accept':'application/json'},
       body:JSON.stringify(payload), signal:AbortSignal.timeout(verify ? 30000 : 70000)});
     const data = await response.json();
     if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request could not be completed.');
     if (verify) {
       queued = true;
       status.textContent = 'Verification queued. The polling worker will run it.';
       const link = document.createElement('a');
       link.href = form.dataset.resultsUrl; link.textContent = ' View progress';
       status.appendChild(link);
     } else { status.textContent = data.answer; }
   } catch (error) {
     status.textContent = error.message + ' Check the latest case state before retrying.';
   } finally {
     form.dataset.busy = queued ? 'true' : 'false';
     buttons.forEach(button => { button.disabled = queued; });
   }
 });
 document.addEventListener('toggle', event => {
   if (event.target.matches('details') && event.target.open && event.target.getClientRects().length) {
     document.dispatchEvent(new Event('symphony:reading'));
   }
 }, true);
})();
"""
