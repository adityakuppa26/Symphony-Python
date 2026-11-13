"""Manual review progress and response, independent of the comment source."""
from __future__ import annotations

from html import escape
from .human_review import _parse_json_object
from .presentation import PRESENTATION_CSS, QUESTION_SCRIPT, question_view, render_document, render_question


def feedback_items(action: dict) -> list[dict]:
    output = action.get("triage_output")
    payload = _parse_json_object(output) if isinstance(output, str) else {}
    items = payload.get("feedback_items", []) if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return []
    return [{key: str(item.get(key) or "") for key in ("id", "comment", "scope", "proposed_action")}
            for item in items if isinstance(item, dict)]


def review_progress(action: dict, run: dict) -> dict:
    status = run["status"]
    decision = action.get("triage_decision")
    labels = {
        "queued": "Waiting for a worker",
        "completed": "Corrections reviewed · ready for your review",
        "blocked": "Your input is needed",
        "failed": "Review continuation failed",
        "cancelled": "Review continuation stopped",
    }
    label = labels.get(status, "Checking review scope" if not decision else "Implementing, verifying, and reviewing")
    items = feedback_items(action)
    evidence = action.get("resolution") or {}
    resolutions = {item["id"]: item for item in evidence.get("items", [])} if status == "completed" else {}
    for item in items:
        resolution = resolutions.get(item["id"], {})
        item["resolution"] = resolution.get("status", "not_recorded" if status == "completed" else "pending")
        item["evidence"] = resolution.get("evidence", "")
    output = action.get("triage_output")
    triage = _parse_json_object(output) if isinstance(output, str) else {}
    saved_question = question_view(run["human_question"]) if run.get("human_question") else {}
    question = saved_question if saved_question.get("suggested") else question_view(triage, run.get("error") or "What should Symphony clarify?")
    return {
        "question_html": render_question(question),
        "report_html": render_document(run.get("final_message") or "The response will appear here after Symphony works on this review."),
        "status": status, "label": label, "decision": decision,
        "items": items, "error": run.get("error") or "",
        "comment_coverage": "recorded" if status == "completed" and resolutions else "not_recorded",
        "report": run.get("final_message") or "",
        "verification": run.get("verification_status") or "Not run yet",
        "result_run_id": run["id"],
        "can_clarify": status == "blocked" and decision == "needs_human" and run.get("blocked_phase") == "review"
            and run.get("human_input_actionable", True),
        "clarifications": action.get("clarifications", []),
        "clarification_revision": len(action.get("clarifications", [])),
    }


