"""Safe, shared presentation for operator questions and saved documents."""
from __future__ import annotations

import hashlib
from html import escape
import json
import re


QUESTION_CONTRACT = '''Human clarification choices:
Whenever you ask a human to resolve a question in any phase, include exactly three
specific, distinct options in your needs_human JSON. Use this shape:
{"decision":"needs_human","question":"...","options":[{"label":"...","description":"..."},{"label":"...","description":"..."},{"label":"...","description":"..."}]}
Describe the consequence of each choice; put the recommended choice first and explain
why in its description. Keep options within the existing evidence and approved scope.
For grouped planning questions, also include questions as an array of strings and
make question summarize the group. Each option must explicitly resolve the whole
group; never let a choice silently answer unrelated decisions. Do not invent facts
to fill three choices: an option may defer a named unresolved decision with its
consequence. Keep any additional required phase fields.
The UI adds Other for free text. Do not include Other in the three options. An answer
is not plan approval; preserve the separate approval step. Retain any other fields
required by this phase (for example feedback_items in human-review triage).
'''


def question_view(payload: object, fallback: str = '') -> dict:
    if not isinstance(payload, dict):
        payload = {}
    question = str(payload.get('question') or payload.get('reason') or fallback).strip()
    options = payload.get('options')
    valid = isinstance(options, list) and len(options) == 3
    parsed = []
    if valid:
        for item in options:
            label = item if isinstance(item, str) else item.get('label') if isinstance(item, dict) else None
            description = item.get('description', '') if isinstance(item, dict) else ''
            if not isinstance(label, str) or not label.strip() or label.strip().casefold() == 'other' or len(label) > 240 or not isinstance(description, str) or len(description) > 2000:
                valid = False
                break
            parsed.append({'label': label.strip(), 'description': description.strip()})
        valid = valid and len({item['label'].casefold() for item in parsed}) == 3
    if not valid:
        parsed = [
            {'label': 'Explain the options', 'description': 'Describe the available choices and recommend one before I decide.'},
            {'label': 'Show the relevant evidence', 'description': 'Show the requirements or existing code that this decision depends on.'},
            {'label': 'Ask a narrower question', 'description': 'Break this down to the smallest decision needed from me.'},
        ]
    return {'question': question, 'options': parsed, 'suggested': bool(valid) and payload.get('suggested') is not False}


def render_question(question: dict) -> str:
    view = question_view(question, 'What should Symphony do next?')
    key = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()[:16]
    hint = ('Choose one reply, or use Other to answer in your own words. Nothing is submitted until you confirm.'
            if view['suggested'] else 'Specific choices are unavailable for this question. Ask for clarification below, or choose Other to give your decision.')
    options = ''.join(
        '<label class="answer-option"><input type="radio" name="choice" required '
        f'value="{escape(item["label"] + (": " + item["description"] if item["description"] else ""), quote=True)}">'
        f'<span><strong>{escape(item["label"])}</strong><small>{escape(item["description"])}</small></span></label>'
        for item in view['options'])
    return (f'<fieldset class="human-question" data-question-key="{key}"><legend>{escape(view["question"])}</legend>'
            f'<p class="question-hint">{escape(hint)}</p>'
            + options + '<label class="answer-option"><input type="radio" name="choice" required value="__other__">'
            '<span><strong>Other</strong><small>Write your own answer or combine ideas.</small></span></label>'
            '<label data-other-answer>Your answer<textarea name="response" maxlength="16000" rows="4" '
            'placeholder="Tell Symphony how you want to proceed"></textarea></label></fieldset>')


