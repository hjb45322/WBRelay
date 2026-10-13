# -*- coding: utf-8 -*-
"""配置读写与派生查询。"""
import copy
import json
import os

from .atomicio import write_text
from .paths import (SETTINGS_PATH, DEFAULT_PORT, READONLY, data_dir_error,
                    ensure_data_dir)
from .state import _lock, _cooldown_reasons, _cooldowns
from .log import _log
from .validate import (MAX_API_KEY, MAX_MODELS_PER_SUPPLIER, MAX_MODEL_PATTERN,
                       MAX_SUPPLIERS, MAX_SUPPLIER_NAME, has_url_credentials)

# ---------------------------------------------------------------------------
# settings（按 mtime 缓存，避免每个请求多次读盘；写后立即失效）
#
# 缓存对象 (`_settings_cache["data"]`) 是进程内共享的，**绝不允许泄漏给调用方**：
# `load_settings()` 一律返回深拷贝，任何调用方对返回值的改动都污染不到缓存
# （详见 docs/方案-配置并发安全.md）。需要「读 → 改 → 写」原子性的场景请用
# `update_settings(mutator)`，它持 `_lock` 完成整个 read-modify-write。
# ---------------------------------------------------------------------------
_settings_cache = {"mtime": None, "data": None}


def load_settings():
    """当前配置（**深拷贝**，调用方可以随便改；改不会影响缓存，也不会落盘）。

    为什么必须深拷贝：`suppliers` / `api_keys` / `group_models_enabled` /
    `model_prices` / `supplier_aliases` 全是嵌套 dict/list，浅拷贝或直接返回缓存
    都会让「顺手改一下再 save_settings(data)」的写法污染全局配置——不落盘、不报错，
    但下一次保存时又被当成"当前状态"整体写回。
    """
    data = _load_settings_cached()
    if not data:
        # 空配置（文件缺失 / 内容为 {}）：直接给一个新 dict，绝不返回共享对象
        return {}
    return copy.deepcopy(data)


def _load_settings_cached():
    """缓存内的**共享**配置对象（内部用，调用方不得修改）。

    仅供 `settings.py` 内部只读派生查询使用；任何外部模块请一律走
    `load_settings()`（深拷贝）。热路径（`routing.get_suppliers` 等）也走
    `load_settings()`：settings.json 通常只有几 KB，深拷贝开销可忽略
    （见 docs/方案-配置并发安全.md 的实测数据）。
    """
    try:
        mtime = os.path.getmtime(SETTINGS_PATH)
    except OSError:
        return {}
    cached = _settings_cache["data"]
    if _settings_cache["mtime"] == mtime and cached is not None:
        return cached
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    _settings_cache["mtime"] = mtime
    _settings_cache["data"] = data
    return data