REVIEW_PROGRESS_SCRIPT = r"""
(() => {
  const id = document.body.dataset.reviewId;
  let polling = false;
  let lastReportHtml = null;
  const form = document.getElementById('review-clarification');
  let answer = form.elements.response;
  const questionHost = document.getElementById("review-question");
  let latestQuestionHtml = questionHost ? questionHost.innerHTML : "";
  let latestRevision = Number(form.elements.expected_revision.value);
  const draftKey = 'symphony.review-answer:' + id;
  try {
    const draft = JSON.parse(sessionStorage.getItem(draftKey) || 'null');
    if (draft && typeof draft.response === 'string' && Number.isInteger(draft.revision)) {
      answer.value = draft.response; form.elements.expected_revision.value = draft.revision;
    }
  } catch {}
  function bindAnswerDraft() { answer.addEventListener('input', () => {
    try { sessionStorage.setItem(draftKey, JSON.stringify({response:answer.value, revision:Number(form.elements.expected_revision.value)})); } catch {}
  }); }
  bindAnswerDraft();
  if (window.syncQuestionChoices) window.syncQuestionChoices(form);
  let currentQuestionKey = form.querySelector('[data-question-key]')?.dataset.questionKey;
  function applyQuestion(html) {
    if (!questionHost || !html) return;
    const key = html.match(/data-question-key="([^"]+)"/)?.[1];
    if (key && key === currentQuestionKey) return;
    currentQuestionKey = key;
    questionHost.innerHTML = html; answer = form.elements.response; bindAnswerDraft();
    if (window.initQuestionChoices) window.initQuestionChoices(form);
  }
  let submitting = false;
  function resetAnswer() {
    answer.value = '';
    form.querySelectorAll('input[name=choice]').forEach(input => { input.checked = false; });
    const box = form.querySelector('[data-question-key]');
    if (box) delete box.dataset.customAnswer;
    if (window.syncQuestionChoices) window.syncQuestionChoices(form);
  }
  document.getElementById('clear-answer').addEventListener('click', () => {
    if (submitting) return;
    answer.value = ''; applyQuestion(latestQuestionHtml);
    resetAnswer();
    form.elements.expected_revision.value = latestRevision;
    try { sessionStorage.removeItem(draftKey); } catch {}
    document.getElementById('clarification-status').textContent = 'Draft cleared. You can answer the current question.';
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    const button = form.querySelector('button');
    const message = document.getElementById('clarification-status');
    button.disabled = true; answer.readOnly = true;
    form.querySelectorAll('input[name=choice]').forEach(input => { input.disabled = true; });
    try {
      const response = await fetch('/api/v1/reviews/' + encodeURIComponent(id) + '/clarifications', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({response:answer.value, expected_revision:Number(form.elements.expected_revision.value)}), signal:AbortSignal.timeout(15000)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Answer could not be saved');
      try { sessionStorage.removeItem(draftKey); } catch {}
      resetAnswer(); message.textContent = 'Answer saved. This review is queued to resume.';
      form.hidden = true; await refresh();
    } catch (error) { message.textContent = error.message + ' Your answer is still here; you can retry.'; }
    finally { submitting = false; button.disabled = false; answer.readOnly = false;
      form.querySelectorAll('input[name=choice]').forEach(input => { input.disabled = false; });
      if (window.syncQuestionChoices) window.syncQuestionChoices(form);
    }
  });
  async function refresh() {
    if (polling || document.hidden) return;
    polling = true;
    try {
      const response = await fetch('/api/v1/reviews/' + encodeURIComponent(id), {
        cache:'no-store', signal:AbortSignal.timeout(10000)
      });
      if (!response.ok) throw new Error('Progress unavailable');
      const data = await response.json();
      document.getElementById('review-status').textContent = data.label;
      document.getElementById('review-verification').textContent = data.verification;
      document.getElementById('review-error').textContent = data.error;
      document.getElementById('review-error').hidden = !data.error || data.can_clarify;
      if (!submitting) form.hidden = !data.can_clarify;
      latestRevision = data.clarification_revision;
      if (data.question_html && questionHost) {
        latestQuestionHtml = data.question_html;
        if (!submitting && !answer.value) applyQuestion(latestQuestionHtml);
        else if (Number(form.elements.expected_revision.value) !== latestRevision) {
          document.getElementById('clarification-status').textContent = 'A new question is available. Your draft is preserved; clear it to answer the new question.';
        }
      }
      if (!answer.value) form.elements.expected_revision.value = data.clarification_revision;
      const history = document.getElementById('review-answers');
      const answers = data.clarifications.map(item => `${item.question || 'Scope question'}\nYour answer: ${item.text}`).join('\n\n');
      if (history.textContent !== answers) history.textContent = answers;
      const report = document.getElementById('review-report');
      const text = data.report || 'The response will appear here after Symphony works on this review.';
      if (data.report_html) { if (lastReportHtml !== data.report_html) { report.innerHTML = data.report_html; lastReportHtml = data.report_html; } }
      else if (report.textContent !== text) report.textContent = text;
      const list = document.getElementById('review-items');
      const signature = JSON.stringify(data.items);
      if (list.dataset.items !== signature) {
        const rows = data.items.map(item => {
          const row = document.createElement('li');
          row.textContent = `${item.id}: ${item.comment} — ${item.scope.replaceAll('_',' ')}. ${item.proposed_action}` +
            (item.resolution ? ` Resolution: ${item.resolution.replaceAll('_',' ')}. ${item.evidence || ''}` : '');
          return row;
        });
        list.replaceChildren(...rows); list.dataset.items = signature;
      }
      document.getElementById('review-sync').textContent = 'Updated at ' + new Date().toLocaleTimeString();
    } catch {
      document.getElementById('review-sync').textContent = 'Connection interrupted. Reconnecting automatically; your submitted review is saved.';
    } finally { polling = false; }
  }
  refresh(); setInterval(refresh, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();
"""