QUESTION_SCRIPT = r'''
(() => {
 window.syncQuestionChoices = form => {
  const box = form.querySelector('[data-question-key]'); if (!box) return;
  const answer = form.elements.response;
  const radios = [...box.querySelectorAll('input[name="choice"]')];
  const exact = radios.find(r => r.value === answer.value && r.value !== '__other__');
  if (answer.value) (exact || radios.find(r => r.value === '__other__')).checked = true;
  const selected = radios.find(r => r.checked);
  box.querySelector('[data-other-answer]').hidden = !selected || selected.value !== '__other__';
  answer.disabled = !selected;
  answer.required = !!selected && selected.value === '__other__';
  answer.readOnly = !!selected && selected.value !== '__other__';
 };
 window.initQuestionChoices = root => {
  root.querySelectorAll('[data-question-key]').forEach(box => {
   if (box.dataset.ready) return; box.dataset.ready = 'true';
   const form = box.closest('form'), answer = form.elements.response;
   const key = 'symphony.answer:' + form.action + ':' + box.dataset.questionKey;
   const persistent = form.id !== 'review-clarification';
   if (persistent) { try { answer.value = sessionStorage.getItem(key) || answer.value; } catch {} }
   window.syncQuestionChoices(form);
   box.addEventListener('change', event => {
    if (event.target.name !== 'choice') return;
    if (event.target.value === '__other__') {
     answer.value = box.dataset.customAnswer || ''; answer.readOnly = false; answer.disabled = false; answer.required = true;
     box.querySelector('[data-other-answer]').hidden = false; answer.focus();
    } else {
     if (!answer.readOnly) box.dataset.customAnswer = answer.value;
     answer.value = event.target.value;
     window.syncQuestionChoices(form);
    }
    answer.dispatchEvent(new Event('input', {bubbles:true}));
   });
   answer.addEventListener('input', () => {
    if (!answer.readOnly) box.dataset.customAnswer = answer.value;
    if (persistent) { try { sessionStorage.setItem(key, answer.value); } catch {} }
   });
  });
 };
 window.initQuestionChoices(document);
})();
'''


PRESENTATION_CSS = '''
.human-question{border:0;padding:0;margin:0;min-width:0;width:100%}
.human-question legend{font-size:1.05rem;font-weight:700;line-height:1.5;white-space:pre-wrap}
.question-hint{color:#64748b;font-size:.85rem}
.answer-option{display:flex!important;gap:.75rem;align-items:flex-start;border:1px solid #cbd5e1;border-radius:10px;padding:.8rem;margin:.55rem 0;background:#fff;cursor:pointer}
.answer-option:has(input:checked){border-color:#2563eb;background:#eff6ff;box-shadow:0 0 0 1px #2563eb}
.answer-option:focus-within{outline:2px solid #2563eb;outline-offset:2px}
.answer-option input{width:auto!important;margin:.25rem 0 0!important;flex:0 0 auto}
.answer-option small{display:block;font-weight:400;color:#475569;font-size:.85rem;line-height:1.5;margin-top:.2rem}
[data-other-answer] textarea{display:block;font-weight:400;box-sizing:border-box;width:100%;font:inherit;padding:.75rem;border:1px solid #94a3b8;border-radius:8px}
[hidden]{display:none!important}
.readable-document{font-size:.94rem;line-height:1.7;overflow-wrap:anywhere;max-width:80ch}
.readable-document section{margin:1rem 0;padding:.7rem 0;border:0;border-bottom:1px solid #e2e8f0;border-radius:0}
.readable-document h3,.readable-document h4{margin:.3rem 0;color:#334155}
.readable-document ul,.readable-document ol{padding-left:1.4rem}
.readable-document li{margin:.45rem 0}
.readable-document p{margin:.55rem 0}
.readable-document pre,.raw-document pre{font-family:ui-monospace,monospace;font-size:.8rem;white-space:pre-wrap;overflow-wrap:anywhere;background:#f8fafc;padding:1rem;border-radius:8px}
.raw-document{margin-top:1rem;color:#64748b;font-size:.8rem}
.document-page{max-width:920px;margin:2rem auto;padding:0 1.25rem;font-family:system-ui,sans-serif;color:#18212f;background:#fff}
.document-contents{display:flex;flex-wrap:wrap;gap:.5rem;margin:1rem 0 2rem}.document-contents a{padding:.25rem .65rem;border:1px solid #dbe3ee;border-radius:20px;text-decoration:none;font-size:.8rem}section[id]{scroll-margin-top:1rem}
.document-page nav{display:flex;gap:1.2rem;flex-wrap:wrap}.document-page a{color:#1d4ed8}
'''


def _inline(text: str) -> str:
    text = escape(text)
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)
    return re.sub(r'\*\*([^*\n]+)\*\*', r'<strong>\1</strong>', text)


