# -*- coding: utf-8 -*-
"""检查看板 JS 语法，以及 IDE 卡片的隔离 mock 行为（不联网、不操作客户端）。

node 从 PATH 查找，找不到时尝试常见安装位置；都没有则跳过（返回 0），
以便没有 Node 的机器也能跑其他测试。

用法：python tests/check_dashboard_js.py
"""
import io
import os
import re
import shutil
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "dashboard.html")
TMP_DIR = os.path.join(ROOT, "tests", "_tmp_js")


def find_node():
    """定位 node 可执行文件，找不到返回空串。不假设固定安装路径。"""
    exe = shutil.which("node")
    if exe:
        return exe
    candidates = [
        os.path.join(os.environ.get("ProgramFiles") or "", "nodejs", "node.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)") or "", "nodejs", "node.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs", "nodejs", "node.exe"),
        "/usr/bin/node",
        "/usr/local/bin/node",
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return ""


NODE = find_node()


IDE_CHECKS = r'''
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const html = fs.readFileSync(process.argv[1], "utf8");
const script = /<script[^>]*>([\s\S]*?)<\/script>/.exec(html)[1];
function section(start, end){
  const a = script.indexOf(start), b = script.indexOf(end, a);
  assert(a >= 0 && b > a, "missing production section: " + start);
  return script.slice(a, b);
}
class Element {
  constructor(){
    this.children = []; this.controls = []; this.attrs = {}; this.listeners = {};
    this.style = {}; this.className = ""; this.value = ""; this.writes = 0;
    this.classList = {
      contains: name => this.className.split(" ").includes(name),
      add: name => { this.className += " " + name; },
      remove: name => { this.className = this.className.split(" ").filter(x => x !== name).join(" "); }
    };
  }
  set innerHTML(value){
    this.markup = value; this.children = []; this.controls = []; this.writes++;
    const pattern = /<(?:div|button) class="([^"]+)" (data-ide-(?:toggle|restart))="([^"]+)"/g;
    for(const m of value.matchAll(pattern)){
      const c = new Element(); c.className = m[1]; c.attrs[m[2]] = m[3]; this.controls.push(c);
    }
  }
  get innerHTML(){ return this.markup || ""; }
  appendChild(child){ this.children.push(child); }
  removeChild(child){ this.children = this.children.filter(c => c !== child); }
  querySelectorAll(selector){
    const attr = selector.slice(1, -1);
    return this.controls.filter(c => attr in c.attrs).concat(this.children.flatMap(c => c.querySelectorAll(selector)));
  }
  getAttribute(name){ return this.attrs[name]; }
  setAttribute(name, value){ this.attrs[name] = value; }
  removeAttribute(name){ delete this.attrs[name]; }
  addEventListener(name, fn){ this.listeners[name] = fn; }
  select(){ this.selected = true; }
}
const nodes = Object.fromEntries(["ides-strip", "ide-base-url", "btn-copy-ide-base-url",
  "st-ver", "st-port", "st-req", "st-err"].map(id => [id, new Element()]));
const tabs = ["suppliers", "models", "usage", "upstream", "settings"].map(name => {
  const b = new Element(); b.dataset = {tab:name};
  b.className = name === "models" ? "tab active" : "tab";
  nodes["pg-" + name] = new Element(); return b;
});
const timers = new Map(), requests = [], pending = [], responses = [], messages = [];
let nextTimer = 0, copied = "", copyOk = true;
const doc = {
  hidden:false, body:new Element(), listeners:{},
  getElementById:id => nodes[id] || null,
  createElement:() => new Element(),
  querySelector:() => tabs.find(b => b.classList.contains("active")),
  querySelectorAll:selector => selector === ".tab" ? tabs : Object.keys(nodes).filter(id => id.startsWith("pg-")).map(id => nodes[id]),
  addEventListener(name, fn){ this.listeners[name] = fn; },
  execCommand(cmd){ assert.equal(cmd, "copy"); copied = this.body.children[0].value; return copyOk; }
};
const ctx = vm.createContext({document:doc, window:{}, localStorage:{getItem:() => null},
  toast:(msg, err) => messages.push({msg, err}), setTimeout:() => 0,
  setInterval:(fn, ms) => { const id = nextTimer++; timers.set(id, {fn, ms}); return id; },
  clearInterval:id => timers.delete(id),
  fetch:(path, opts) => {
    requests.push({path, opts});
    if(responses.length) return Promise.resolve(responses.shift());
    return new Promise((resolve, reject) => pending.push({resolve, reject}));
  }
});
function response(text, status=200){
  return {status, text:() => Promise.resolve(text), json:() => Promise.resolve().then(() => JSON.parse(text))};
}
function enqueue(text, status=200){ responses.push(response(text, status)); }
async function drain(){ for(let i=0; i<25; i++) await Promise.resolve(); }
function activate(name){ tabs.find(b => b.dataset.tab === name).listeners.click(); }
vm.runInContext(section("function el(", "var toastTimer"), ctx);
vm.runInContext(section("/* ---------- IDE 客户端卡片组", "/* ---------- 模型配置页"), ctx);
vm.runInContext(section("function loadState(){", "function healthBadge("), ctx);
ctx.supSig = () => ""; ctx.renderCards = () => {};
for(const name of ["loadModels", "loadSettings", "loadUsage", "loadModelPrices",
  "startUsageAutoRefresh", "stopUsageAutoRefresh", "loadUpstream"]) ctx[name] = () => {};
vm.runInContext(section("/* ---------- Tab", "/* ---------- 供应商页"), ctx);
vm.runInContext(section("function copyText(", "/* 初始 */"), ctx);

(async function(){
  const labels = {wbrelay:"WBRelay 接管中", wbrelay_stale:"WBRelay 残留（端口不一致）",
    external:"其他工具或手工接管", none:"未接管"};
  const colors = {wbrelay:"on", wbrelay_stale:"cool", external:"neutral", none:"off"};
  for(const [state, label] of Object.entries(labels)){
    const markup = ctx.ideOwnerHtml({owner:{state, detail:'host<"&'}, external_change:true});
    assert(markup.includes('class="badge ' + colors[state] + '" title="host&lt;&quot;&amp;">' + label));
    assert(markup.includes("配置已被外部改写"));
    assert(markup.includes("host&lt;&quot;&amp;"));
    assert(!markup.includes('host<"&'));
  }
  for(const owner of [undefined, null, {}, {state:"future"}, {state:"__proto__"}]){
    assert(ctx.ideOwnerHtml({owner}).includes('class="badge neutral">—'));
    assert(ctx.ideOwnerHtml({owner}).includes("未提供外部改动状态"));
  }
  assert(!ctx.ideOwnerHtml({owner:{state:"none"}, external_change:false}).includes("配置已被外部改写"));
  ctx.renderIdes({ides:[{id:"old", name:"Old", detected:true}, {id:"missing", detected:false}]});
  const strip = nodes["ides-strip"];
  assert.equal(strip.children.length, 2);
  assert(strip.children[0].innerHTML.includes("已检测到"));
  assert(strip.children[0].innerHTML.includes("—"));
  assert(strip.children[1].classList.contains("not-detected"));
  assert(strip.children[1].innerHTML.includes("未检测到"));
  console.log("  IDE owner: four states, external change, escaping, legacy fallback OK");

  const payload = JSON.stringify({ides:[{id:"demo", name:"Demo", detected:true, enabled:false, restartable:true}], detected:1});
  enqueue(payload); await ctx.loadIdes();
  const card = strip.children[0], writes = strip.writes;
  enqueue(payload); await ctx.loadIdes();
  assert.strictEqual(strip.children[0], card); assert.equal(strip.writes, writes);
  enqueue(payload + "\n"); await ctx.loadIdes();
  assert.equal(strip.writes, writes + 1, "raw whitespace difference must count");
  enqueue("not JSON"); await ctx.loadIdes();
  enqueue("null"); await ctx.loadIdes();
  enqueue('{"ides":{}}'); await ctx.loadIdes();
  enqueue("{}", 503); await ctx.loadIdes();
  const failed = ctx.loadIdes(); pending.shift().reject(new Error("offline")); await failed;
  assert.equal(strip.writes, writes + 1, "bad responses retain cards");
  const one = ctx.loadIdes(), count = requests.length;
  assert.strictEqual(ctx.loadIdes(), one); assert.equal(requests.length, count);
  pending.shift().resolve(response(payload)); await one;
  assert.equal(ctx._idesRequest, null);
  console.log("  IDE payload: byte-exact deduplication, single flight, error recovery OK");

  enqueue(payload); ctx.startIdesAutoRefresh(); await drain();
  assert.equal(timers.size, 1); assert.equal([...timers.values()][0].ms, 5000);
  const beforeHidden = requests.length;
  doc.hidden = true; [...timers.values()][0].fn(); await ctx.loadIdes();
  assert.equal(requests.length, beforeHidden);
  doc.hidden = false;
  enqueue(payload); [...timers.values()][0].fn(); await drain();
  assert.equal(requests.length, beforeHidden + 1);
  enqueue(payload); activate("settings"); await drain(); assert.equal(timers.size, 1);
  enqueue(payload); activate("models"); await drain(); assert.equal(timers.size, 1);
  for(const tab of ["usage", "upstream", "suppliers"]){ activate(tab); assert.equal(timers.size, 0); }
  ctx.startIdesAutoRefresh(); assert.equal(timers.size, 0);
  enqueue(payload); activate("models"); await drain(); ctx.stopIdesAutoRefresh();
  console.log("  IDE polling: fixed 5000 ms, settings/models only, hidden skip, tab cleanup OK");

  const poll = ctx.loadIdes();
  const sw = strip.querySelectorAll("[data-ide-toggle]")[0];
  sw.listeners.click(); sw.listeners.click();
  assert.equal(ctx._idesBusy, 1, "duplicate clicks ignored");
  const whileBusy = requests.length; await ctx.loadIdes(); assert.equal(requests.length, whileBusy);
  pending.shift().resolve(response(payload + " ")); await poll;
  assert.strictEqual(strip.querySelectorAll("[data-ide-toggle]")[0], sw);
  enqueue(payload.replace('"enabled":false', '"enabled":true'));
  pending.shift().resolve(response("{}")); await drain();
  assert.equal(ctx._idesBusy, 0);
  assert(strip.querySelectorAll("[data-ide-toggle]")[0].classList.contains("on"));
  const restart = strip.querySelectorAll("[data-ide-restart]")[0];
  restart.listeners.click(); assert.equal(ctx._idesBusy, 1); assert.equal(restart.disabled, true);
  enqueue(payload); pending.shift().reject(new Error("offline")); await drain();
  assert.equal(ctx._idesBusy, 0); assert.equal(restart.disabled, false);
  assert(messages.some(m => m.err));
  // A mutation finishing before an older poll must trigger a fresh probe after that poll settles.
  const oldPoll = ctx.loadIdes(), oldReply = pending.shift();
  strip.querySelectorAll("[data-ide-toggle]")[0].listeners.click();
  pending.shift().resolve(response("{}")); await drain();
  enqueue(payload + "  "); oldReply.resolve(response(payload + "   ")); await oldPoll;
  assert.equal(ctx._idesPayload, payload + "  ");
  console.log("  IDE actions: in-flight DOM preserved, stale probes ignored, controls recover OK");

  assert(html.includes('id="ide-base-url"'));
  assert(html.includes('data-copy="ide-base-url"'));
  ctx.renderIdeBaseUrl(undefined); assert.equal(nodes["ide-base-url"].value, "");
  assert.equal(nodes["btn-copy-ide-base-url"].disabled, true);
  enqueue('{"port":23456}'); await ctx.loadState();
  assert.equal(nodes["ide-base-url"].value, "http://127.0.0.1:23456/v1");
  assert.equal(nodes["btn-copy-ide-base-url"].disabled, false);
  const copyBtn = {dataset:{copy:"ide-base-url"}};
  doc.listeners.click({target:{closest:() => copyBtn}});
  assert.equal(copied, "http://127.0.0.1:23456/v1");
  assert.equal(doc.body.children.length, 0);
  nodes["path-test"] = new Element(); nodes["path-test"].value = "mock-path";
  doc.listeners.click({target:{closest:() => ({dataset:{copy:"path-test"}})}});
  assert.equal(copied, "mock-path");
  copyOk = false; ctx.copyText("mock", "copied"); assert.equal(messages[messages.length-1].err, true);
  for(const port of [null, "", 0, -1, 65536, "bad", "123.5", '<img>']){
    ctx.renderIdeBaseUrl(port); assert.equal(nodes["ide-base-url"].value, "");
    assert.equal(nodes["btn-copy-ide-base-url"].disabled, true);
  }
  ctx.renderIdeBaseUrl("34567"); assert.equal(nodes["ide-base-url"].value, "http://127.0.0.1:34567/v1");
  assert.equal(pending.length, 0); assert.equal(timers.size, 0);
  console.log("  IDE manual access: runtime port, missing/invalid fallback, shared clipboard OK");
})().catch(err => { console.error(err); process.exitCode = 1; });
'''


