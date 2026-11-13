import shutil
import subprocess

import pytest

from symphony_jira.dashboard_client import DASHBOARD_SCRIPT
from symphony_jira.review_page import REVIEW_PROGRESS_SCRIPT


@pytest.mark.skipif(not shutil.which('node'), reason='Node is required for browser behavior checks')
def test_review_drafts_preview_retries_and_duplicate_clicks():
    harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict'), fs=require('node:fs');
const script=fs.readFileSync(0,'utf8'), saved=new Map();
let posts=0, fail=true;
const storage={getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,value),removeItem:key=>saved.delete(key)};
function element(){return {value:'',textContent:'',hidden:true,disabled:false,handlers:{},dataset:{},
 addEventListener(name,fn){this.handlers[name]=fn},setAttribute(){},classList:{toggle(){}}};}
function boot(){
 const toolbar=new Map(), parts=new Map(), details={open:false};
 const form=element();form.action='/api/v1/runs/run-1/human-review';
 form.elements={reviewer_identity:element(),source_url:element(),comments:element()};
 form.elements.reviewer_identity.value='Operator';
 form.getAttribute=()=>form.action;form.closest=()=>details;
 form.querySelector=key=>{if(!parts.has(key)) parts.set(key,element());return parts.get(key)};
 const document={hidden:false,addEventListener(){},
 getElementById:key=>{if(!toolbar.has(key)) toolbar.set(key,element());return toolbar.get(key)},
 querySelectorAll:key=>key==='[data-review-form]'?[form]:[]};
 vm.runInNewContext(script,{document,window:{location:{reload(){throw Error('Unexpected reload')}}},
 localStorage:storage,sessionStorage:storage,AbortSignal,setInterval(){},
 fetch:async(url,options)=>{
  if(options?.method==='POST'){
   posts++;await new Promise(resolve=>setImmediate(resolve));
   if(fail)throw Error('offline');
   assert.equal(JSON.parse(options.body).comments,'<script>literal feedback</script>');
   return {ok:true,json:async()=>({human_review:{id:'review-1'},replayed:true})};
  }
  return {ok:true,json:async()=>({blocked_issues:[],all_runs:[]})};
 }});
 return {form,parts,details};
}
(async()=>{
 let app=boot();app.form.elements.comments.value='<script>literal feedback</script>';
 app.form.handlers.input();
 assert.equal(app.parts.get('[data-review-preview]').textContent,'<script>literal feedback</script>');
 assert.equal(saved.has('symphony.review-draft:/api/v1/runs/run-1/human-review'),true);
 app=boot();assert.equal(app.details.open,true);
 assert.equal(app.form.elements.comments.value,'<script>literal feedback</script>');
 const event={preventDefault(){}};
 await Promise.all([app.form.handlers.submit(event),app.form.handlers.submit(event)]);
 assert.equal(posts,1);assert.equal(app.parts.get('button[type="submit"]').disabled,false);
 assert.match(app.parts.get('[data-review-status]').textContent,/offline/);
 assert.equal(app.form.elements.comments.value,'<script>literal feedback</script>');
 assert.equal(saved.has('symphony.review-draft:/api/v1/runs/run-1/human-review'),true);
 fail=false;await app.form.handlers.submit(event);
 assert.equal(posts,2);assert.equal(saved.has('symphony.review-draft:/api/v1/runs/run-1/human-review'),false);
 assert.equal(app.parts.get('[data-review-result]').href,'/reviews/review-1');
 assert.equal(app.parts.get('[data-review-result]').hidden,false);
 assert.equal(app.form.elements.comments.readOnly,true);
 await app.form.handlers.submit(event);assert.equal(posts,2);
 const next=boot();next.form.elements.comments.value='Discard';next.form.handlers.input();
 next.parts.get('[data-review-clear]').handlers.click();
 assert.equal(next.form.elements.comments.value,'');
 assert.equal(saved.has('symphony.review-draft:/api/v1/runs/run-1/human-review'),false);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', harness], input=DASHBOARD_SCRIPT, text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not shutil.which('node'), reason='Node is required for browser behavior checks')
def test_review_progress_answers_preserve_drafts_and_question_revision():
    harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict'),fs=require('node:fs');
const elements=new Map(),storage=new Map(),intervals=[];
function el(id){if(!elements.has(id))elements.set(id,{value:'',textContent:'',hidden:false,dataset:{},handlers:{},
 addEventListener(name,fn){this.handlers[name]=fn},replaceChildren(...rows){this.rows=rows}});return elements.get(id)}
const form=el('review-clarification'),answer=el('answer'),revision=el('revision'),button=el('button');
revision.value='0';form.elements={response:answer,expected_revision:revision};form.querySelector=()=>button;form.querySelectorAll=()=>[];
let fail=true,posts=0;
let state={label:'Your input is needed',verification:'Not run yet',error:'Which helper?',can_clarify:true,
 clarification_revision:0,clarifications:[],items:[{id:'R1',comment:'<script>quoted</script>',scope:'clarification',proposed_action:'Confirm helper'}],report:''};
const context={window:{},document:{body:{dataset:{reviewId:'review-1'}},hidden:false,getElementById:el,createElement:()=>({}),addEventListener(){}},
 sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
 AbortSignal,setInterval(fn){intervals.push(fn)},fetch:async(url,options)=>{
  if(options?.method==='POST'){
   posts++;const payload=JSON.parse(options.body);assert.equal(payload.expected_revision,0);
   if(fail)throw Error('offline');
   state={...state,label:'Waiting for a worker',can_clarify:false,clarification_revision:1,
    clarifications:[{question:'Which helper?',text:payload.response}]};
   return {ok:true,json:async()=>({status:'queued'})};
  }
  return {ok:true,json:async()=>state};
 }};
vm.runInNewContext(fs.readFileSync(0,'utf8'),context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 await settle();assert.equal(el('review-items').rows[0].textContent.includes('<script>quoted</script>'),true);
 answer.value='Use the existing helper';answer.handlers.input();
 assert.equal(storage.size,1);
 // Another tab answered first: retain the revision of this draft instead of applying it to a new question.
 state.clarification_revision=1;await intervals[0]();assert.equal(Number(revision.value),0);
 await form.handlers.submit({preventDefault(){}});
 assert.match(el('clarification-status').textContent,/offline/);assert.equal(answer.value,'Use the existing helper');
 fail=false;await form.handlers.submit({preventDefault(){}});
 assert.equal(storage.size,0);assert.equal(answer.value,'');assert.equal(form.hidden,true);
 assert.equal(el('review-status').textContent,'Waiting for a worker');
 assert.match(el('review-answers').textContent,/Use the existing helper/);
 answer.value='Old draft';answer.handlers.input();el('clear-answer').handlers.click();
 assert.equal(answer.value,'');assert.equal(Number(revision.value),1);assert.equal(storage.size,0);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', harness], input=REVIEW_PROGRESS_SCRIPT, text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('hidden', [False, True])
@pytest.mark.skipif(not shutil.which('node'), reason='Node is required for browser behavior checks')
def test_successful_clarification_resets_repeated_question(hidden):
    from symphony_jira.presentation import QUESTION_SCRIPT

    harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict'),fs=require('node:fs');
const elements=new Map();
function el(id){if(!elements.has(id))elements.set(id,{value:'',textContent:'',dataset:{},handlers:{},
 addEventListener(name,fn){this.handlers[name]=fn},replaceChildren(){}});return elements.get(id)}
const form=el('review-clarification'),answer=el('answer'),revision=el('revision'),box=el('box');
const radios=[{value:'A'},{value:'B'},{value:'C'},{value:'__other__'}];
box.dataset={questionKey:'same',customAnswer:'previous custom draft'};
box.closest=()=>form;box.querySelectorAll=()=>radios;box.querySelector=()=>el('other');
form.id='review-clarification';form.elements={response:answer,expected_revision:revision};revision.value='0';
form.querySelector=key=>key==='[data-question-key]'?box:el('button');
form.querySelectorAll=key=>key==='[data-question-key]'?[box]:radios;
el('review-question').innerHTML='<div data-question-key="same"></div>';
const intervals=[],posts=[];
const document={hidden:false,body:{dataset:{reviewId:'review'}},getElementById:el,
 querySelectorAll:()=>[box],addEventListener(){}};
const state={label:'Question',verification:'',error:'',can_clarify:true,clarification_revision:0,
 question_html:el('review-question').innerHTML,clarifications:[],items:[],report:''};
vm.runInNewContext(fs.readFileSync(0,'utf8'),{window:{},document,AbortSignal,
 sessionStorage:{getItem(){return null},removeItem(){}},setInterval(fn){intervals.push(fn)},
 fetch:async(url,options)=>{
  if(options?.method==='POST'){posts.push(JSON.parse(options.body));state.clarification_revision++}
  return {ok:true,json:async()=>state};
 }});
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 answer.value='A';radios[0].checked=true;answer.readOnly=true;
 document.hidden=HIDDEN;
 await form.handlers.submit({preventDefault(){}});
 assert.equal(answer.value,'');assert.equal(radios.some(r=>r.checked),false);
 assert.equal(box.dataset.customAnswer,undefined);assert.equal(answer.readOnly,false);
 document.hidden=false;await intervals[0]();
 assert.equal(Number(revision.value),1);
 // Selecting the repeated option must submit its value for the new revision.
 answer.value='A';radios[0].checked=true;
 await form.handlers.submit({preventDefault(){}});
 assert.deepEqual(posts,[{response:'A',expected_revision:0},{response:'A',expected_revision:1}]);
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('HIDDEN', 'true' if hidden else 'false')
    result = subprocess.run(['node', '-e', harness], input=QUESTION_SCRIPT + REVIEW_PROGRESS_SCRIPT,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('fenced', [False, True])
def test_review_progress_preserves_triage_choices_without_saved_question(fenced):
    import json
    from symphony_jira.review_page import review_progress

    payload = json.dumps({'question': 'Which helper?', 'options': ['Reuse', 'Extend', 'Defer'],
                          'feedback_items': [{'id': 'R1', 'comment': 'Fix helper'}]})
    if fenced:
        payload = '```json\n' + payload + '\n```'
    state = review_progress({'triage_output': payload, 'triage_decision': 'needs_human'},
                            {'id': 'run', 'status': 'blocked', 'blocked_phase': 'review'})
    for choice in ('Reuse', 'Extend', 'Defer'):
        assert choice in state['question_html']
    assert 'Specific choices are unavailable' not in state['question_html']
    assert state['items'][0]['comment'] == 'Fix helper'
