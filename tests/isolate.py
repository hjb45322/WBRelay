# -*- coding: utf-8 -*-
"""测试隔离引导层：让每个测试都在「自己创建的临时数据目录」里跑。

用法（**必须在使用 wbrelay 之前调用**，因为 `wbrelay.paths` 在导入期就定死了数据目录）：

    import isolate
    isolate.bootstrap()                 # 建临时根 + 改 sys.argv/env
    from wbrelay.paths import DATA      # 此刻 DATA 已指向临时目录
    isolate.assert_isolated()           # 自动断言：DATA 必须在临时根内

`bootstrap()` 做的事：
  1) `tempfile.mkdtemp()` 建专属临时根（测试结束自动删除）
  2) 把 `APPDATA` / `LOCALAPPDATA` / `USERPROFILE` / `HOME` 指向该临时根 —— 即使有代码路径
     回落到「默认数据目录」或 `~/.codex`，也会落在临时根里，碰不到真实 `%APPDATA%`
  3) 清掉 `WBRELAY_DATA_DIR` / `WBRELAY_UPSTREAM_DIR` 与 `WBRELAY_READONLY` /
     `WBRELAY_NO_IDE_INJECT` / `WBRELAY_HEADLESS` —— 后三者会改变运行模式，
     测试的模式必须只由测试自己的参数决定（避免外部环境污染）
  4) 重置 `sys.argv`：`--data-dir <临时>/data --no-ide-inject --port <空闲端口> --headless`
  5) 注册 atexit 清理，并暴露 `assert_isolated()` 供测试断言

设计约束：只依赖标准库；不读、不复制任何真实数据（真实配置里的 Key / 供应商名一律不进来）。
隔离断言**只基于路径规则与本轮临时目录**：不枚举、不读取任何受保护对象的内容（包括
只读列目录名）。
"""
import atexit
import os
import shutil
import socket
import sys
import tempfile

# ---- 控制台编码降级 ----
# 中文 Windows 是 GBK，英文 Windows / GitHub Actions 是 cp1252。编码装不下中文时
# 按 errors="replace" 输出 —— 别让一次 print 把脚本自己打崩（与 tests/ 里 _out() 同源）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TMP_ROOT = None
_DATA_DIR = None
_PORT = None

# 受保护对象名（**只用于断言「没指过去」，不做任何读写**）
PROTECTED_DIR_NAMES = ("WBRelay", "WBRelay-OSS", ".codex")
PROTECTED_DIR_PREFIXES = ("WBRelay-legacy",)

# 真实用户目录（仅用于拼出「禁止指向」的路径规则，不做目录枚举）
_REAL_APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~")
_REAL_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
_REAL_USERPROFILE = os.environ.get("USERPROFILE") or os.path.expanduser("~")
_REAL_HOME = os.environ.get("HOME") or os.path.expanduser("~")

_ENV_KEYS = ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME",
             "DSH_HOME", "CRUSH_GLOBAL_DATA", "CRUSH_GLOBAL_CONFIG", "XDG_DATA_HOME",
             "WBRELAY_DATA_DIR", "WBRELAY_UPSTREAM_DIR",
             "WBRELAY_READONLY", "WBRELAY_NO_IDE_INJECT", "WBRELAY_HEADLESS")
_ENV_BACKUP = {}
_TMP_PREFIX = "wbrelay-test-"


