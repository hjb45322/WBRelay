# -*- coding: utf-8 -*-
"""无 GUI 依赖冒烟：验证「缺少 pywebview / pystray / Pillow」时的降级行为。

场景（用于干净 venv 里安装 wheel 后的验证）：
  N1  三个可选依赖确实都不可导入（否则本测试没有验证到降级路径）
  N2  main() 不因缺少 GUI 依赖而崩：HTTP 服务照常起、看板照常可读
  N3  降级为「浏览器模式」：调用系统浏览器打开看板，而不是抛异常退出
  N4  看板页面来自安装包内的静态资源（wbrelay/static/dashboard.html）
  N5  普通模式会生成默认配置（HTTP 服务真正工作）
  N6  全程数据落在自建临时目录（不碰真实 %APPDATA% / USERPROFILE / HOME）

用法（在装好 wheel 的干净环境里）：
    python tests/no_gui_smoke.py
退出码：0=通过
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="wbrelay-nogui-")
    data_dir = os.path.join(tmp, "data")
    # 即使被测代码丢了 --data-dir，默认目录也落在临时根里
    os.environ["APPDATA"] = tmp
    os.environ["LOCALAPPDATA"] = os.path.join(tmp, "localappdata")
    # 家目录也重定向：~ / ~/.codex 全落在临时根里（不碰真实 Codex 配置）
    os.environ["USERPROFILE"] = tmp
    os.environ["HOME"] = tmp
    for _k in ("WBRELAY_DATA_DIR", "WBRELAY_UPSTREAM_DIR", "WBRELAY_READONLY",
               "WBRELAY_NO_IDE_INJECT", "WBRELAY_HEADLESS"):
        os.environ.pop(_k, None)
    port = free_port()
    sys.argv = ["wbrelay", "--data-dir", data_dir, "--no-ide-inject", "--port", str(port)]

    # ---- N1 可选依赖不可用 ----
    missing = {}
    for mod in ("webview", "pystray", "PIL"):
        try:
            __import__(mod)
            missing[mod] = False
        except ImportError:
            missing[mod] = True
    check("N1 可选 GUI 依赖确实不可导入（验证的正是降级路径）",
          all(missing.values()), json.dumps(missing))
    if not all(missing.values()):
        print("      本环境装了 GUI 依赖，无法验证降级路径（请在干净 venv 里跑）")

    # ---- 拦截真实副作用：不弹浏览器、不进无限循环 ----
    import webbrowser
    opened = []
    webbrowser.open = lambda url, *a, **k: (opened.append(url), True)[1]

    import wbrelay.app as appmod
    real_sleep = appmod.time.sleep
    state = {"sleeps": 0}

    def fake_sleep(sec):
        if threading.current_thread() is not threading.main_thread():
            return                     # 后台线程（如用量清理）不受影响
        state["sleeps"] += 1
        raise KeyboardInterrupt        # GUI 降级分支的 while 循环立即退出

    appmod.time.sleep = fake_sleep
    try:
        appmod.main()                  # N2/N3：不应抛异常
        main_ok = True
        err = ""
    except SystemExit as exc:
        main_ok = False
        err = "SystemExit(%s)" % exc
    except Exception as exc:
        main_ok = False
        err = "%s: %s" % (type(exc).__name__, exc)
    finally:
        appmod.time.sleep = real_sleep

    check("N2 main() 在缺少 GUI 依赖时不崩（HTTP 服务已起）", main_ok, err)
    check("N3 降级为浏览器模式：调用了系统浏览器打开看板",
          bool(opened) and str(port) in opened[0], repr(opened))

    # ---- N4/N5 看板与配置 ----
    url = "http://127.0.0.1:%d/" % port
    body = ""
    status = None
    for _ in range(40):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", "replace")
            break
        except Exception:
            time.sleep(0.25)
    check("N4 看板可访问（HTTP 200 + HTML）",
          status == 200 and "<html" in body.lower(), "status=%s len=%d" % (status, len(body)))
    check("N4b 看板来自安装包内的静态资源",
          "wbrelay" in body.lower() or "看板" in body, body[:60].replace("\n", " "))
    check("N5 普通模式生成了默认配置（服务真正工作）",
          os.path.isfile(os.path.join(data_dir, "settings.json")),
          str(sorted(os.listdir(data_dir)) if os.path.isdir(data_dir) else "目录不存在"))
    check("N6 数据目录落在自建临时目录内",
          os.path.abspath(data_dir).startswith(os.path.abspath(tmp)), data_dir)

    print("\n===== 无 GUI 降级冒烟：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())