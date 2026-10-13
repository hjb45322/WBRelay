# -*- coding: utf-8 -*-
"""程序入口：单实例检查、端口选择、后台线程与窗口/托盘。"""
import atexit
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

from . import state
from . import windowstate
from .atomicio import write_bytes
from .paths import DATA, DIR, _DATA_SOURCE
from .redact import redact
from .autostart import (set_gateway_autostart)
from .ides import (ides_backup_and_inject, ides_restore)
from .log import (_log)
from .paths import (DEFAULT_PORT, HEADLESS, NO_IDE_INJECT, READONLY, SETTINGS_PATH,
                    data_dir_error, ensure_data_dir)
from .routing import (route_table)
from .settings import (configured_port, get_suppliers, load_settings, require_api_key, save_settings)
from .usage import (_retention_loop, enforce_usage_retention)
from .web.server import (Handler)


class _GatewayServer(ThreadingHTTPServer):
    """本网关专用 HTTP 服务线程。

    与裸 `ThreadingHTTPServer` 的差别只有两处，且都只影响**异常与退出路径**：

    1. `daemon_threads = True`：进程退出时不再等待残留的请求线程（长连接 / 半途
       断开的客户端不会把进程卡在关机路径上）。
    2. `handle_error()` 收敛：`BaseServer` 的默认实现会把**完整 traceback** 写到
       stderr，源码模式下泄漏内部绝对路径与调用栈，`--noconsole` 的 EXE 模式下这些
       输出又直接丢失（排障时反而看不到）。这里改为：客户端主动断开不算异常、
       静默忽略；其余异常只记「类型 + 脱敏后的消息」——异常文本可能夹带 URL 凭据，
       所以仍走 `redact()` 与统一 `_log()` 出口，不打印堆栈。
    """
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        # 客户端主动断开是正常情况（Codex 取消请求 / 浏览器切页），不记录
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        try:
            detail = redact(exc, 200)
        except Exception:
            detail = "<unprintable>"
        # 不打印完整堆栈：只记类型 + 脱敏后的消息
        _log("[WBRelay] HTTP handler 异常 %s: %s" % (type(exc).__name__, detail))


def _write_window_icon(ico_bytes, data_dir=None, readonly=None):
    """把窗口图标原子写进数据目录；**只读模式不创建、不覆写**。

    返回写入的路径；只读模式 / 数据目录不可用 / 写盘失败时返回空串（调用方据此跳过
    窗口图标设置，而不是反复重试一个不存在的文件）。
    图标文件走统一原子写工具（tmp + os.replace），不再直接 `open(path, "wb")` 覆写。
    """
    data_dir = DATA if data_dir is None else data_dir
    if readonly is None:
        readonly = READONLY
    path = os.path.join(data_dir, "WBRelay.ico")
    if readonly:
        _log("[WBRelay] 只读模式：不生成窗口图标文件（%s）" % path)
        return ""
    if data_dir == DATA and ensure_data_dir() is None:
        _log("[WBRelay] 窗口图标未写入：数据目录不可用（%s）" % data_dir_error())
        return ""
    try:
        write_bytes(path, ico_bytes)
    except Exception as exc:
        _log("[WBRelay] 窗口图标写入失败（本次不设置窗口图标）：%s" % exc)
        return ""
    return path


def _migrate_legacy_data():
    """把旧版散落在 EXE/项目目录的数据迁移到固定用户数据目录（仅当新位置无同名文件时）。"""
    moved = []
    # 候选旧位置：1) DIR\data\   2) DIR 根目录（更早版本散落）
    for base in (os.path.join(DIR, "data"), DIR):
        for f in ("settings.json", "usage.jsonl", "models_cache.json", "model_profiles.json",
                  "codex_original_config.json", "WBRelay.ico"):
            old = os.path.join(base, f)
            new = os.path.join(DATA, f)
            if os.path.exists(old) and not os.path.exists(new):
                try:
                    os.replace(old, new)
                    moved.append(f)
                except Exception:
                    pass
        old_b = os.path.join(base, "backups")
        new_b = os.path.join(DATA, "backups")
        if os.path.isdir(old_b) and not os.path.isdir(new_b):
            try:
                os.makedirs(new_b, exist_ok=True)
                for fn in os.listdir(old_b):
                    os.replace(os.path.join(old_b, fn), os.path.join(new_b, fn))
                os.rmdir(old_b)
                moved.append("backups/")
            except Exception:
                pass
    if moved:
        _log("[WBRelay] 数据已迁移到 %s：%s" % (DATA, ", ".join(moved)))
    # 旧版 codex_original_config.json → ide_backups/codex.json
    old_codex_bak = os.path.join(DATA, "codex_original_config.json")
    new_codex_bak = os.path.join(DATA, "ide_backups", "codex.json")
    if os.path.isfile(old_codex_bak) and not os.path.isfile(new_codex_bak):
        try:
            os.makedirs(os.path.dirname(new_codex_bak), exist_ok=True)
            os.replace(old_codex_bak, new_codex_bak)
            _log("[WBRelay] 旧版 Codex 备份已迁移到 ide_backups/codex.json")
        except Exception:
            pass
    return moved


