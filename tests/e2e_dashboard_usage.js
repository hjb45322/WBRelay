/* 真机验证（无头 Chrome + CDP，零依赖）
 *
 * 验证 20261011 用量看板改动：
 *   S1 候选收敛：今日 / 本周 / 全部 三个范围下，下拉候选数随时间范围变化
 *   S2 口径自洽：模型下拉候选数 - 1 == 按模型汇总表行数
 *   S3 失效回落：在「全部」下选中一个「今日没有」的模型，切回「今日」应自动清空该筛选并重查
 *   S4 页码重置：翻到第 3 页后切「今日」应回到第 1 页（旧版会停在越界页 + 空表）
 *   S5 刷新间隔：设置页可改，保存后前端即时生效，后端可读回（测完恢复默认 5）
 *   S6 全程无页面 JS 异常
 *
 * 用法：node tests/e2e_dashboard_usage.js [CDP端口]
 * 前置：tests/preview_readonly_server.py 已监听 18899，且无头 Chrome 已打开该地址
 */
const fs = require('fs');
const path = require('path');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PORT = Number(process.argv[2] || 9301);
const SHOT_DIR = path.join(__dirname, '_shots');
const 结果 = [];

function 记(name, ok, detail) {
  结果.push({ name, ok });
  console.log((ok ? '[PASS] ' : '[FAIL] ') + name + (detail ? ' | ' + detail : ''));
}

const 读状态 = `(function(){
  var selM = document.getElementById('filter-model');
  var selS = document.getElementById('filter-supplier');
  var firstRow = document.querySelector('#tbl-recent tbody tr td:nth-child(2) b');
  return {
    range: USAGE_RANGE,
    supplier: USAGE_SUPPLIER,
    model: USAGE_MODEL,
    supOpts: [].slice.call(selS.options).map(function(o){ return o.textContent; }),
    modelOpts: [].slice.call(selM.options).map(function(o){ return o.textContent; }),
    selM: selM.value,
    modelRows: document.querySelectorAll('#tbl-model tbody tr').length,
    supRows: document.querySelectorAll('#tbl-sup tbody tr').length,
    recentFirstModel: firstRow ? firstRow.textContent : '',
    page: _curUsagePage,
    refreshSec: USAGE_REFRESH_SEC
  };
})()`;

