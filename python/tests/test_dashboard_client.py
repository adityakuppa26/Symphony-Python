"""Exercise browser behavior without requiring a browser or audio hardware."""
import shutil
import subprocess
import unittest

from symphony_jira.dashboard_client import DASHBOARD_SCRIPT


@unittest.skipUnless(shutil.which("node"), "Node is required for dashboard client checks")
class DashboardClientTests(unittest.TestCase):
    def test_notifications_filters_and_refresh_preserve_input(self):
        harness = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
const fs = require('node:fs');
const script = fs.readFileSync(0, 'utf8');
const elements = new Map(), listeners = {}, intervals = [], storage = new Map();
let tones = 0, notifications = 0, reloads = 0, offline = false;
let state = {blocked_issues: [{id:'one', issue_identifier:'T-1', human_input_actionable:true}]};
function element(id) {
  if (!elements.has(id)) elements.set(id, {value: id === 'case-filter' ? 'all' : '',
    textContent:'', handlers:{}, classList:{toggle(){}},
    addEventListener(name, callback) {this.handlers[name] = callback;}, setAttribute(){}});
  return elements.get(id);
}
const rows = [
  {dataset:{case:'t-1', filter:'attention'}},
  {dataset:{case:'t-2', filter:'completed'}}
];
class AudioContext {
  state = 'running'; currentTime = 0; destination = {};
  async resume() {}
  createOscillator() { return {frequency:{}, connect(){}, disconnect(){}, start(){tones++;}, stop(){}}; }
  createGain() {return {gain:{setValueAtTime(){}, linearRampToValueAtTime(){}, exponentialRampToValueAtTime(){}}, connect(){}, disconnect(){}};}
}
class Notification {
  static permission = 'default';
  static async requestPermission() {this.permission = 'granted'; return 'granted';}
  constructor() {notifications++;}
}
const document = {getElementById:element, querySelectorAll:()=>rows, hidden:false,
  addEventListener(name, callback){listeners[name] = callback;}};
vm.runInNewContext(script, {document, window:{AudioContext, Notification, location:{reload(){reloads++;}}, focus(){}},
  Notification, AbortSignal, localStorage:{getItem:key=>storage.get(key) ?? null, setItem:(key,value)=>storage.set(key,value)},
  fetch:async()=>{if(offline) throw Error('offline'); return {ok:true, json:async()=>state};},
  setInterval(callback, delay){intervals.push({callback, delay});}, console});
const settle = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await settle();
  assert.equal(tones, 0); assert.equal(notifications, 0);
  assert.match(element('attention-summary').textContent, /T-1/);
  await element('sound-toggle').handlers.click(); await settle();
  assert.equal(tones, 4); // Preview plus first actionable request, two tones each.
  const poll = intervals.find(item=>item.delay===15000).callback;
  await poll(); assert.equal(tones, 4); // No repeat on every poll.
  state.blocked_issues.push({id:'old', issue_identifier:'T-0', human_input_actionable:false});
  await poll(); assert.equal(tones, 4);
  state.blocked_issues.push({id:'two', issue_identifier:'T-2', human_input_actionable:true});
  await poll(); assert.equal(tones, 6);
  await element('sound-toggle').handlers.click();
  await element('desktop-toggle').handlers.click(); await settle();
  state.blocked_issues.push({id:'three', issue_identifier:'T-3', human_input_actionable:true});
  await poll(); assert.equal(notifications, 1); assert.equal(tones, 6);
  await poll(); assert.equal(notifications, 1);
  await element('desktop-toggle').handlers.click();
  state.blocked_issues.push({id:'four', issue_identifier:'T-4', human_input_actionable:true});
  await poll(); assert.equal(notifications, 1);
  element('case-filter').value='completed'; element('case-filter').handlers.change();
  assert.equal(rows[0].hidden,true); assert.equal(rows[1].hidden,false);
  element('case-search').value='T-1'; element('case-search').handlers.input();
  assert.equal(rows[1].hidden,true);
  intervals.find(item=>item.delay===60000).callback(); assert.equal(reloads,0);
  offline=true; await poll(); assert.match(element('alert-status').textContent,/Cannot check/);
  offline=false; state.blocked_issues=[]; await poll();
  assert.equal(document.title,'Symphony Jira');
})().catch(error=>{console.error(error); process.exitCode=1;});
'''
        result = subprocess.run(
            ["node", "-e", harness], input=DASHBOARD_SCRIPT, text=True,
            capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
