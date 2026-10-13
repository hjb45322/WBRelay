# -*- coding: utf-8 -*-
"""路径与运行模式解析。

本模块必须在其他模块之前求值：数据目录、隔离开关、只读模式都由这里决定，
且这些值在导入期就定下来了（详见 docs/方案-隔离机制.md）。

**导入期零写入**：本模块只做解析与只读探测，不创建任何目录、不写任何文件。
需要数据目录的写入原语各自调用 `ensure_data_dir()`（只读模式下它不创建目录）。
"""
import os
import sys

from . import __version__


_PKG_DIR = os.path.dirname(os.path.abspath(__file__))

# frozen（PyInstaller 打包）时，DIR 为 EXE 所在目录（仅用于旧数据迁移探测）；
# 源码运行时 DIR 为「包的上一级」= 项目根目录（relay.py 所在处）。
if getattr(sys, "frozen", False):
    DIR = os.path.dirname(sys.executable)   # EXE 所在目录
    RES = getattr(sys, "_MEIPASS", DIR)     # 只读资源解包位置
else:
    DIR = os.path.dirname(_PKG_DIR)         # 项目根目录
    RES = DIR
# ---------------------------------------------------------------------------
# 隔离机制（必须在任何路径常量之前求值，详见 docs/方案-隔离机制.md）
# ---------------------------------------------------------------------------
APP_DIR_NAME = "WBRelay"          # 本方数据目录名（2026-10-11 身份接管，原 WBRelay-OSS）
# 接管前后用过的目录名：属历史/归档目录，指过去时必须显式 --allow-legacy-data
GUARDED_DIR_NAMES = ("WBRelay-OSS",)
GUARDED_DIR_PREFIXES = ("WBRelay-legacy",)


def _argv_has(flag):
    return flag in sys.argv


def _argv_value(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return ""


NO_IDE_INJECT = _argv_has("--no-ide-inject") or os.environ.get("WBRELAY_NO_IDE_INJECT") == "1"
READONLY = _argv_has("--readonly") or os.environ.get("WBRELAY_READONLY") == "1"
ALLOW_LEGACY_DATA = _argv_has("--allow-legacy-data")
HEADLESS = _argv_has("--headless") or os.environ.get("WBRELAY_HEADLESS") == "1"


def _resolve_data_dir():
    """优先级：--data-dir > WBRELAY_DATA_DIR > APPDATA/APP_DIR_NAME"""
    raw = _argv_value("--data-dir") or os.environ.get("WBRELAY_DATA_DIR") or ""
    if raw:
        return os.path.abspath(os.path.expanduser(raw)), "命令行 --data-dir"
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME), "默认"


DATA, _DATA_SOURCE = _resolve_data_dir()

# 安全阀①：拒绝打开历史/归档数据目录（接管前的 WBRelay-OSS、归档的 WBRelay-legacy-*）
_appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
_guarded = [os.path.join(_appdata, _n) for _n in GUARDED_DIR_NAMES]
try:
    for _entry in os.listdir(_appdata):
        if _entry.startswith(GUARDED_DIR_PREFIXES):
            _guarded.append(os.path.join(_appdata, _entry))
except OSError:
    pass
_norm_data = os.path.normcase(os.path.abspath(DATA))
_base_name = os.path.basename(os.path.abspath(DATA))
if not ALLOW_LEGACY_DATA:
    _hit = None
    # ① 按目录名判定（覆盖尚未创建、或不在 %APPDATA% 下的同名目录）
    if _base_name in GUARDED_DIR_NAMES or _base_name.startswith(GUARDED_DIR_PREFIXES):
        _hit = os.path.abspath(DATA)
    # ② 再按 %APPDATA% 下实际存在的历史/归档目录逐项比对
    if _hit is None:
        for _g in _guarded:
            if _norm_data == os.path.normcase(os.path.abspath(_g)):
                _hit = _g
                break
    if _hit:
        raise SystemExit(
            "[WBRelay] 拒绝启动：数据目录指向历史/归档目录 %s\n"
            "          该目录已不再由本项目维护。确需操作请显式加 --allow-legacy-data。"
            % _hit)

# 安全阀②：数据目录必须是绝对路径
if not os.path.isabs(DATA):
    raise SystemExit("[WBRelay] 拒绝启动：数据目录必须是绝对路径：%s" % DATA)

# ---------------------------------------------------------------------------
# 数据目录：**不在导入期创建**（只读模式必须零写入、零目录创建）
# ---------------------------------------------------------------------------
_data_dir_error = ""


def ensure_data_dir():
    """按需创建数据目录。返回数据目录路径；不可用（只读 / 创建失败）返回 None。

    只读模式（--readonly）直接返回 None 且不创建任何目录 —— 只读必须零写入。
    写入原语（settings / profiles / usage / backup / catalog / log ...）统一走这里，
    因此即使将来新增调用点，也只读模式也写不进任何文件。
    """
    global _data_dir_error
    if READONLY:
        _data_dir_error = "只读模式：不创建数据目录"
        return None
    try:
        os.makedirs(DATA, exist_ok=True)
    except Exception as exc:
        _data_dir_error = str(exc)
        return None
    _data_dir_error = ""
    return DATA


def data_dir_error():
    """最近一次 ensure_data_dir() 失败原因（用于启动期给出明确报错）。"""
    return _data_dir_error


SETTINGS_PATH = os.path.join(DATA, "settings.json")
USAGE_PATH = os.path.join(DATA, "usage.jsonl")
MODELS_CACHE_PATH = os.path.join(DATA, "models_cache.json")
PROFILES_PATH = os.path.join(DATA, "model_profiles.json")
BACKUP_DIR = os.path.join(DATA, "backups")
LOG_PATH = os.path.join(DATA, "wb.log")


def _resolve_dashboard():
    """看板资源位置：兼容「仓库根」「包内 static」「PyInstaller 解包目录」三种布局。"""
    candidates = [
        os.path.join(RES, "dashboard.html"),                        # 旧打包布局 / 仓库根
        os.path.join(RES, "wbrelay", "static", "dashboard.html"),   # 新版 PyInstaller datas
        os.path.join(_PKG_DIR, "static", "dashboard.html"),         # wheel / 源码安装
        os.path.join(DIR, "dashboard.html"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    # 一个都不存在：返回包内期望位置，由 HTTP 层给出「资源缺失」的明确报错
    return candidates[2]


DASHBOARD_PATH = _resolve_dashboard()
DEFAULT_PORT = 8789
WBRELAY_VERSION = __version__
MAX_PAYLOAD = 8 * 1024 * 1024
MODEL_FETCH_TIMEOUT = 30