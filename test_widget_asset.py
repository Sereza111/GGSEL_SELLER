"""Packaging and browser-free contract checks for the actual widget JavaScript.

Node is optional locally; CI runs these checks when Node is present. The small
DOM adapter deliberately has no HTML parser: customer strings must stay text.
"""

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
import zlib


ASSET = Path(__file__).parent / "assets" / "dashboard.html"
SOURCE = ASSET.read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>(.*?)</script>", SOURCE, re.S).group(1)
NODE = shutil.which("node")

HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.dataset={}; this.style={};
    this.attributes={}; this.className=''; this.disabled=false; this.nodeType=1; this._text=''; }
  appendChild(n) { this.children.push(n); return n; }
  set textContent(s) { this._text=String(s); this.children=[]; }
  get textContent() { return this._text+this.children.map(n=>n.textContent).join(''); }
  set innerHTML(_) { throw Error('Dynamic HTML is forbidden in this dashboard'); }
  setAttribute(k,v) { this.attributes[k]=v; }
  click() { if(!this.disabled && this.onclick) this.onclick(); }
}
const ids={};
for(const id of ['content','notice','badge','tabs','time','refresh']) {
  ids[id]=new Element(id==='refresh'?'button':'div'); ids[id].id=id;
}
const all=()=>Object.values(ids).flatMap(function walk(n){return [n,...n.children.flatMap(walk)]});
const document={getElementById:id=>ids[id],createElement:tag=>new Element(tag),
  querySelectorAll:selector=>{assert.equal(selector,'button');return all().filter(n=>n.tagName==='button')}};
const base={v:1,view:'home',sync:true,notify:true,chats:5,commands:2,plugins:1,queue:0,
  group:'https://t.me/c/12345/1',updated:'12:30 UTC',health:'ok',page:0,pages:1,
  items:[],checks:['12:30 UTC','12:30 UTC','12:30 UTC'],confirm:'',busy:false};
const events={},requests=[],links=[],pending=[];
let ready=0;
const W={chatData:JSON.stringify(base),onEvent:(name,fn)=>events[name]=fn,ready:()=>ready++,
  request:key=>{requests.push(key);return new Promise((resolve,reject)=>pending.push({resolve,reject}))},
  openTelegramLink:url=>links.push(url)};
vm.runInNewContext(SCRIPT,{window:{TelegramWidget:W},document,console});
const state=data=>{W.chatData=JSON.stringify({...base,...data});events.chatDataChanged()};
const button=text=>{const b=all().find(n=>n.tagName==='button'&&n.textContent===text);
  assert.ok(b,'Missing button: '+text);return b;};