def main():
    if not NODE:
        print("未找到 node，跳过看板 JS 语法检查（安装 Node.js 后会自动启用）")
        return 0

    html = io.open(HTML, "r", encoding="utf-8").read()
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    print("dashboard.html 内联 script 块：%d 个" % len(blocks))
    if not blocks:
        print("未找到 script 块")
        return 1

    if os.path.isdir(TMP_DIR):
        shutil.rmtree(TMP_DIR)
    os.makedirs(TMP_DIR)

    ok_all = True
    try:
        for i, body in enumerate(blocks):
            tmp = os.path.join(TMP_DIR, "block%d.js" % i)
            io.open(tmp, "w", encoding="utf-8").write(body)
            proc = subprocess.run([NODE, "--check", tmp], capture_output=True, text=True)
            status = "OK" if proc.returncode == 0 else "语法错误"
            print("  block#%d（%d 字符）→ %s" % (i, len(body), status))
            if proc.returncode != 0:
                ok_all = False
                print(proc.stderr.strip()[:800])
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
    if ok_all:
        proc = subprocess.run([NODE, "-e", IDE_CHECKS, HTML], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        print("IDE 卡片隔离 mock 检查（无网络 / 无客户端操作）：")
        print(proc.stdout.strip())
        if proc.returncode != 0:
            ok_all = False
            print(proc.stderr.strip()[:2000])
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
