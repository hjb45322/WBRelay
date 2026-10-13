# -*- coding: utf-8 -*-
"""只读预览服务器（仅供真机验证用）

复用生产 Handler 提供看板页面与只读 API，但**不调用 wbrelay.app.main()**，因此：
  - 不启动后台线程（用量清理 / 上游服务 watchdog）
  - 不写任何数据文件
  - 不接管 ~/.codex/config.toml（那是 main() 里 codex_auto_switch 的行为）

脚本会强制追加 `--readonly`，所以无论如何调用都不会写盘。

用法：
    python tests/preview_readonly_server.py [端口] [--data-dir 临时目录]

`--data-dir` 指向临时目录时，预览用的是该目录下的 settings.json / models_cache.json，
真实数据目录 %APPDATA%\\WBRelay 零改动（与 tests/run_label_e2e.sh 同一套路）。
"""
import os
import sys
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 强制只读：paths 在导入期解析 argv，必须先于 wbrelay 导入追加该开关
if "--readonly" not in sys.argv:
    sys.argv.append("--readonly")

from wbrelay import state                  # noqa: E402
from wbrelay.paths import DATA, READONLY   # noqa: E402
from wbrelay.web.server import Handler     # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18899

if __name__ == "__main__":
    state._SERVER_PORT = PORT          # 避免 /api/state 读到未初始化的端口
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write("preview server on http://127.0.0.1:%d/  readonly=%s  data=%s\n"
                     % (PORT, READONLY, DATA))
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
