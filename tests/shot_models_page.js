/* 模型配置页布局取证（无头 Chrome + CDP，零依赖）
 *
 * 断言（1440×900 宽档 + 390×844 窄档，明暗两主题）：
 *   L1 纵向顺序：标题 → IDE 卡片 → 拉取容器 → 分组容器
 *   L2 拉取容器内有边框、按钮左对齐、说明紧随按钮右侧（宽带同行 / 窄带落到下一行）
 *   L3 统计徽标 = 6 枚「标签 + 数值」胶囊，分组容器有边框
 *   L4 无横向溢出（scrollWidth <= innerWidth + 1）
 *   L5 全程无页面 JS 异常
 *
 * 用法：node tests/shot_models_page.js [CDP端口] [输出目录]
 * 前置：tests/preview_readonly_server.py 已监听 18899（可用临时数据目录），
 *       无头 Chrome 已打开 http://127.0.0.1:18899/
 */
const fs = require('fs');
const path = require('path');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PORT = Number(process.argv[2] || 9305);
const OUT = process.argv[3] || path.join(__dirname, '_shots');
const 结果 = [];
const 记 = (name, ok, detail) => {
  结果.push({ name, ok });
  console.log((ok ? '[PASS] ' : '[FAIL] ') + name + (detail ? ' | ' + detail : ''));
};

const 读布局 = `(function(){
  function box(sel){ var e = document.querySelector(sel); if(!e) return null;
    var r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
             right: Math.round(r.right), bottom: Math.round(r.bottom),
             border: parseFloat(cs.borderTopWidth) > 0 }; }
  var chips = [].slice.call(document.querySelectorAll('#models-meta .mm-chip'));
  return {
    vw: window.innerWidth,
    title: box('.ph-title'), ides: box('#ides-group'), phBox: box('.ph-box'),
    actions: box('.ph-actions'), btn: box('#btn-refresh-models'), desc: box('.ph-desc'),
    stats: box('.ph-stats'), groups: box('#models-box'),
    chips: chips.map(function(c){ var lab = c.querySelector('span'), b = c.querySelector('b');
      return (lab ? lab.textContent.trim() : '') + '=' + (b ? b.textContent : ''); }),
    overflow: document.documentElement.scrollWidth - window.innerWidth
  };
})()`;

(async () => {
  fs.mkdirSync(OUT, { recursive: true });

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
    if (r.exceptionDetails) throw new Error('求值失败：' + ((r.exceptionDetails.exception || {}).description || r.exceptionDetails.text));
    return r.result.value;
  };
  const 视口 = async (w, h) => {
    await 调用('Emulation.setDeviceMetricsOverride', {
      width: w, height: h, deviceScaleFactor: 1, mobile: w < 700,
    });
    await sleep(700);
  };
  const 主题 = async (t) => {
    await 求值(`(function(){ document.documentElement.dataset.theme = '${t}';
      try { localStorage.setItem('wb_theme', '${t}'); } catch(e){} })()`);
    await sleep(500);
  };
  const 截图 = async (name) => {
    const r = await 调用('Page.captureScreenshot', { format: 'png' });
    const f = path.join(OUT, name);
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
    console.log('  → ' + f);
  };

  await 调用('Runtime.enable');
  await 调用('Page.enable');
  await 调用('Page.reload');
  await sleep(3500);
  await 求值(`document.querySelector('.tab[data-tab="models"]').click()`);
  for (let i = 0; i < 40; i++) {
    if (await 求值(`document.querySelectorAll('#models-meta .mm-chip').length > 0`)) break;
    await sleep(500);
  }
  await sleep(600);

  // ---- 宽档 1440×900 ----
  await 视口(1440, 900);
  await 主题('light');
  await 截图('models-1440-light.png');
  let L = await 求值(读布局);
  记('L1a IDE 卡片在标题下方', L.ides.y >= L.title.bottom,
    `title.bottom=${L.title.bottom} ides.y=${L.ides.y}`);
  记('L1b 拉取容器在 IDE 卡片下方', L.phBox.y >= L.ides.bottom,
    `ides.bottom=${L.ides.bottom} phBox.y=${L.phBox.y}`);
  记('L1c 分组容器在拉取容器下方', L.groups.y >= L.phBox.bottom,
    `phBox.bottom=${L.phBox.bottom} groups.y=${L.groups.y}`);
  记('L2a 拉取容器与分组容器都有边框', L.phBox.border && L.groups.border,
    `phBox.border=${L.phBox.border} groups.border=${L.groups.border}`);
  记('L2b 按钮在容器内左对齐', L.btn.x === L.actions.x,
    `btn.x=${L.btn.x} 行左缘=${L.actions.x}`);
  记('L2c 宽带说明紧跟在按钮右侧（同一行）', L.desc.x > L.btn.right && L.desc.y < L.actions.bottom,
    `btn.right=${L.btn.right} desc.x=${L.desc.x} desc.y=${L.desc.y}`);
  记('L3a 统计徽标 6 枚', L.chips.length === 6, L.chips.join(' / '));
  记('L3b 统计含标签与数值', /^模型=33 个$/.test(L.chips[1] || ''), L.chips[1]);
  记('L3c 统计行在说明行下方', L.stats.y >= L.desc.bottom,
    `desc.bottom=${L.desc.bottom} stats.y=${L.stats.y}`);

  await 主题('dark');
  await 截图('models-1440-dark.png');

  // ---- 窄档 390×844 ----
  await 视口(390, 844);
  await 主题('light');
  await 截图('models-390-light.png');
  L = await 求值(读布局);
  记('L4a 窄档无横向溢出（明）', L.overflow <= 1, `overflow=${L.overflow}px vw=${L.vw}`);
  记('L4b 窄档说明落到按钮下一行', L.desc.y >= L.btn.bottom,
    `btn.bottom=${L.btn.bottom} desc.y=${L.desc.y}`);
  记('L4c 窄档统计行仍在说明行下方', L.stats.y >= L.desc.bottom,
    `desc.bottom=${L.desc.bottom} stats.y=${L.stats.y}`);
  await 主题('dark');
  await 截图('models-390-dark.png');
  L = await 求值(读布局);
  记('L4d 窄档无横向溢出（暗）', L.overflow <= 1, `overflow=${L.overflow}px`);

  await 调用('Emulation.clearDeviceMetricsOverride');
  记('L5 全程无页面 JS 异常', 异常.length === 0, 异常.slice(0, 3).join(' || '));

  const 失败 = 结果.filter((r) => !r.ok);
  console.log('\n===== 模型配置页布局：' + (结果.length - 失败.length) + ' 通过 / ' + 失败.length + ' 失败 =====');
  失败.forEach((f) => console.log('  失败项：' + f.name));
  ws.close();
  process.exit(失败.length ? 1 : 0);
})().catch((e) => { console.error('取证脚本失败：', e.message); process.exit(2); });