def save_settings(data):
    """原子写配置。返回 True=已落盘；False=未写入（只读模式 / 写盘失败）。

    保存语义（与 set_suppliers 统一）：**成功保存后清空冷却表** —— 配置变了，
    旧的失败冷却不再代表新配置的可用性（核心语义「配置保存即清冷却」）。
    只读模式或写盘失败时：不清冷却、不更新缓存、也不对调用方伪称成功。

    并发语义：本函数**只在写盘瞬间持锁**，保护的是「文件不被并发写坏」。
    调用方的「读 → 改 → 写」若要整体原子，请用 `update_settings(mutator)`
    （它持同一把 RLock 完成整个 read-modify-write，不会与本函数死锁）。
    """
    if READONLY:
        return False
    with _lock:
        if ensure_data_dir() is None:
            _log("[WBRelay] 配置未保存：数据目录不可用（%s）" % data_dir_error())
            return False
        try:
            write_text(SETTINGS_PATH, json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as exc:
            _log("[WBRelay] 配置保存失败（原文件保持不变）：%s" % exc)
            return False
        _settings_cache["mtime"] = None
        _settings_cache["data"] = None
        _cooldowns.clear()        # 保存即清冷却（核心语义 2）
        _cooldown_reasons.clear()  # 冷却原因与冷却同生共死，不能留下悬空原因
        return True


# update_settings 的哨兵：mutator 返回它表示「本次无需写盘」。
# 用于「检查后发现无事可做」的路径（如旧配置迁移已完成）——这类调用如果也落盘，
# 会顺带触发 save_settings 的清冷却与缓存失效，属于无谓的副作用。
_NO_SAVE = object()


def update_settings(mutator):
    """持锁的 read-modify-write 原语：消除并发保存互相覆盖（丢更新）。

    用法::

        ok, result = update_settings(lambda data: data.__setitem__("port", 9000))

    语义：
      · 进入即持 `_lock`（`state._lock`，RLock）——**整段读 → 改 → 写**都在锁内，
        两个并发调用因此串行化，后保存者看到的一定是前保存者的结果，
        "A 拿着旧快照、B 已保存、A 把旧值写回"这种丢更新不再可能发生。
      · `data` 是**锁内重新读**的配置副本（深拷贝），**不是**调用方先前
        `load_settings()` 拿到的旧快照 —— 调用方请勿在 mutator 外先读配置再传进来。
      · `mutator(data)` 的返回值原样带回（第二个元素），便于调用方拿结果
        （如 `set_*` 生成的新 id、清洗后的列表）。
      · `mutator` 抛出的异常**原样上抛**，且此时**尚未**调用 `save_settings`：
        配置不会被写坏，也不会被"半改"落盘。
      · 保存失败（只读模式 / 写盘失败）时返回 `(False, result)`，**不伪称成功**。
      · 保存成功才清冷却表（核心语义 2 由 `save_settings` 统一执行）。
      · 持锁期间只做内存计算 + 一次原子写盘：**不做网络 IO、不起子进程、
        不做长时间磁盘读**。

    死锁安全：`save_settings()` 内部再拿同一把 RLock，同线程重入，不会死锁。
    """
    with _lock:
        data = load_settings()          # 锁内重新读，绝不用调用方的旧快照
        result = mutator(data)
        if result is _NO_SAVE:
            # 本次无需写盘（如「早已迁移完」）：不落盘、不清冷却、不伪称成功
            return True, None
        ok = save_settings(data)        # 同把 RLock，可重入
        return ok, result


def setting_int(name, default):
    try:
        v = int(load_settings().get(name))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _setting_int_multi(names, default):
    """兼容多个 key 名（历史上 cooldown_secs / cooldown_sec 两种写法都存在过）。"""
    for name in names:
        try:
            v = int(load_settings().get(name))
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return default


def require_api_key():
    return bool(load_settings().get("require_api_key"))


# ---- 管理 API 可选令牌（默认关；开启后 /api/* 管理端点需 X-Admin-Token）----
def admin_token_enabled():
    return bool(load_settings().get("require_admin_token"))


def admin_token():
    return str(load_settings().get("admin_token") or "")


def admin_token_ok(provided):
    if not admin_token_enabled():
        return True
    return bool(provided) and provided == admin_token()


def cooldown_secs():
    return _setting_int_multi(("cooldown_sec", "cooldown_secs"), 60)


def cooldown_429_secs():
    return _setting_int_multi(("cooldown_429_sec", "cooldown_429_secs"), 30)


def default_timeout_sec():
    return setting_int("default_timeout_sec", 120)


def usage_retention_days():
    return setting_int("usage_retention_days", 0)


def usage_refresh_secs():
    """用量看板自动刷新间隔（秒）；0 = 关闭自动刷新。因允许 0，故不走 setting_int。"""
    try:
        v = int(load_settings().get("usage_refresh_sec"))
    except (TypeError, ValueError):
        return 5
    return v if 0 <= v <= 600 else 5


def configured_port():
    try:
        p = int(load_settings().get("port") or 0)
        return p if 1 <= p <= 65535 else 0
    except (TypeError, ValueError):
        return 0


def model_prices():
    p = load_settings().get("model_prices") or {}
    return p if isinstance(p, dict) else {}


def models_enabled_map():
    m = load_settings().get("models_enabled") or {}
    return m if isinstance(m, dict) else {}


def model_allowed(model):
    """模型未显式配置时默认启用；显式停用才拒绝。"""
    v = models_enabled_map().get(model)
    if v is None:
        return True
    return bool(v)


def set_models_enabled(mapping):
    """保存全局模型启停。返回 True=已落盘；False=未写入（只读 / 写盘失败）。

    整段 read-modify-write 走 `update_settings` 持锁完成：与供应商 / API Key /
    组级启用等并发保存互不覆盖。
    """
    def _apply(data):
        data["models_enabled"] = {str(k).strip(): bool(v)
                                  for k, v in (mapping or {}).items() if str(k).strip()}
        return True

    ok, _result = update_settings(_apply)
    return ok


# ---------------------------------------------------------------------------
# 组级模型启用（路由归属）：group_models_enabled = {base_url: {model: bool}}
# 语义：供应商的 models 清单只影响拉取显示范围；路由 = 拉取显示范围 ∩ 组级启用。
# 默认全部不启用（拉取后需手动启用），手动停用即该组不再路由该模型。
# ---------------------------------------------------------------------------
_gem_migrated = {"done": False}


def group_models_enabled_map():
    """组级模型启用配置：{base_url: {model: bool}}，未配置的模型默认不启用。"""
    _ensure_gem_migrated()
    g = load_settings().get("group_models_enabled") or {}
    return g if isinstance(g, dict) else {}


def _ensure_gem_migrated():
    """旧全局 models_enabled（{model: bool}）迁移到组级：显式启用的模型应用到所有组。
    仅首次调用执行一次；迁移后写回 settings，后续直接读新字段。"""
    if _gem_migrated["done"]:
        return
    _gem_migrated["done"] = True
    try:
        def _apply(data):
            if data.get("group_models_enabled") is not None:
                return _NO_SAVE        # 已迁移过：不落盘（保持原有语义）
            old = data.get("models_enabled") or {}
            gem = {}
            if isinstance(old, dict):
                for sup in get_suppliers():
                    base = sup["base_url"].rstrip("/")
                    if base:
                        gem.setdefault(base, {})
                for mid, v in old.items():
                    if v is True:
                        for base in gem:
                            gem[base][mid] = True
            data["group_models_enabled"] = gem
            return True

        # 迁移也要持锁：避免与并发保存交错，把迁移结果写丢。
        # 返回值里 ok=False 只可能是只读 / 写盘失败 —— 不重试（下次启动会再试）。
        update_settings(_apply)
    except Exception:
        pass


def set_group_models_enabled(mapping):
    """保存组级启用：{base_url: {model: bool}}。返回 True=已落盘。

    整段 read-modify-write 走 `update_settings` 持锁完成（并发保存不丢更新）。
    """
    def _apply(data):
        clean = {}
        for base, mm in (mapping or {}).items():
            base = str(base).strip().rstrip("/")
            if not base or not isinstance(mm, dict):
                continue
            clean[base] = {str(k).strip(): bool(v) for k, v in mm.items() if str(k).strip()}
        data["group_models_enabled"] = clean
        return True

    ok, _result = update_settings(_apply)
    return ok


def group_model_enabled(base, model):
    """该组（base_url）是否可路由该模型。未配置（未显式停用）= 默认启用；显式 false 才停用。"""
    g = group_models_enabled_map().get(str(base or "").rstrip("/")) or {}
    return g.get(str(model)) is not False


# ---------------------------------------------------------------------------
# 路由表：各供应商可路由模型清单（供应商数组顺序 = 路由优先级）
# 生成条件（三重判断）：
#   ① 上游返回里有它（models_cache 该供应商列表 = 上游返回 ∩ models 清单过滤）
#   ② models 清单没过滤掉它（拉取时已过滤，体现在缓存列表）
#   ③ 组级启用配置没显式停用它（group_models_enabled[base_url][model] != false；未配置 = 默认启用）
# ①②③ 全满足 → 加入该供应商路由表；任一不满足 → 跳过。
# ---------------------------------------------------------------------------


def _mask_key(raw_key):
    """掩码显示用：sk-S***1XNS；短 Key 一律 ******。"""
    raw_key = str(raw_key or "")
    return (raw_key[:4] + "******" + raw_key[-4:]) if len(raw_key) > 8 else "******"


def clean_suppliers(items):
    out = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        base = str(raw.get("base_url") or "").strip().rstrip("/")
        key = str(raw.get("api_key") or "").strip()
        sid = str(raw.get("id") or "").strip()
        if not sid:
            sid = "sup_" + os.urandom(5).hex()
        if not base or not key:
            continue
        if has_url_credentials(base):
            # 供应商 URL 里不允许内嵌用户名/密码（凭据一律走 api_key 字段）
            _log("[WBRelay] 供应商 %s 的 base_url 含用户名/密码，已忽略该条（请改用 API Key）" % sid)
            continue
        try:
            timeout_sec = int(raw.get("timeout_sec") or 120)
        except (TypeError, ValueError):
            timeout_sec = 120
        if timeout_sec < 1:
            timeout_sec = 120
        models = [str(m).strip()[:MAX_MODEL_PATTERN] for m in (raw.get("models") or [])
                  if str(m).strip()][:MAX_MODELS_PER_SUPPLIER]
        out.append({
            "id": sid,
            "name": str(raw.get("name") or sid or "中转").strip()[:MAX_SUPPLIER_NAME],
            "base_url": base,
            "api_key": key[:MAX_API_KEY],
            "wire_api": "chat" if str(raw.get("wire_api") or "responses") == "chat" else "responses",
            "models": models,
            "enabled": raw.get("enabled", True) is not False,
            "timeout_sec": timeout_sec,
        })
        if len(out) >= MAX_SUPPLIERS:
            break
    return out


def get_suppliers():
    return clean_suppliers(load_settings().get("suppliers"))


def set_suppliers(items):
    """保存供应商列表。返回 True=已落盘；False=未写入（只读 / 写盘失败）。

    成功保存会清空冷却表（由 save_settings 统一执行）。

    整段 read-modify-write（读旧列表 → 登记删除留档 → 写新列表）走
    `update_settings` 持锁完成：与 API Key / 组级启用 / 其它设置项并发保存
    互不覆盖。
    """
    def _apply(data):
        old_list = clean_suppliers(data.get("suppliers"))
        new_list = clean_suppliers(items)
        data["suppliers"] = new_list
        # 用量记录只存供应商 id（realm="supplier:<id>"），供应商一被删，读数据时就查不到名字了。
        # 这里把被删供应商的 id->名字留档到 supplier_aliases，历史用量仍能显示原名。
        alive = set(s["id"] for s in new_list)
        aliases = dict(data.get("supplier_aliases") or {})
        changed = False
        for s in old_list:
            if s["id"] not in alive and s.get("name") and aliases.get(s["id"]) != s["name"]:
                aliases[s["id"]] = s["name"]
                changed = True
        if changed:
            data["supplier_aliases"] = aliases
        # 冷却清理由 save_settings 统一负责（保存成功才清），这里不再无条件清
        return True

    ok, _result = update_settings(_apply)
    return ok


def supplier_aliases():
    """已删除供应商的 id -> 显示名留档（由 set_suppliers 自动登记）。"""
    al = load_settings().get("supplier_aliases")
    return al if isinstance(al, dict) else {}


def supplier_config_names():
    """当前配置里的 供应商 id -> 显示名。"""
    return {s["id"]: s["name"] for s in get_suppliers()}


def resolve_supplier_label(sid, snap=None, cfg=None, aliases=None):
    """供应商显示名解析：当前配置 > 用量记录里的历史快照 > 删除留档 > id 本身。

    用量记录里只存 id（realm="supplier:<id>"），显示名靠实时查配置；
    供应商一旦被删，只看配置就会掉成内部 id（如 sup_977e61d1bf），故做多层兜底。
    """
    sid = str(sid or "")
    if not sid:
        return ""
    cfg = supplier_config_names() if cfg is None else cfg
    if sid in cfg:
        return cfg[sid]
    if snap:
        return str(snap)
    aliases = supplier_aliases() if aliases is None else aliases
    if sid in aliases:
        return str(aliases[sid])
    return sid


def get_keys():
    keys = load_settings().get("api_keys") or []
    if not isinstance(keys, list):
        return []
    return [k for k in keys if isinstance(k, dict) and (k.get("key") or "")]


def auth_key_ok(provided):
    if not require_api_key():
        return True          # 默认纯本机路由软件，不校验
    if not provided:
        return False
    for k in get_keys():
        if k.get("enabled", True) is not False and k.get("key") == provided:
            return True
    return False


# ---------------------------------------------------------------------------
# 路由 / 冷却
# ---------------------------------------------------------------------------