def render_review_page(action: dict, run: dict) -> str:
    state = review_progress(action, run)
    items = "".join(
        f'<li><strong>{escape(item["id"])}</strong>: {escape(item["comment"])} '
        f'— {escape(item["scope"].replace("_", " "))}. {escape(item["proposed_action"])} '
        f'<strong>{escape(item["resolution"].replace("_", " "))}</strong>: {escape(item["evidence"])}</li>'
        for item in state["items"]
    )
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review progress · Symphony</title><style>
body{font-family:system-ui,sans-serif;background:#f4f7fb;color:#18212f;line-height:1.6;margin:0}
main{max-width:960px;margin:2rem auto;padding:0 1rem}h1{margin-bottom:.3rem}
section{background:white;border:1px solid #dfe6ef;border-radius:14px;padding:1.4rem;margin:1rem 0}
a{color:#1d4ed8}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-family:inherit;max-height:36rem;overflow:auto}
.muted{color:#64748b}#review-error{padding:1rem;background:#fff1f2;border-radius:8px}
li{margin:.75rem 0}nav{display:flex;gap:1rem;flex-wrap:wrap}h2{font-size:1.15rem}
""" + PRESENTATION_CSS + "</style></head>" + f"""
<body data-review-id="{escape(action['id'])}"><main>
<nav><a href="/">All cases</a><a href="/#run-{escape(run['id'])}">Open case · decisions &amp; next review</a></nav>
<h1>{escape(action['issue_identifier'])} · Human review</h1>
<p class="muted">Review by {escape(action['reviewer_identity'])} · {escape(action['created_at'])}</p>
<section aria-label="Review progress"><h2 id="review-status" role="status">{escape(state['label'])}</h2>
<p>Scope check → Corrections → Focused verification → Independent review → Your review</p>
<p>Verification: <strong id="review-verification">{escape(state['verification'])}</strong> (advisory)</p>
<p id="review-error"{'' if state['error'] and not state['can_clarify'] else ' hidden'}>{escape(state['error'])}</p>
<form id="review-clarification" method="post" action="/api/v1/reviews/{escape(action['id'])}/clarifications"{'' if state['can_clarify'] else ' hidden'}>
<input type="hidden" name="expected_revision" value="{state['clarification_revision']}">
<div id="review-question">{state["question_html"]}</div>
<button type="submit">Save answer &amp; resume review</button> <button type="button" id="clear-answer">Clear answer draft</button><p id="clarification-status" role="status"></p></form>
<p>If input or a revised plan is needed, open the case above. After completion, you can submit another review round there.</p>
<p class="muted" id="review-sync">Progress updates automatically without reloading this page.</p></section>
<section><h2>What Symphony understood</h2><p class="muted">The scope check records each actionable comment here. This is the proposed work, not a claim that it is complete.</p>
<ol id="review-items">{items}</ol></section>
<section><details><summary>Scope questions &amp; your answers</summary><pre id="review-answers">{escape(chr(10).join(str(item.get('question') or '') + chr(10) + 'Your answer: ' + str(item.get('text') or '') for item in action.get('clarifications', [])))}</pre></details></section>
<section><h2>Response to your review</h2><div id="review-report">{state["report_html"]}</div></section>
<section><details><summary>Original feedback · preserved exactly as submitted</summary><pre>{escape(action['comments'])}</pre>
<p class="muted">Reference: {escape(action.get('source_url') or 'Manual feedback')}</p></details></section>
</main><script>{QUESTION_SCRIPT}
{REVIEW_PROGRESS_SCRIPT}</script></body></html>"""