def _structured(value, depth=0, anchors=False):
    if depth > 8:
        return '<pre>' + escape(json.dumps(value, indent=2)) + '</pre>'
    if isinstance(value, dict) and depth > 0:
        parts, references = [], {}
        for key, item in value.items():
            if item in (None, '', [], {}):
                continue
            if key in ('id', 'jira_sources', 'source_id', 'schema_version') or key.endswith(('_hash', '_sha')):
                references[key] = item
                continue
            label = escape(str(key).replace('_', ' ').capitalize())
            if isinstance(item, (dict, list)):
                parts.append('<div><h4>' + label + '</h4>' + _structured(item, depth+1) + '</div>')
            else:
                parts.append('<p><strong>' + label + ':</strong> ' + _inline(str(item)).replace('\n', '<br>') + '</p>')
        if references:
            parts.append('<details class="raw-document"><summary>References</summary>' +
                         ''.join('<div><strong>' + escape(k.replace('_', ' ').capitalize()) + ':</strong> ' +
                                 (_structured(v, depth+1) if isinstance(v, (dict,list)) else _inline(str(v))) + '</div>'
                                 for k,v in references.items()) + '</details>')
        return ''.join(parts)
    if isinstance(value, dict):
        value = {key: (item.replace('_', ' ').capitalize() if key in ('decision', 'status') and isinstance(item,str) else item) for key,item in value.items()}
        return ''.join((f'<section id="section-{index}">' if anchors else '<section>') + '<h4>' + escape(str(key).replace('_', ' ').capitalize()) + '</h4>' + _structured(item, depth+1) + '</section>'
                       for index, (key, item) in enumerate(value.items()) if item not in (None, '', [], {}))
    if isinstance(value, list):
        return '<ul>' + ''.join('<li>' + _structured(item, depth+1) + '</li>' for item in value) + '</ul>'
    if isinstance(value, bool):
        return '<p>' + ('Yes' if value else 'No') + '</p>'
    return '<p>' + _inline(str(value)).replace('\n', '<br>') + '</p>'


def render_document(content: str, *, raw=True, contents=False) -> str:
    content = str(content or '')
    try:
        parsed = json.loads(content)
    except ValueError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        technical = {}
        if isinstance(parsed, dict):
            technical = {k:v for k,v in parsed.items() if k.endswith(('_hash', '_sha')) or k in ('schema_version', 'baseline_repository_shas')}
            parsed = {k:v for k,v in parsed.items() if k not in technical}
        navigation = ''
        if contents and isinstance(parsed, dict):
            navigation = '<nav class="document-contents" aria-label="On this page">' + ''.join(
                f'<a href="#section-{index}">{escape(key.replace("_", " ").capitalize())}</a>'
                for index, (key, item) in enumerate(parsed.items()) if item not in (None, '', [], {})) + '</nav>'
        body = navigation + _structured(parsed, anchors=contents)
        if technical:
            body += '<details><summary>Technical references</summary>' + _structured(technical) + '</details>'
    else:
        lines = content.splitlines(); parts = []; code = None; list_kind = None
        for line in lines + ['']:
            if line.startswith('```'):
                if code is None: code = []
                else: parts.append('<pre><code>'+escape('\n'.join(code))+'</code></pre>'); code = None
                continue
            if code is not None: code.append(line); continue
            match = re.match(r'^\s*(?:[-*]|\d+\.)\s+(.+)', line)
            kind = 'ol' if match and re.match(r'^\s*\d+\.',line) else 'ul' if match else None
            if kind != list_kind:
                if list_kind: parts.append(f'</{list_kind}>')
                if kind: parts.append(f'<{kind}>')
                list_kind = kind
            if match: parts.append('<li>'+_inline(match[1])+'</li>')
            elif line.startswith('#'): parts.append('<h3>'+_inline(line.lstrip('#').strip())+'</h3>')
            elif line.strip(): parts.append('<p>'+_inline(line)+'</p>')
        if code is not None: parts.append('<pre><code>'+escape('\n'.join(code))+'</code></pre>')
        body = ''.join(parts)
    original = ('<details class="raw-document"><summary>View original text</summary><pre>'+escape(content)+'</pre></details>') if raw else ''
    return '<div class="readable-document">' + body + '</div>' + original


def document_page(title: str, content: str, raw_url: str = '') -> str:
    link = f'<a href="{escape(raw_url, quote=True)}">Original JSON</a>' if raw_url else ''
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{escape(title)} · Symphony</title><style>{PRESENTATION_CSS}</style>'
            f'<body class="document-page"><nav><a href="/">← All cases</a>{link}</nav><h1>{escape(title)}</h1>'
            + render_document(content, contents=True) + '</body></html>')