const flush=async()=>{await new Promise(resolve=>setImmediate(resolve))};
const answer=async(data)=>{pending.shift().resolve({result:JSON.stringify({...base,...data})});await flush()};
"""


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = set()
        self.resources = []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.resources.extend(value for key, value in attrs if key in ("src", "href"))


class WidgetAssetPackagingTests(unittest.TestCase):
    def test_payload_fits_telegram_limits(self):
        raw = ASSET.read_bytes()
        self.assertLessEqual(len(raw), 32768)
        self.assertLessEqual(len(zlib.compress(raw, 9)), 4096)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))

    def test_asset_is_self_contained_and_uses_supported_profile(self):
        parser = AssetParser()
        parser.feed(SOURCE)
        self.assertLessEqual(parser.tags, {
            "html", "head", "style", "body", "header", "span", "main", "p",
            "nav", "div", "button", "script",
        })
        self.assertEqual(parser.resources, [])
        self.assertNotRegex(SCRIPT, r"\b(fetch|XMLHttpRequest|WebSocket|eval)\s*\(")
        self.assertNotRegex(SOURCE, r"@import|@font-face|overflow\s*:\s*(auto|scroll)")


@unittest.skipUnless(NODE, "Node is required to exercise the embedded JavaScript")
class WidgetAssetRuntimeTests(unittest.TestCase):
    def run_js(self, check):
        source = "const SCRIPT=" + json.dumps(SCRIPT) + ";\n" + HARNESS
        source += "\n(async()=>{\n" + check + "\n})().catch(e=>{console.error(e);process.exit(1)});"
        result = subprocess.run([NODE, "-"], input=source, text=True,
                                encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_navigation_uses_result_envelope_and_blocks_double_taps(self):
        self.run_js("""
          assert.equal(ready,1);
          button('Перейти к покупателям →').click();
          button('Перейти к покупателям →').click();
          assert.deepEqual(requests,['view:chats:0']);
          assert.ok(ids.refresh.disabled);
          await answer({view:'chats'});
          assert.match(ids.content.textContent,/Переписки/);
          assert.equal(ids.refresh.disabled,false);
        """)

    def test_transition_notice_keeps_controls_locked_but_allows_refresh(self):
        self.run_js("""
          state({view:'settings',busy:true,notice:'Изменяем состояние синхронизации…'});
          assert.equal(ids.notice.textContent,'Изменяем состояние синхронизации…');
          assert.ok(button('Приостановить').disabled);
          assert.equal(ids.refresh.disabled,false);
          ids.refresh.click();
          assert.ok(ids.refresh.disabled);
          state({view:'settings',busy:false});
          assert.ok(button('Приостановить').disabled,'A state event cannot unlock a pending request');
          await answer({view:'settings',busy:false,notice:'Синхронизация включена'});
          assert.equal(button('Приостановить').disabled,false);
          assert.equal(ids.notice.textContent,'Синхронизация включена');
        """)

    def test_disabled_page_edges_survive_failure_and_chat_updates(self):
        self.run_js("""
          state({view:'chats',page:0,pages:2});
          assert.ok(button('←').disabled);
          button('→').click();
          assert.deepEqual(requests,['view:chats:1']);
          pending.shift().reject({code:'RATE_LIMITED'}); await flush();
          assert.ok(button('←').disabled);
          assert.equal(button('→').disabled,false);
          assert.match(ids.notice.textContent,/Подождите/);
          state({view:'chats',page:1,pages:2});
          assert.ok(button('→').disabled);
          button('←').click();
          assert.equal(requests[1],'view:chats:0');
        """)

    def test_customer_markup_is_text_and_only_valid_topic_links_open(self):
        self.run_js("""
          state({view:'chats',items:[
            {id:7,name:'<img src=x onerror=alert(1)>',url:'https://t.me/c/12345/7'},
            {id:8,name:'Unsafe',url:'javascript:alert(1)'}]});
          const chats=all().filter(n=>n.className==='chat');
          assert.equal(chats.length,2);
          assert.ok(chats[0].textContent.includes('<img src=x onerror=alert(1)>'));
          chats[0].click();
          assert.deepEqual(links,['https://t.me/c/12345/7'],'Link opens directly on tap');
          assert.ok(chats[1].disabled);
          ids.refresh.click();
          pending.shift().reject({code:'TIMEOUT'});await flush();
          assert.ok(chats[1].disabled);
          chats[1].click();assert.equal(links.length,1);
        """)

    def test_pause_requires_confirmation_and_carries_server_nonce(self):
        self.run_js("""
          state({view:'settings'});button('Приостановить').click();
          assert.equal(requests[0],'sync:pause');
          await answer({view:'settings',confirm:'nonce_abc'});
          assert.match(ids.content.textContent,/Приостановить работу/);
          button('Приостановить').click();
          assert.equal(requests[1],'confirm:nonce_abc');
          await answer({view:'settings',sync:false,notice:'Синхронизация приостановлена'});
          button('Возобновить').click();assert.equal(requests[2],'sync:resume');
        """)

    def test_notification_toggle_and_invalid_group_link(self):
        self.run_js("""
          state({view:'settings',group:'https://example.com/steal'});
          assert.ok(button('Рабочая группа ↗').disabled);
          button('Вкл.').click();assert.equal(requests[0],'notify:off');
          await answer({view:'settings',notify:false,group:''});
          button('Выкл.').click();assert.equal(requests[1],'notify:on');
          assert.ok(button('Рабочая группа ↗').disabled);
        """)

    def test_malformed_answer_and_platform_rejection_show_failure(self):
        self.run_js("""
          ids.refresh.click();pending.shift().resolve({result:'not JSON'});await flush();
          assert.equal(ids.notice.className,'error');assert.equal(ids.refresh.disabled,false);
          ids.refresh.click();pending.shift().reject({code:'NOT_ALLOWED'});await flush();
          assert.match(ids.notice.textContent,/личном чате/);
          ids.refresh.click();pending.shift().resolve({result:JSON.stringify({error:'Доступ запрещён'})});
          await flush();assert.equal(ids.notice.textContent,'Доступ запрещён');
          ids.refresh.click();pending.shift().reject(null);await flush();
          assert.equal(ids.notice.className,'error');assert.equal(ids.refresh.disabled,false);
        """)

    def test_server_state_subscription_updates_without_a_request(self):
        self.run_js("""
          state({view:'status',checks:['10:11 UTC','10:12 UTC','10:13 UTC'],queue:4});
          assert.match(ids.content.textContent,/10:13 UTC/);
          assert.match(ids.content.textContent,/ОчередьДоставка в Telegram4/);
          assert.deepEqual(requests,[]);
          W.chatData='invalid';events.chatDataChanged();
          assert.equal(ids.notice.className,'error');
        """)


if __name__ == "__main__":
    unittest.main()