def main():
    if not READONLY and ensure_data_dir() is None:
        raise SystemExit("[WBRelay] 无法创建数据目录 %s：%s" % (DATA, data_dir_error()))
    if not READONLY:
        _migrate_legacy_data()
    _log("[WBRelay] 数据目录(%s): %s" % (_DATA_SOURCE, DATA))
    if READONLY:
        _log("[WBRelay] 运行模式：只读（管理接口写入、后台线程、IDE 接管均已禁用）")
    elif NO_IDE_INJECT:
        _log("[WBRelay] 运行模式：禁止接管 IDE（--no-ide-inject）")
    # 首次运行引导：缺 settings.json 时生成默认配置（打包 EXE 首次启动必走此分支）
    if not READONLY and not os.path.exists(SETTINGS_PATH):
        save_settings({
            "port": DEFAULT_PORT,
            "default_timeout_sec": 120,
            "cooldown_sec": 60,
            "cooldown_429_sec": 30,
            "usage_retention_days": 0,
            "usage_refresh_sec": 5,
            "require_api_key": False,
            "api_keys": [],
            "suppliers": [],
            "model_prices": {},
            "models_enabled": {},
            "group_models_enabled": {},
            "upstream_enabled": True,
            "autostart": False,
        })
        _log("[WBRelay] 首次运行：已生成默认 %s" % SETTINGS_PATH)
    port = configured_port() or DEFAULT_PORT
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (ValueError, IndexError):
            port = configured_port() or DEFAULT_PORT
    state._SERVER_PORT = port
    # 路由表预热：触发旧 models_enabled → 组级迁移，并按当前配置构建路由表
    try:
        route_table()
    except Exception:
        pass
    # 开机自启动：若已开启，启动时重写注册表启动项指向当前可执行文件（EXE 升级/换目录后自动修复）
    try:
        if not READONLY and load_settings().get("autostart"):
            set_gateway_autostart(True)
    except Exception:
        pass
    if READONLY:
        _log("[WBRelay] 只读模式：不启动任何后台写线程（用量清理），不执行用量保留")
    else:
        # R5：不启动任何「自动拉起外部进程」的守护线程；只保留本项目自身的用量清理
        threading.Thread(target=_retention_loop, daemon=True).start()
        try:
            removed = enforce_usage_retention()
            if removed:
                _log("[WBRelay] 用量保留策略已移除 %d 条过期记录" % removed)
        except Exception:
            pass
    # 端口占用检测：自身多开提示后退出；其他进程占用给出提示（可用 --port 覆盖）
    def _port_owner(p):
        try:
            out = subprocess.check_output(["netstat", "-ano"], text=True, errors="ignore",
                                          creationflags=state._NO_WINDOW)
            for line in out.splitlines():
                if (":%d " % p) in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        try:
                            who = subprocess.check_output(
                                ["tasklist", "/FI", "PID eq %s" % pid], text=True,
                                errors="ignore", creationflags=state._NO_WINDOW)
                            for wl in who.splitlines():
                                if pid in wl and "Image Name" not in wl and "映像名称" not in wl:
                                    return pid, wl.split()[0]
                        except Exception:
                            pass
                        return pid, ""
        except Exception:
            pass
        return None, ""
    # 端口占用处理：被其他进程占用 → 自动顺延到空闲端口（Codex 配置自动写入实际端口，无需人工干预）
    def _port_busy(p):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", p))
            s.close()
            return False
        except OSError:
            return True

    def _port_is_wbrelay(p):
        """该端口是否已经在跑本网关：`/api/state` 能拿到 suppliers 就算。

        只看进程名会误判：源码模式跑的是 python.exe，名字不以 wb 开头，
        会被当成「其他进程」而顺延端口 + 改写 Codex 配置。
        """
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % p, timeout=1.5) as resp:
                head = resp.read(4096).decode("utf-8", "replace")
            return '"suppliers"' in head
        except Exception:
            return False

    owner_pid, owner_name = _port_owner(port)
    if owner_pid:
        # 自身多开判定：① 进程名以 wb 开头（打包版 WBRelay.exe）
        #               ② 或该端口确实在提供本网关 API（源码模式）
        if (owner_name and owner_name.lower().startswith("wb")) or _port_is_wbrelay(port):
            # 退出防双实例抢数据 / 抢 Codex 配置
            _log("[WBRelay] 端口 %d 已被另一个 WBRelay 占用（PID %s %s），可能已在运行，退出。"
                 % (port, owner_pid, owner_name or "?"))
            sys.exit(0)
        _log("[WBRelay] 端口 %d 被其他进程占用（PID %s %s），自动改用空闲端口"
             % (port, owner_pid, owner_name or "?"))
        while _port_busy(port):
            port += 1
        state._SERVER_PORT = port
        _log("[WBRelay] 已改用端口 %d（Codex 配置将自动写入）" % port)
    server = _GatewayServer(("127.0.0.1", port), Handler)
    _log("[WBRelay] listening  : http://127.0.0.1:%d/v1  (api key 校验: %s)"
          % (port, "开" if require_api_key() else "关"))
    _log("[WBRelay] dashboard  : http://127.0.0.1:%d/" % port)
    _log("[WBRelay] suppliers  : %d 个（%d 启用）"
          % (len(get_suppliers()), sum(1 for s in get_suppliers() if s["enabled"])))
    # IDE 自动接管：启动时备份并注入各 IDE 配置，关闭时恢复
    try:
        ides_backup_and_inject(port)
    except Exception as exc:
        _log("[WBRelay] IDE 接管异常: %s" % exc)
    atexit.register(ides_restore)
    def _on_exit(signum, frame):
        ides_restore()
        sys.exit(0)
    for _sig in (signal.SIGINT, signal.SIGBREAK, getattr(signal, "SIGTERM", None)):
        if _sig:
            try:
                signal.signal(_sig, _on_exit)
            except Exception:
                pass

    # HTTP server 在后台线程跑
    threading.Thread(target=server.serve_forever, daemon=True).start()

    if HEADLESS:
        _log("[WBRelay] headless 模式：不创建窗口与托盘，Ctrl+C 退出")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            ides_restore()
        return

    # 独立窗口（pywebview）+ 系统托盘（pystray）：可选依赖，缺失时降级为浏览器模式
    try:
        import webview
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        url = "http://127.0.0.1:%d/" % port
        _log("[WBRelay] 未安装可选 GUI 依赖（pywebview / pystray / Pillow）：%s" % exc)
        _log("[WBRelay] 已降级为浏览器模式：HTTP 服务与看板照常可用")
        _log("[WBRelay] 需要独立窗口/托盘时执行：pip install wbrelay-gateway[gui]")
        _log("[WBRelay] 看板地址：%s （Ctrl+C 退出）" % url)
        try:
            webbrowser.open(url)
        except Exception as open_exc:
            _log("[WBRelay] 自动打开浏览器失败（请手动访问上面的地址）：%s" % open_exc)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            ides_restore()
        return

    def _make_icon():
        from PIL import Image, ImageDraw
        # 方案7「六边形中继」：渐变底 + 中继器符号（主体撑满，托盘 16px 清晰）
        S = 256
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        c0 = (10, 30, 79)     # #0A1E4F 左上深藏蓝
        c1 = (27, 167, 230)   # #1BA7E6 右下青蓝
        px = img.load()
        for y in range(S):
            for x in range(S):
                t = (x + y) / (2 * (S - 1))
                px[x, y] = (int(c0[0] + (c1[0] - c0[0]) * t),
                            int(c0[1] + (c1[1] - c0[1]) * t),
                            int(c0[2] + (c1[2] - c0[2]) * t), 255)
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle((8, 8, 248, 248), radius=56, fill=255)
        img.putalpha(mask)
        d = ImageDraw.Draw(img)
        WHITE = (255, 255, 255, 255)
        # 中央六边形（网关中继器）
        d.polygon([(78, 128), (103, 85), (153, 85), (178, 128), (153, 171), (103, 171)], fill=WHITE)
        # 上下切换箭头
        d.polygon([(128, 40), (106, 80), (150, 80)], fill=WHITE)
        d.polygon([(128, 216), (106, 176), (150, 176)], fill=WHITE)
        # 左右转发链路 + 端点圆点
        d.line([(78, 128), (43, 128)], fill=WHITE, width=14)
        d.line([(178, 128), (213, 128)], fill=WHITE, width=14)
        d.ellipse([(21, 117), (43, 139)], fill=WHITE)
        d.ellipse([(213, 117), (235, 139)], fill=WHITE)
        return img

    def _make_ico_bytes(img, sizes):
        """手工构造标准多尺寸 ICO（32bpp BMP 帧）。
        规避 Pillow 11 保存 ICO 只写 256 PNG 帧、导致 Windows 渲染异常的兼容问题。"""
        import struct as _struct
        entries, datas = [], []
        offset = 6 + 16 * len(sizes)
        for s in sorted(set(sizes)):
            im = img.resize((s, s), Image.LANCZOS)
            w = h = s
            xor = bytearray()
            for y in range(h - 1, -1, -1):  # BMP 自底向上
                for x in range(w):
                    r, g, b, a = im.getpixel((x, y))
                    xor += _struct.pack('<BBBB', b, g, r, a)
            and_stride = ((w + 31) // 32) * 4
            and_mask = b"\x00" * (and_stride * h)
            header = _struct.pack('<IiiHHIIiiII', 40, w, h * 2, 1, 32, 0,
                                  len(xor) + len(and_mask), 0, 0, 0, 0)
            data = header + bytes(xor) + and_mask
            ew = 0 if w == 256 else w
            eh = 0 if h == 256 else h
            entries.append(_struct.pack('<BBBBHHII', ew, eh, 0, 0, 1, 32, len(data), offset))
            datas.append(data)
            offset += len(data)
        return _struct.pack('<HHH', 0, 1, len(entries)) + b"".join(entries) + b"".join(datas)

    icon_img = _make_icon()
    # 保存 ICO 到数据目录（窗口标题栏图标）：只读模式下跳过，绝不写盘
    try:
        ico_bytes = _make_ico_bytes(icon_img, [16, 32, 48, 64, 256])
    except Exception as exc:
        _log("[WBRelay] 窗口图标构造失败（本次不设置窗口图标）：%s" % exc)
        ico_bytes = b""
    ico_path = _write_window_icon(ico_bytes) if ico_bytes else ""

    # 创建独立窗口，加载看板
    # 注：pywebview 6.x 的 create_window 不支持 icon 参数（Windows 标题栏图标仅 GTK/QT 后端支持），
    # 因此窗口图标改用 Win32 API（标准库 ctypes）在窗口出现后设置；托盘图标由 pystray 使用 _make_icon()。
    # 窗口几何记忆：拖拽调整过的尺寸/位置/最大化状态落盘，下次启动照原样打开
    geom = windowstate.load(screen=windowstate.screen_size())
    window = webview.create_window("WBRelay · 多供应商自动路由", "http://127.0.0.1:%d/" % port,
                                   width=geom["width"], height=geom["height"],
                                   x=geom.get("x"), y=geom.get("y"),
                                   maximized=geom.get("maximized", False),
                                   min_size=(windowstate.MIN_WIDTH, windowstate.MIN_HEIGHT),
                                   resizable=True)
    _log("[WBRelay] 窗口几何：%dx%d%s%s"
         % (geom["width"], geom["height"],
            (" 位于 %d,%d" % (geom["x"], geom["y"])) if "x" in geom else "",
            "（最大化）" if geom.get("maximized") else ""))

    # 拖拽期间 resized/moved 会连续触发：合并成 0.8 秒一次落盘，避免频繁写盘
    _geom = dict(geom)
    _geom_timer = {"t": None}

    def _save_geometry():
        if windowstate.save(_geom):
            _log("[WBRelay] 窗口几何已保存：%dx%d%s"
                 % (_geom["width"], _geom["height"],
                    (" 位于 %d,%d" % (_geom["x"], _geom["y"])) if "x" in _geom else ""))

    def _queue_save_geometry():
        timer = _geom_timer.get("t")
        if timer:
            timer.cancel()
        timer = threading.Timer(0.8, _save_geometry)
        timer.daemon = True
        _geom_timer["t"] = timer
        timer.start()

    def _on_resized(width, height):
        _geom["width"], _geom["height"] = int(width), int(height)
        _queue_save_geometry()

    def _on_moved(x, y):
        _geom["x"], _geom["y"] = int(x), int(y)
        _queue_save_geometry()

    def _on_maximized():
        _geom["maximized"] = True
        _queue_save_geometry()

    def _on_restored():
        _geom["maximized"] = False
        _queue_save_geometry()

    try:
        window.events.resized += _on_resized
        window.events.moved += _on_moved
        window.events.maximized += _on_maximized
        window.events.restored += _on_restored
    except Exception as exc:
        _log("[WBRelay] 窗口几何事件绑定失败（本次不记忆尺寸）：%s" % exc)

    # 窗口图标（标题栏 + 任务栏）
    def _apply_window_icon(attempt=0):
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            pid = kernel32.GetCurrentProcessId()
            WM_SETICON, IMAGE_ICON = 0x0080, 1
            LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040
            user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                          ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.LoadImageW.restype = wintypes.HANDLE
            user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            hicon = user32.LoadImageW(None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if not hicon:
                raise OSError("LoadImageW 返回空句柄")
            found = []
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def _enum_cb(hwnd, lparam):
                wpid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                if wpid.value == pid and user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                return True

            cb = WNDENUMPROC(_enum_cb)
            user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
            user32.EnumWindows(cb, 0)
            if found:
                for hwnd in found:
                    user32.SendMessageW(hwnd, WM_SETICON, 0, hicon)  # 标题栏小图标
                    user32.SendMessageW(hwnd, WM_SETICON, 1, hicon)  # 任务栏图标
                _log("[WBRelay] 窗口图标已应用 (%d 个窗口)" % len(found))
                return
        except Exception as exc:
            _log("[WBRelay] 窗口图标设置失败: %s" % exc)
        # 窗口尚未就绪，稍后重试（最多 30 秒；pywebview 窗口异步创建）
        if attempt < 30:
            threading.Timer(1.0, _apply_window_icon, args=[attempt + 1]).start()

    # 窗口创建后轮询应用图标（等窗口句柄就绪）；只读模式下没有 ICO 文件，直接跳过
    if ico_path:
        threading.Timer(1.0, _apply_window_icon).start()

    # 关闭窗口时隐藏到托盘（不退出）
    def _on_closing():
        _save_geometry()      # 关到托盘前先落盘，避免刚拖完就关、去抖还没触发
        window.hide()
        return False  # 阻止默认关闭

    window.events.closing += _on_closing

    # 托盘菜单
    def _tray_open(icon, item):
        # 左键单击（default=True）/ 双击 / 右键菜单均走这里：打开看板窗口
        try:
            window.show()
        except Exception as exc:
            # 兜底：窗口不可用（被销毁/初始化失败）时改用系统浏览器打开看板
            _log("[WBRelay] 托盘打开窗口失败（%s），改用系统浏览器打开看板" % exc)
            webbrowser.open("http://127.0.0.1:%d/" % port)

    def _tray_quit(icon, item):
        _save_geometry()
        icon.stop()
        server.shutdown()
        ides_restore()
        os._exit(0)

    tray = pystray.Icon("WBRelay", _make_icon(), "WBRelay · 多供应商自动路由",
                        menu=pystray.Menu(
                            # default=True：Windows 左键单击托盘图标直接触发「打开看板」
                            pystray.MenuItem("打开看板", _tray_open, default=True),
                            pystray.MenuItem("退出", _tray_quit),
                        ))
    tray.on_double_click = lambda icon, item: _tray_open(icon, item)
    threading.Thread(target=tray.run, daemon=True).start()

    _log("[WBRelay] 独立窗口已启动，关闭窗口最小化到托盘")
    try:
        webview.start()
    finally:
        ides_restore()


if __name__ == "__main__":
    main()
