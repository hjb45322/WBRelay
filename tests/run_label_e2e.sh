#!/usr/bin/env bash
# 供应商显示名回落（留档名）的无头浏览器取证 —— 全程使用**自建通用 fixture**。
#
# 做法：
#   ① 在系统临时目录里造一份**测试自己的** settings.json + usage.jsonl
#      （通用供应商 id sup_demo_b / 名称「演示供应商 B」，假 Key）
#   ② 临时配置里把 sup_demo_b 从 suppliers 移除、写入 supplier_aliases 留档
#   ③ 起只读预览服务（--readonly --data-dir <临时目录> --no-ide-inject，端口 18899）
#   ④ 无头 Chrome + CDP 断言页面显示的是留档名，而不是内部 id
#
# 不读取、不复制 %APPDATA%\WBRelay 或任何在用数据；不触碰 8789 与外部进程。
#
# 用法：bash tests/run_label_e2e.sh
#   可用环境变量：WBRELAY_NODE / WBRELAY_CHROME 指定可执行文件
set -u
export PATH="/usr/bin:/bin:/c/Windows/System32:$PATH"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TMPD="${TMPDIR:-/tmp}/wbrelay-label-e2e"
PORT=18899
CDP=9303
SUP_ID="sup_demo_b"
SUP_NAME="演示供应商 B"

PY="${WBRELAY_PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in "/c/Python39/python.exe" "$(command -v python)" "$(command -v python3)"; do
    [ -n "$cand" ] && [ -x "$cand" ] && PY="$cand" && break
  done
fi
NODE="${WBRELAY_NODE:-$(command -v node || true)}"
CHROME="${WBRELAY_CHROME:-}"
if [ -z "$CHROME" ]; then
  for cand in \
    "/c/Program Files/Google/Chrome/Application/chrome.exe" \
    "/c/Program Files (x86)/Google/Chrome/Application/chrome.exe" \
    "$LOCALAPPDATA/Google/Chrome/Application/chrome.exe" \
    "$(command -v google-chrome || true)" \
    "$(command -v chromium || true)"; do
    [ -n "$cand" ] && [ -x "$cand" ] && CHROME="$cand" && break
  done
fi

if [ -z "$PY" ]; then echo "未找到 python 解释器（可设 WBRELAY_PYTHON）"; exit 3; fi
if [ -z "$NODE" ]; then echo "未找到 node（可设 WBRELAY_NODE）"; exit 3; fi
if [ -z "$CHROME" ]; then echo "未找到 chrome（可设 WBRELAY_CHROME）"; exit 3; fi

cd "$ROOT" || exit 1
mkdir -p /tmp/wbrun
rm -rf "$TMPD"
mkdir -p "$TMPD/data"

# ② 通用 fixture（全部由本脚本生成；无真实供应商、无真实凭据）
"$PY" - "$TMPD/data" "$SUP_ID" "$SUP_NAME" <<'PYEOF'
import json, os, sys, time

data_dir, sup_id, sup_name = sys.argv[1], sys.argv[2], sys.argv[3]
settings = {
    "port": 18899, "default_timeout_sec": 120, "cooldown_sec": 60,
    "cooldown_429_sec": 30, "usage_retention_days": 0, "usage_refresh_sec": 5,
    "require_api_key": False, "api_keys": [], "upstream_enabled": False,
    "suppliers": [{"id": "sup_demo_a", "name": "演示供应商 A",
                   "base_url": "http://127.0.0.1:18788/v1",
                   "api_key": "sk-test-FAKE-KEY-AAAA", "wire_api": "responses",
                   "models": [], "enabled": True, "timeout_sec": 10}],
    "supplier_aliases": {sup_id: sup_name},
}
now = time.time()
rows = [
    {"at": now - 30, "iso": "2026-01-01T00:00:30", "model": "demo-model-1",
     "realm": "supplier:" + sup_id, "stream": False, "tokens": 12,
     "input_tokens": 8, "output_tokens": 4, "elapsed_ms": 120},
    {"at": now - 20, "iso": "2026-01-01T00:00:20", "model": "demo-model-1",
     "realm": "supplier:sup_demo_a", "stream": True, "tokens": 21,
     "input_tokens": 13, "output_tokens": 8, "elapsed_ms": 210},
]
with open(os.path.join(data_dir, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump(settings, fh, ensure_ascii=False, indent=2)
with open(os.path.join(data_dir, "usage.jsonl"), "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
print("[准备] 通用 fixture：%s 条用量 / 留档 %s=%s" % (len(rows), sup_id, sup_name))
PYEOF

# ③ 只读预览服务（隔离数据目录 + 非 8789 端口）
# 用户目录全部重定向到本脚本临时目录：子进程即使去算 ~/.codex 也碰不到真实 Codex 配置
USERPROFILE="$TMPD" HOME="$TMPD" APPDATA="$TMPD" LOCALAPPDATA="$TMPD/localappdata" \
  WBRELAY_READONLY=1 WBRELAY_NO_IDE_INJECT=1 \
  "$PY" tests/preview_readonly_server.py "$PORT" --data-dir "$TMPD/data" \
  > /tmp/wbrun/label_preview.log 2>&1 &
SRV=$!
sleep 3

echo "--- 预览服务自检（隔离数据）---"
curl -s -o /dev/null -w "GET / = %{http_code}\n" --max-time 6 "http://127.0.0.1:$PORT/"
curl -s --max-time 20 "http://127.0.0.1:$PORT/api/usage?range=all&limit=1" | "$PY" -c "
import sys, json
d = json.load(sys.stdin)
f = {s['id']: s['name'] for s in d['filters']['suppliers']}
print('  接口层：filters 里 %s = %r' % ('$SUP_ID', f.get('$SUP_ID')))
rows = [s for s in d['by_supplier'] if (s.get('realm') or '').replace('supplier:', '') == '$SUP_ID']
print('  接口层：by_supplier 里 supplier_label =', repr(rows[0].get('supplier_label') if rows else None))
"

echo ""
"$CHROME" --headless=new --disable-gpu --hide-scrollbars --no-first-run --no-default-browser-check \
  --remote-debugging-port=$CDP --user-data-dir="$TMPD/chrome-profile" \
  --window-size=1600,1000 "http://127.0.0.1:$PORT/" > /tmp/wbrun/label_chrome.log 2>&1 &
CH=$!
sleep 7

WBRELAY_E2E_PAGE_PORT="$PORT" WBRELAY_E2E_SUP_ID="$SUP_ID" WBRELAY_E2E_ALIAS="$SUP_NAME" \
  "$NODE" tests/e2e_supplier_label.js $CDP
RC=$?

kill $CH 2>/dev/null
kill $SRV 2>/dev/null
echo "LABEL_E2E_EXIT=$RC"
exit $RC