(async () => {
  if (!fs.existsSync(SHOT_DIR)) fs.mkdirSync(SHOT_DIR, { recursive: true });

  let target = null;
  for (let i = 0; i < 60 && !target; i++) {
    try {
      const list = await (await fetch('http://127.0.0.1:' + PORT + '/json/list')).json();
      target = list.find((x) => x.type === 'page' && x.url.indexOf('18899') >= 0);
    } catch (e) { /* Chrome 尚未就绪 */ }
    if (!target) await sleep(500);
  }
  if (!target) throw new Error('未找到预览页面 target（Chrome 未启动或地址不符）');

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener('open', r));
  let seq = 0;
  const 待 = new Map();
  const 异常 = [];
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && 待.has(m.id)) { 待.get(m.id)(m.result); 待.delete(m.id); return; }
    if (m.method === 'Runtime.exceptionThrown') {
      异常.push(((m.params.exceptionDetails.exception || {}).description) || m.params.exceptionDetails.text);
    }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      异常.push('console.error: ' + m.params.args.map((a) => a.value || a.description || '').join(' '));
    }
  });
  const 调用 = (method, params) => new Promise((res) => {
    const i = ++seq;
    待.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params: params || {} }));
  });
  const 求值 = async (expr) => {
    const r = await 调用('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return { __异常: (r.exceptionDetails.exception || {}).description || r.exceptionDetails.text };
    return r.result.value;
  };
  const 截图 = async (n) => {
    const r = await 调用('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(SHOT_DIR, n), Buffer.from(r.data, 'base64'));
  };
  const 切范围 = async (r, wait) => {
    await 求值(`document.querySelector('#range-seg button[data-r="${r}"]').click()`);
    await sleep(wait || 2200);
  };

  await 调用('Runtime.enable');
  await 调用('Page.enable');
  await 调用('DOM.enable');
  await 调用('Page.reload');
  await sleep(3200);

  // 关掉自动刷新定时器，避免 5 秒周期与断言抢状态
  await 求值('stopUsageAutoRefresh()');
  await 求值(`document.querySelector('.tab[data-tab="usage"]').click()`);
  await sleep(2600);

  // ---- S1/S2 今日 ----
  const 今日 = await 求值(读状态);
  if (今日.__异常) throw new Error('今日状态读取失败：' + 今日.__异常);
  记('S1 默认范围为「今日」', 今日.range === 'today', 'range=' + 今日.range);
  记('S2a 今日：模型候选 = 汇总表行数 + 1（口径一致）',
    今日.modelOpts.length - 1 === 今日.modelRows,
    `候选 ${今日.modelOpts.length} 项（含"全部模型"）/ 汇总 ${今日.modelRows} 行`);
  记('S2b 今日：供应商候选 = 汇总表行数 + 1',
    今日.supOpts.length - 1 === 今日.supRows,
    `候选 ${今日.supOpts.length} 项 / 汇总 ${今日.supRows} 行`);
  await 截图('01_今日.png');

  // ---- S1 本周 ----
  await 切范围('week');
  const 本周 = await 求值(读状态);
  记('S1 本周：候选 = 汇总表行数 + 1',
    本周.modelOpts.length - 1 === 本周.modelRows,
    `候选 ${本周.modelOpts.length} 项 / 汇总 ${本周.modelRows} 行`);
  记('S1 本周候选数 ≥ 今日候选数',
    本周.modelOpts.length >= 今日.modelOpts.length,
    `${本周.modelOpts.length} >= ${今日.modelOpts.length}`);

  // ---- S1 全部 ----
  await 切范围('all', 2600);
  const 全部 = await 求值(读状态);
  记('S1 全部：候选 = 汇总表行数 + 1',
    全部.modelOpts.length - 1 === 全部.modelRows,
    `候选 ${全部.modelOpts.length} 项 / 汇总 ${全部.modelRows} 行`);
  记('S1 全部候选数 ≥ 本周候选数（时间范围越大候选越多）',
    全部.modelOpts.length >= 本周.modelOpts.length,
    `${全部.modelOpts.length} >= ${本周.modelOpts.length}`);
  await 截图('02_全部.png');

  // ---- S3 失效回落 ----
  const 今日集合 = 今日.modelOpts.filter(function (x) { return x !== '全部模型'; });
  const 失效项 = await 求值(`(function(){
    var all = ${JSON.stringify(全部.modelOpts)};
    var today = ${JSON.stringify(今日集合)};
    var cand = all.filter(function(x){ return x && x !== '全部模型' && today.indexOf(x) < 0; });
    if(!cand.length) return '';
    var sel = document.getElementById('filter-model');
    sel.value = cand[0];
    sel.dispatchEvent(new Event('change'));
    return cand[0];
  })()`);
  if (!失效项) {
    记('S3 构造「今日不存在」的模型用于回落测试', false, '当前数据下无可用样本');
  } else {
    await sleep(2000);
    const 选中后 = await 求值(读状态);
    记('S3a 「全部」下可选中仅历史存在的模型', 选中后.model === 失效项,
      '选中=' + 失效项 + ' / USAGE_MODEL=' + 选中后.model);
    记('S3b 选中后明细确实按该模型过滤', 选中后.recentFirstModel === 失效项,
      '明细首行=' + 选中后.recentFirstModel);

    await 切范围('today');
    const 回落 = await 求值(读状态);
    记('S3c 切「今日」后失效筛选被自动清空', 回落.model === '',
      'USAGE_MODEL="' + 回落.model + '"');
    记('S3d 下拉显示回「全部模型」', 回落.selM === '', 'select.value="' + 回落.selM + '"');
    记('S3e 回落触发了重查（今日有数据）', 回落.modelRows > 0, '汇总 ' + 回落.modelRows + ' 行');
  }

  // ---- S4 页码重置 ----
  await 切范围('all', 2600);
  const 点了3 = await 求值(`(function(){
    var btns = [].slice.call(document.querySelectorAll('#pg-btns button'));
    var b3 = btns.filter(function(b){ return b.textContent === '3'; })[0];
    if(!b3) return false;
    b3.click(); return true;
  })()`);
  if (!点了3) {
    记('S4 构造「翻到第 3 页」场景', false, '当前数据不足 3 页');
  } else {
    await sleep(1600);
    const 第三页 = await 求值(读状态);
    记('S4a 已翻到第 3 页', 第三页.page === 3, 'page=' + 第三页.page);
    await 切范围('today');
    const 回一 = await 求值(读状态);
    记('S4b 切「今日」后页码重置为 1', 回一.page === 1, 'page=' + 回一.page);
    记('S4c 重置后今日明细非空（旧版此处为空表）', 回一.modelRows > 0, '汇总 ' + 回一.modelRows + ' 行');
  }

  // ---- S5 刷新间隔可配 ----
  await 求值(`document.querySelector('.tab[data-tab="settings"]').click()`);
  await sleep(1800);
  const 设置页 = await 求值(`(function(){ var e = document.getElementById('set-refresh'); return { exists: !!e, value: e ? e.value : null }; })()`);
  记('S5a 设置页存在「自动刷新间隔」输入框', 设置页.exists);
  记('S5b 默认值 5 秒（与后端 usage_refresh_secs 默认一致）', String(设置页.value) === '5', 'value=' + 设置页.value);

  await 求值(`(function(){ document.getElementById('set-refresh').value = '7'; document.getElementById('btn-save-settings').click(); })()`);
  await sleep(2000);
  const 改后 = await 求值(读状态);
  记('S5c 改为 7 后前端即时生效', 改后.refreshSec === 7, 'USAGE_REFRESH_SEC=' + 改后.refreshSec);
  const 服务端7 = await 求值(`fetch('/api/settings').then(function(r){return r.json();}).then(function(j){return j.usage_refresh_sec;})`);
  记('S5d 后端已持久化 usage_refresh_sec = 7', 服务端7 === 7, 'server=' + 服务端7);

  await 求值(`(function(){ document.getElementById('set-refresh').value = '5'; document.getElementById('btn-save-settings').click(); })()`);
  await sleep(2000);
  const 服务端5 = await 求值(`fetch('/api/settings').then(function(r){return r.json();}).then(function(j){return j.usage_refresh_sec;})`);
  const 复原 = await 求值(读状态);
  记('S5e 已恢复默认 5 秒（不留副作用）', 服务端5 === 5 && 复原.refreshSec === 5,
    'server=' + 服务端5 + ' / 前端=' + 复原.refreshSec);

  // ---- S6 无异常 ----
  记('S6 全程无页面 JS 异常', 异常.length === 0, 异常.slice(0, 3).join(' || '));

  const 失败 = 结果.filter(function (r) { return !r.ok; });
  console.log('\n===== 真机结果：' + (结果.length - 失败.length) + ' 通过 / ' + 失败.length + ' 失败 =====');
  失败.forEach(function (f) { console.log('  失败项：' + f.name); });
  ws.close();
  process.exit(失败.length ? 1 : 0);
})().catch((e) => { console.error('测试脚本失败：', e.message); process.exit(2); });
