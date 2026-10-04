"""Static extension contract checks; installed-browser acceptance is separate."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser-extension"


class ExtensionContractTests(unittest.TestCase):
    def test_manifest_has_scoped_dom_and_loopback_permissions(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(set(manifest["permissions"]), {"storage", "alarms", "clipboardWrite"})
        self.assertEqual(manifest["host_permissions"], ["http://127.0.0.1/*"])
        self.assertEqual(manifest["content_scripts"][0]["matches"], ["https://chatgpt.com/*"])
        self.assertNotIn("externally_connectable", manifest)

    def test_capture_uses_dom_not_private_api(self):
        script = (EXTENSION / "content.js").read_text(encoding="utf-8")
        self.assertIn("MutationObserver", script)
        self.assertIn("data-message-author-role", script)
        self.assertIn("data-message-id", script)
        self.assertIn("incomplete_coverage: true", script)
        self.assertIn("if (!id)", script)
        for forbidden in ("document.cookie", "chrome.cookies", "/backend-api/", "fetch("):
            self.assertNotIn(forbidden, script)

    def test_worker_uses_durable_bounded_queue_and_bearer(self):
        script = (EXTENSION / "worker.js").read_text(encoding="utf-8")
        self.assertIn("MAX_QUEUE_ITEMS = 100", script)
        self.assertIn("MAX_QUEUE_BYTES = 6 * 1024 * 1024", script)
        self.assertIn("MAX_PAYLOAD_BYTES = 4 * 1024 * 1024", script)
        self.assertIn("127.0.0.1:${port}/api/${route}", script)
        self.assertIn('"Authorization": `Bearer ${settings.token}`', script)
        self.assertIn('credentials: "omit"', script)
        self.assertIn("chrome.storage.local.set({queue", script)
        self.assertNotIn("chrome.cookies", script)

    @unittest.skipUnless(shutil.which("node"), "Node.js not installed; browser syntax check required")
    def test_javascript_syntax(self):
        for script in ("content.js", "worker.js", "options.js", "popup.js"):
            with self.subTest(script=script):
                result = subprocess.run(
                    [shutil.which("node"), "--check", str(EXTENSION / script)],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js needed for mocked extension behavior")
    def test_worker_offline_queue_recovery_and_stream_coalescing(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
const state = {settings: {enabled: true, token: 'local-only-test-token', port: 8765}, queue: []};
let listener, offline = true;
const requests = [];
const context = vm.createContext({TextEncoder, crypto, AbortSignal, Date, Promise, Map,
  fetch: async (url, options) => {
    requests.push({url, options});
    if (offline) throw new Error('offline');
    return {ok:true, json: async () => url.endsWith('/resume') ? ({handoff: 'latest checkpoint'}) : ({messages_ingested: 1})};
  },
  chrome: {
    storage: {local: {
      get: async keys => {
        const names = typeof keys === 'string' ? [keys] : keys;
        return JSON.parse(JSON.stringify(Object.fromEntries(names.map(k => [k,state[k]]))));
      },
      set: async values => Object.assign(state, JSON.parse(JSON.stringify(values)))
    }, onChanged: {addListener() {}}},
    runtime: {onMessage: {addListener(fn) {listener=fn;}}, onStartup: {addListener() {}}},
    alarms: {create() {}, onAlarm: {addListener() {}}}
  }
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
async function send(message) {
  const reply = await new Promise(resolve => listener(message, {}, resolve));
  await vm.runInContext('operations', context);
  return reply;
}
(async () => {
  const payload = {source:'chatgpt', account:'plus-a', project:'demo', conversation_id:'chat-id-0001',
    messages:[{id:'msg-1',role:'assistant',text:'partial'}]};
  assert.equal((await send({type:'capture',payload})).ok, true);
  assert.equal(state.queue.length, 1);
  assert.equal(state.status.connected, false);
  payload.messages[0].text='complete answer';
  await send({type:'capture',payload});
  assert.equal(state.queue.length, 1);
  assert.equal(state.queue[0].payload.messages[0].text, 'complete answer');
  offline=false;
  await send({type:'retry'});
  assert.equal(state.queue.length, 0);
  assert.equal(state.status.connected, true);
  const sent=requests[requests.length-1];
  assert.equal(sent.url,'http://127.0.0.1:8765/api/live');
  assert.equal(sent.options.headers.Authorization,'Bearer local-only-test-token');
  assert.equal(sent.options.credentials,'omit');
  assert.equal(JSON.parse(sent.options.body).account,'plus-a');
  assert.equal(JSON.parse(sent.options.body).messages[0].text,'complete answer');
  const status=await send({type:'status'});
  assert.equal(JSON.stringify(status).includes('local-only-test-token'),false);
  state.settings.enabled=false;
  const resumed=await send({type:'resume',project:'demo'});
  assert.equal(resumed.handoff,'latest checkpoint');
  const resumeRequest=requests[requests.length-1];
  assert.equal(resumeRequest.url,'http://127.0.0.1:8765/api/resume');
  assert.equal(JSON.parse(resumeRequest.options.body).project,'demo');
  assert.equal(resumeRequest.options.headers.Authorization,'Bearer local-only-test-token');
  state.queue=Array.from({length:100},(_,index)=>({id:`q-${index}`, attempts:0, nextAttempt:0,
    payload:{...payload,conversation_id:`other-chat-${index}`,messages:[{id:'old',role:'user',text:'retained'}]}}));
  const full=await send({type:'capture',payload});
  assert.equal(full.ok,false);
  assert.equal(full.error,'queue_full');
  assert.equal(state.queue.length,100);
  assert.equal(state.queue[0].payload.messages[0].text,'retained');
})().catch(error => {console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(EXTENSION / "worker.js")],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js needed for mocked DOM capture behavior")
    def test_content_stable_chat_stream_revision_and_home_skip(self):
        script = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
let periodic;
const captured=[];
const settings={enabled:true,token:'test-token',account:'plus-a',project:'demo'};
const node={text:'hello',role:'assistant',id:'stable-msg-1',
  closest(selector){return selector==='[data-message-id]' ? this : null;},
  getAttribute(name){return name==='data-message-author-role' ? this.role : name==='data-message-id' ? this.id : null;},
  getClientRects(){return [1];},querySelector(){return {innerText:this.text};}};
const location={pathname:'/',origin:'https://chatgpt.com'};
let nodes=[node];
const context=vm.createContext({console,Date,Map,Set,WeakMap,Promise,crypto:require('node:crypto').webcrypto,
  location,document:{body:{},title:'Example - ChatGPT',querySelectorAll(){return nodes;}},
  setTimeout(){return 1;},clearTimeout(){},setInterval(fn){periodic=fn;},addEventListener(){},
  MutationObserver:class {observe(){}},
  chrome:{storage:{local:{get:async()=>({settings})},onChanged:{addListener(){}}},
    runtime:{sendMessage:async message=>{captured.push(JSON.parse(JSON.stringify(message)));return {ok:true};}}}
});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
  await periodic();
  assert.equal(captured.length,0);
  location.pathname='/c/chat-id-0001';
  await periodic();
  assert.equal(captured.length,1);
  assert.equal(captured[0].payload.messages[0].id,'stable-msg-1');
  assert.equal(captured[0].payload.account,'plus-a');
  await periodic();
  assert.equal(captured.length,1);
  node.text='hello complete';
  await periodic();
  assert.equal(captured.length,2);
  assert.equal(captured[1].payload.messages[0].id,'stable-msg-1');
  nodes=[];
  await periodic();
  assert.equal(captured.length,2); // Missing DOM turns do not issue deletes.
  location.pathname='/';nodes=[node];
  await periodic();
  assert.equal(captured.length,2);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(EXTENSION / "content.js")],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
