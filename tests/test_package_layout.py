# -*- coding: utf-8 -*-
"""打包布局与发布产物的静态检查（不构建、不联网、零写入）。

覆盖：
  P1  wbrelay/static/dashboard.html 与仓库根 dashboard.html 逐字节一致（防副本漂移）
  P2  pyproject 的包发现覆盖全部子包（wbrelay.web / .ides / .integrations）
  P3  静态资源声明（package-data）覆盖包内 dashboard.html
  P4  子包都有 __init__.py（否则 wheel 里会缺包）
  P5  paths.DASHBOARD_PATH 指向真实存在的看板文件
  P6  PyInstaller spec 不含任何用户运行数据（配置/用量/日志/备份/截图/EXE）
  P7  pyproject 不含 example.invalid 之类占位 URL
  P8  核心依赖为零（只用标准库）；GUI 依赖只能是可选 extras

用法：python tests/test_package_layout.py
"""
import io
import os
import re
import subprocess
import sys
import tempfile

def _out(text):
    """GBK 控制台下 UnicodeEncodeError 的安全输出（Python 3.9 兼容）。"""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        try:
            print(text.encode(enc, "replace").decode(enc, "replace"))
        except Exception:
            print(text.encode("ascii", "replace").decode("ascii", "replace"))


def _child_env():
    """子进程固定 UTF-8 输出：否则 GBK 控制台下的子进程吐 GBK 字节，
    父进程按 UTF-8 解码得到乱码，中文关键字断言会假失败。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


import isolate

isolate.bootstrap(readonly=True)

from wbrelay.paths import DASHBOARD_PATH   # noqa: E402

PASS, FAIL = [], []
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_HTML = os.path.join(ROOT, "dashboard.html")
PKG_HTML = os.path.join(ROOT, "wbrelay", "static", "dashboard.html")
PYPROJECT = os.path.join(ROOT, "pyproject.toml")
SPEC = os.path.join(ROOT, "WBRelay.spec")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    _out("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def read(path):
    with io.open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def main():
    # ---- P1 包内副本与源文件一致 ----
    src = open(SRC_HTML, "rb").read()
    pkg = open(PKG_HTML, "rb").read() if os.path.isfile(PKG_HTML) else None
    check("P1a 包内 dashboard.html 存在", pkg is not None, PKG_HTML)
    check("P1b 包内副本与源文件逐字节一致（漂移会被本测试拦住）", pkg == src,
          "源 %d 字节 / 包内 %s" % (len(src), "缺失" if pkg is None else "%d 字节" % len(pkg)))
    if pkg != src:
        _out("      修复：python tools/sync_dashboard.py")

    pyproject = read(PYPROJECT)
    # ---- P2 包发现 ----
    check("P2a pyproject 使用 packages.find（不再写死 single package）",
          "[tool.setuptools.packages.find]" in pyproject, "")
    check("P2b find.include 覆盖 wbrelay*",
          re.search(r'include\s*=\s*\[\s*"wbrelay\*"\s*\]', pyproject) is not None)
    check("P2c 不再出现 packages = [\"wbrelay\"] 这种漏子包的写法",
          'packages = ["wbrelay"]' not in pyproject)

    # ---- P3 静态资源 ----
    check("P3a package-data 声明了 static/*.html",
          re.search(r'\[tool\.setuptools\.package-data\][\s\S]*?wbrelay\s*=\s*\[\s*"static/\*\.html"\s*\]',
                    pyproject) is not None)

    # ---- P4 子包 __init__ ----
    missing = [p for p in ("web", "ides", "integrations")
               if not os.path.isfile(os.path.join(ROOT, "wbrelay", p, "__init__.py"))]
    check("P4 子包 web / ides / integrations 都有 __init__.py", missing == [], repr(missing))

    # ---- P5 看板资源可解析 ----
    check("P5 paths.DASHBOARD_PATH 指向真实文件", os.path.isfile(DASHBOARD_PATH),
          DASHBOARD_PATH)
    check("P5b 解析到的看板与源文件一致", os.path.isfile(DASHBOARD_PATH) and
          open(DASHBOARD_PATH, "rb").read() == src, DASHBOARD_PATH)

    # ---- P6 spec 不含用户运行数据 ----
    spec = read(SPEC)
    forbidden = ("settings.json", "usage.jsonl", "wb.log", "*.log", "backups",
                 "ide_backups", "models_cache.json", "model_profiles.json",
                 "window.json", "shots", "截图")
    hits = [f for f in forbidden if f in spec]
    check("P6a spec 只打包 dashboard.html（无用户数据 / 无截图）",
          hits == [], "命中：%s" % hits)
    check("P6b spec 的 datas 仅含 dashboard.html",
          re.search(r"datas=\[\('dashboard\.html', '\.'\)\]", spec) is not None,
          spec.strip().splitlines()[0] if spec else "")

    # ---- P7 无占位 URL ----
    check("P7 pyproject 不含 example.invalid 占位地址",
          "example.invalid" not in pyproject)
    check("P7b pyproject 不含其它占位域名",
          not re.search(r"https?://(example|invalid|localhost)", pyproject, re.I))

    # ---- P8 依赖边界 ----
    core_deps = re.search(r"^dependencies\s*=", pyproject, re.M)
    check("P8a [project] 未声明强制依赖（核心只用标准库）", core_deps is None)
    check("P8b GUI 依赖在可选 extras（gui）里",
          re.search(r"\[project\.optional-dependencies\][\s\S]*?gui\s*=\s*\[[^\]]*pywebview", pyproject)
          is not None)
    check("P8c requires-python 仍为 >=3.9", 'requires-python = ">=3.9"' in pyproject)

    # ---- P9 发布副本生成器（dry-run，零写入）----
    copier = os.path.join(ROOT, "tools", "make_publish_copy.py")
    excludes = os.path.join(ROOT, "tools", "publish_excludes.txt")
    check("P9a 发布副本生成器与排除清单都在", os.path.isfile(copier) and os.path.isfile(excludes))
    inside = os.path.join(ROOT, ".publish-probe")
    r = subprocess.run([sys.executable, copier, "--dest", inside],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=_child_env())
    out = (r.stdout or "") + (r.stderr or "")
    check("P9b 拒绝把发布副本写进本仓库内部",
          r.returncode != 0 and "本仓库内部" in out, out.strip()[:80])
    check("P9c 被拒绝时没有写任何东西", not os.path.exists(inside))
    dest = os.path.join(tempfile.gettempdir(), "wbrelay-publish-probe-dry")
    r2 = subprocess.run([sys.executable, copier, "--dest", dest, "--excludes", excludes],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        env=_child_env())
    out2 = (r2.stdout or "") + (r2.stderr or "")
    check("P9d dry-run 打印计划且 exit=0", r2.returncode == 0 and "dry-run" in out2,
          out2.strip().splitlines()[-1][:80] if out2.strip() else "")
    check("P9e dry-run 不创建目标目录", not os.path.exists(dest))
    check("P9f dry-run 列出排除数与将复制的文件数",
          "排除掉" in out2 and "将复制" in out2)
    check("P9f2 内部资料目录不会进入发布计划",
          "_internal" not in out2.replace("\\", "/"))
    check("P9g 明确声明不带 .git", ".git 不带" in out2)

    _out("\n===== 打包布局：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        _out("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
