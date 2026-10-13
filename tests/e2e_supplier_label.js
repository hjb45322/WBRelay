/* 无头浏览器验证：供应商删除后，看板仍显示留档名（无头 Chrome + CDP，零依赖）
 *
 * 用**自建通用 fixture** 制造场景（见 tests/run_label_e2e.sh）：
 *   临时 settings.json = 通用供应商 A（配置在册）+ supplier_aliases{<目标id>: <留档名>}
 *   临时 usage.jsonl   = 含 <目标id> 的历史记录
 * 于是 <目标id> 在「当前配置」里查不到，只能靠三层解析还原名字。
 * 目标 id / 留档名默认是通用值，可用环境变量覆盖：
 *   WBRELAY_E2E_SUP_ID / WBRELAY_E2E_ALIAS / WBRELAY_E2E_PAGE_PORT / WBRELAY_E2E_SHOTS
 *
 * 断言：
 *   A1 按供应商汇总表显示留档名，而不是内部 id
 *   A2 筛选下拉显示留档名
 *   A3 按该供应商过滤后，请求明细同样显示留档名
 *   A4 全程无页面 JS 异常
 *
 * 用法：node tests/e2e_supplier_label.js [CDP端口]
 */
const fs = require('fs');
const path = require('path');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PORT = Number(process.argv[2] || 9303);
// 截图目录默认放进 tests/_shots（已在 .gitignore 中忽略，不会入库）
const SHOT_DIR = path.join(__dirname, process.env.WBRELAY_E2E_SHOTS || '_shots');
const PAGE_PORT = Number(process.env.WBRELAY_E2E_PAGE_PORT || 18899);
const 别名 = process.env.WBRELAY_E2E_ALIAS || '演示供应商 B';
const 目标id = process.env.WBRELAY_E2E_SUP_ID || 'sup_demo_b';
const 结果 = [];

function 记(name, ok, detail) {
  结果.push({ name, ok });
  console.log((ok ? '[PASS] ' : '[FAIL] ') + name + (detail ? ' | ' + detail : ''));
}

(async () => {
  if (!fs.existsSync(SHOT_DIR)) fs.mkdirSync(SHOT_DIR, { recursive: true });

  let target = null;
  for (let i = 0; i < 60 && !target; i++) {
    try {
      const list = await (await fetch('http://127.0.0.1:' + PORT + '/json/list')).json();
      target = list.find((x) => x.type === 'page' && x.url.indexOf(String(PAGE_PORT)) >= 0);
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

  await 调用('Runtime.enable');
  await 调用('Page.enable');
  await 调用('Page.reload');
  await sleep(3200);
  await 求值('stopUsageAutoRefresh()');                       // 关掉自动刷新，避免抢状态
  await 求值(`document.querySelector('.tab[data-tab="usage"]').click()`);
  await sleep(1200);
  // 切「全部」范围，保证已删供应商的历史记录都在候选里
  await 求值('USAGE_RANGE="all"; _curUsagePage=1; loadUsage(); 1');
  await sleep(2800);

  // ---- 前置自检：预览服务确实用的是隔离数据（配置里已无该供应商）----
  const 配置里有 = await 求值(`SUPPLIERS.some(function(s){return s.id==='${目标id}';})`);
  记('A0 前置：当前配置中已无该供应商（场景成立）', 配置里有 === false,
    'SUPPLIERS 含 ' + 目标id + ' = ' + 配置里有);

  // ---- A1 汇总表 ----
  const supLabels = await 求值(
    `[].slice.call(document.querySelectorAll('#tbl-sup tbody tr')).map(function(tr){return tr.children[0].textContent.trim();})`);
  if (supLabels && supLabels.__异常) throw new Error('读取汇总表失败：' + supLabels.__异常);
  记('A1 按供应商汇总显示留档名（而非 id）',
    supLabels.indexOf(别名) >= 0 && supLabels.indexOf(目标id) < 0 && supLabels.indexOf('暂无数据') < 0,
    '含「' + 别名 + '」= ' + (supLabels.indexOf(别名) >= 0) + ' / 含「' + 目标id + '」= ' + (supLabels.indexOf(目标id) >= 0));
  await 截图('03_删除后汇总.png');

  // ---- A2 筛选下拉 ----
  const opts = await 求值(
    `[].slice.call(document.getElementById('filter-supplier').options).map(function(o){return o.textContent;})`);
  记('A2 筛选下拉显示留档名',
    opts.indexOf(别名) >= 0 && opts.indexOf(目标id) < 0,
    '含「' + 别名 + '」= ' + (opts.indexOf(别名) >= 0) + ' / 含「' + 目标id + '」= ' + (opts.indexOf(目标id) >= 0));

  // ---- A3 明细：按该已删供应商过滤 ----
  await 求值(`USAGE_SUPPLIER='${目标id}'; _curUsagePage=1; loadUsage(); 1`);
  await sleep(2800);
  const 明细供应商列 = await 求值(
    `[].slice.call(document.querySelectorAll('#tbl-recent tbody tr')).map(function(tr){return tr.children[2].textContent.trim();})`);
  if (明细供应商列 && 明细供应商列.__异常) throw new Error('读取明细失败：' + 明细供应商列.__异常);
  const 全为别名 = 明细供应商列.length > 0 && 明细供应商列.every(function(x){ return x === 别名; });
  记('A3 请求明细同样显示留档名',
    全为别名,
    '共 ' + 明细供应商列.length + ' 行 / 唯一值 ' + JSON.stringify([...new Set(明细供应商列)]));
  记('A3b 明细中不出现内部 id', 明细供应商列.indexOf(目标id) < 0);
  await 截图('04_删除后明细.png');

  // ---- A4 无 JS 异常 ----
  记('A4 全程无页面 JS 异常', 异常.length === 0, 异常.slice(0, 3).join(' ;; '));

  const 通过 = 结果.filter((x) => x.ok).length;
  console.log('\n===== 真机结果：' + 通过 + ' 通过 / ' + (结果.length - 通过) + ' 失败 =====');
  ws.close();
  process.exit(结果.some((x) => !x.ok) ? 1 : 0);
})().catch((e) => {
  console.error('E2E 异常：' + (e && e.stack ? e.stack : e));
  process.exit(2);
});