def free_port():
    """要一个空闲回环端口（避免固定端口撞车；调用方仍不得使用 8789）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def bootstrap(readonly=False, port=None, extra_args=(), keep_argv=False):
    """在临时根里初始化隔离环境。返回临时数据目录路径。可重复调用（幂等）。"""
    global _TMP_ROOT, _DATA_DIR, _PORT
    if _TMP_ROOT is None:
        _TMP_ROOT = tempfile.mkdtemp(prefix=_TMP_PREFIX)
        _DATA_DIR = os.path.join(_TMP_ROOT, "data")
        for k in _ENV_KEYS:
            _ENV_BACKUP[k] = os.environ.get(k)
        # 家目录类：真实用户目录一律不参与（~ / ~/.codex 也在临时根内）
        os.environ["APPDATA"] = _TMP_ROOT
        os.environ["LOCALAPPDATA"] = os.path.join(_TMP_ROOT, "localappdata")
        os.environ["USERPROFILE"] = _TMP_ROOT
        os.environ["HOME"] = _TMP_ROOT
        # 受控因素：清掉它们，由本次测试的参数决定运行模式
        for k in ("WBRELAY_DATA_DIR", "WBRELAY_UPSTREAM_DIR",
                  "DSH_HOME", "CRUSH_GLOBAL_DATA", "CRUSH_GLOBAL_CONFIG", "XDG_DATA_HOME",
                  "WBRELAY_READONLY", "WBRELAY_NO_IDE_INJECT", "WBRELAY_HEADLESS"):
            os.environ.pop(k, None)
        atexit.register(cleanup)
    _PORT = port or _PORT or free_port()
    if _PORT == 8789:
        raise AssertionError("测试禁止占用 8789（在用实例端口）")
    if not keep_argv:
        argv = [sys.argv[0] if sys.argv else "wbrelay-test",
                "--data-dir", _DATA_DIR,
                "--no-ide-inject",
                "--port", str(_PORT),
                "--headless"]
        if readonly:
            argv.append("--readonly")
        argv.extend(extra_args)
        sys.argv = argv
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    return _DATA_DIR


def bootstrap_readonly(**kwargs):
    kwargs["readonly"] = True
    return bootstrap(**kwargs)


def tmp_root():
    return _TMP_ROOT


def data_dir():
    return _DATA_DIR


def port():
    return _PORT


def _norm(path):
    return os.path.normcase(os.path.abspath(str(path)))


def forbidden_dirs():
    """测试期间绝不允许被指向的目录（默认数据目录 / 仓库 / 受保护目录）。

    **只按路径规则拼路径**：不调 os.listdir、不读任何受保护对象的内容。
    """
    names = list(PROTECTED_DIR_NAMES) + [PROTECTED_DIR_PREFIXES[0]]
    out = [os.path.join(_REAL_APPDATA, n) for n in names]
    out.append(os.path.join(_REAL_LOCALAPPDATA, "WBRelay"))
    out.append(os.path.join(_REAL_USERPROFILE, ".codex"))
    out.append(os.path.join(_REAL_HOME, ".codex"))
    out.append(ROOT)
    return [_norm(p) for p in out]


def isolation_problems(data=None, tmp=None):
    """返回「未隔离」的问题列表；空列表 = 合格。"""
    problems = []
    if _TMP_ROOT is None:
        return ["未调用 isolate.bootstrap()：测试未进入隔离模式"]
    data = data if data is not None else _DATA_DIR
    tmp = tmp if tmp is not None else _TMP_ROOT
    nd, nt = _norm(data), _norm(tmp)
    if not (nd == nt or nd.startswith(nt + os.sep)):
        problems.append("数据目录不在临时根内：%s" % data)
    for bad in forbidden_dirs():
        if nd == bad or nd.startswith(bad + os.sep):
            problems.append("数据目录指向受保护/默认目录：%s" % data)
    base = os.path.basename(nd.rstrip(os.sep))
    if base in PROTECTED_DIR_NAMES or base.startswith(PROTECTED_DIR_PREFIXES):
        problems.append("数据目录名与受保护对象同名：%s" % base)
    for key in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"):
        val = os.environ.get(key) or ""
        if val and not _norm(val).startswith(nt + os.sep) and _norm(val) != nt:
            problems.append("环境变量 %s 未指向临时根：%s" % (key, val))
    try:
        import wbrelay.paths as _paths
        if _norm(_paths.DATA) != nd:
            problems.append("wbrelay.paths.DATA(%s) 与测试数据目录(%s) 不一致"
                            % (_paths.DATA, data))
    except Exception:
        pass
    return problems


def assert_isolated(data=None):
    """断言隔离成立；失败直接抛 AssertionError（测试应立即失败）。"""
    problems = isolation_problems(data=data)
    if problems:
        raise AssertionError("测试隔离断言失败：\n  - " + "\n  - ".join(problems))
    return True


def is_own_temp_dir(path):
    """该路径是否是本测试自己 mkdtemp 出来的临时目录（清理前必须先过这一步）。"""
    if not path:
        return False
    n = os.path.normcase(os.path.realpath(str(path)))
    base = os.path.basename(n.rstrip(os.sep))
    if not base.startswith(_TMP_PREFIX):
        return False
    parent = os.path.normcase(os.path.realpath(os.path.dirname(n)))
    root = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
    return parent == root


def cleanup():
    """删除临时根（只删自己 mkdtemp 出来、且路径校验通过的目录）。"""
    global _TMP_ROOT, _DATA_DIR
    root = _TMP_ROOT
    if not root:
        return
    _TMP_ROOT = None
    _DATA_DIR = None
    if is_own_temp_dir(root) and os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
    for k, v in _ENV_BACKUP.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# fixture：测试自己的配置 / 用量数据（全部写进临时目录，不读真实数据）
# ---------------------------------------------------------------------------


def demo_supplier(sid, name, base_url, key="sk-test-FAKE-KEY-0000", models=None,
                  enabled=True, wire_api="responses"):
    """通用测试供应商（假 Key，非真实凭据）。"""
    return {
        "id": sid,
        "name": name,
        "base_url": base_url,
        "api_key": key,
        "wire_api": wire_api,
        "models": list(models or []),
        "enabled": bool(enabled),
        "timeout_sec": 10,
    }


def base_settings(**overrides):
    """测试用最小配置（不含任何真实供应商/凭据）。"""
    data = {
        "port": _PORT or 18790,
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
        "upstream_enabled": False,
        "autostart": False,
    }
    data.update(overrides)
    return data


def write_settings(settings):
    """把测试配置原子写入临时数据目录（同目录临时文件 + os.replace）。"""
    import json
    os.makedirs(_DATA_DIR, exist_ok=True)
    path = os.path.join(_DATA_DIR, "settings.json")
    tmp = path + ".fixture.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def write_models_cache(cache):
    """把测试用模型缓存原子写入临时数据目录（路由表由它推导）。"""
    import json
    os.makedirs(_DATA_DIR, exist_ok=True)
    path = os.path.join(_DATA_DIR, "models_cache.json")
    tmp = path + ".fixture.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def write_usage(rows):
    """把测试用量写入临时数据目录（每行一条 JSON）。"""
    import json
    os.makedirs(_DATA_DIR, exist_ok=True)
    path = os.path.join(_DATA_DIR, "usage.jsonl")
    tmp = path + ".fixture.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return path


def list_files(root=None):
    """列出目录下全部文件（相对路径），用于「零写入」断言。"""
    root = root or _DATA_DIR
    out = []
    for base, _dirs, names in os.walk(root):
        for n in names:
            out.append(os.path.relpath(os.path.join(base, n), root))
    return sorted(out)
