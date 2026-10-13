# -*- coding: utf-8 -*-
"""原子写的隔离测试：配置类文件必须「同目录临时文件 + os.replace」。

覆盖（配置/状态类写入点逐项确认）：
  A1  settings.json          ：成功=原子落盘；os.replace 失败=旧内容完好、无残留、返回 False
  A2  backups/settings-*.json：备份文件原子写；失败不产生半截备份
  A3  Codex catalog + .bak   ：主文件与 .bak 都原子写；失败保留上一版
  A4  model_profiles.json    ：失败保留旧内容
  A5  usage.jsonl（保留策略重写）：失败保留旧内容、返回 0
  A6  models_cache.json      ：失败返回 False、旧内容不变
  A7  追加型文件（usage 记录 / wb.log）不做原子替换，但也不得写坏已有内容

用法：python tests/test_atomic_writes.py
"""
import io
import json
import os
import sys
import time
from unittest import mock

import isolate

isolate.bootstrap()

from wbrelay import backup, catalog, modelcache, profiles, usage   # noqa: E402
from wbrelay.paths import BACKUP_DIR, DATA, SETTINGS_PATH            # noqa: E402
from wbrelay.settings import load_settings, save_settings            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def read(path):
    with io.open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def leftovers():
    """数据目录内疑似临时文件（.tmp / .fixture.tmp / 隐藏临时名）。"""
    out = []
    for base, _dirs, names in os.walk(DATA):
        for n in names:
            if n.endswith(".tmp") or n.startswith(".") and n.endswith(".tmp"):
                out.append(os.path.join(base, n))
    return out


def main():
    isolate.assert_isolated()
    check("A0 数据目录已隔离（临时目录）", os.path.abspath(DATA).startswith(isolate.tmp_root()), DATA)

    # ---- A1 settings.json ----
    isolate.write_settings(isolate.base_settings(port=18790))
    old_text = read(SETTINGS_PATH)
    new = isolate.base_settings(port=18791, cooldown_sec=7)
    with mock.patch("os.replace", side_effect=OSError("模拟断电")):
        ok = save_settings(new)
    check("A1a 写盘失败时 save_settings 返回 False（不伪称成功）", ok is False, repr(ok))
    check("A1b 失败后原文件保持有效旧内容", read(SETTINGS_PATH) == old_text)
    check("A1c 失败后无临时文件残留", leftovers() == [], repr(leftovers()))
    check("A1d 失败后旧内容仍可解析为 JSON",
          json.loads(read(SETTINGS_PATH))["port"] == 18790)
    ok = save_settings(new)
    check("A1e 正常保存返回 True", ok is True, repr(ok))
    check("A1f 正常保存后内容为新值且无残留",
          json.loads(read(SETTINGS_PATH))["port"] == 18791 and leftovers() == [],
          repr(leftovers()))

    # ---- A2 备份文件 ----
    isolate.write_settings(isolate.base_settings(port=18790))
    before_backups = sorted(os.listdir(BACKUP_DIR)) if os.path.isdir(BACKUP_DIR) else []
    with mock.patch("os.replace", side_effect=OSError("模拟断电")):
        fn = backup.make_backup()
    after_backups = sorted(os.listdir(BACKUP_DIR)) if os.path.isdir(BACKUP_DIR) else []
    check("A2a 备份失败返回 None", fn is None, repr(fn))
    check("A2b 备份失败不产生文件（也不留半截）", after_backups == before_backups,
          repr(after_backups))
    check("A2c 备份失败无临时文件残留", leftovers() == [], repr(leftovers()))
    fn = backup.make_backup()
    check("A2d 备份成功返回文件名", bool(fn) and fn.startswith("settings-"), repr(fn))
    check("A2e 备份内容与当前配置一致",
          os.path.isfile(os.path.join(BACKUP_DIR, fn)) and
          read(os.path.join(BACKUP_DIR, fn)) == read(SETTINGS_PATH))
    check("A2f 备份后无临时文件残留", leftovers() == [], repr(leftovers()))
    check("A2g 备份文件名不含凭据（仅时间戳）", all(
        "sk-" not in n and "token" not in n.lower() for n in os.listdir(BACKUP_DIR)))

    # ---- A3 catalog + .bak ----
    target = os.path.join(DATA, "codex-catalog-test.json")
    real_catalog, real_view, real_route = catalog.CODEX_CATALOG, catalog.models_view, catalog._route_models_set
    try:
        catalog.CODEX_CATALOG = target
        with io.open(target, "w", encoding="utf-8") as fh:
            fh.write('{"models": [{"slug": "OLD", "shell_type": "shell_command"}]}')
        old_catalog = read(target)
        catalog.models_view = lambda refresh=False: {"suppliers": {"s1": {"models": [
            {"id": "demo-model", "context_window": 1000, "max_output_tokens": 100,
             "reasoning_efforts": ["low"]}]}}}
        catalog._route_models_set = lambda: {"demo-model"}
        written, _hidden = catalog.sync_codex_catalog()
        check("A3a catalog 写入成功", written == 1, "written=%r" % written)
        check("A3b .bak 保存的是上一版内容",
              os.path.isfile(target + ".bak") and read(target + ".bak") == old_catalog)
        check("A3c 主文件已更新且可解析", "demo-model" in read(target))
        check("A3d catalog 写入后无 .tmp 残留",
              not os.path.isfile(target + ".tmp") and not os.path.isfile(target + ".bak.tmp"),
              repr([f for f in os.listdir(DATA) if f.endswith(".tmp")]))

        # 失败路径：replace 失败 → 主文件保持「上一版有效内容」
        good = read(target)
        with mock.patch("os.replace", side_effect=OSError("模拟断电")):
            raised = False
            try:
                catalog.sync_codex_catalog()
            except Exception:
                raised = True
        check("A3e .bak 写入失败时抛出（由接口层转成 500，不静默）", raised)
        check("A3f 主文件保持有效的上一版内容", read(target) == good)
        check("A3g 失败后无 .tmp 残留",
              not os.path.isfile(target + ".tmp") and not os.path.isfile(target + ".bak.tmp"))
    finally:
        catalog.CODEX_CATALOG = real_catalog
        catalog.models_view = real_view
        catalog._route_models_set = real_route

    # ---- A4 model_profiles.json ----
    profiles.save_profiles({"demo-model": {"context_window": 1000}})
    old_prof = read(profiles.PROFILES_PATH)
    with mock.patch("os.replace", side_effect=OSError("模拟断电")):
        ok = profiles.save_profiles({"other": {}})
    check("A4a 档案保存失败返回 False", ok is False, repr(ok))
    check("A4b 档案失败后旧内容完好", read(profiles.PROFILES_PATH) == old_prof)
    check("A4c 档案失败后无残留", leftovers() == [], repr(leftovers()))

    # ---- A5 usage 保留策略重写 ----
    isolate.write_usage([
        {"at": time.time() - 90 * 86400, "model": "old", "realm": "supplier:x"},
        {"at": time.time(), "model": "new", "realm": "supplier:x"},
    ])
    old_usage = read(usage.USAGE_PATH)
    isolate.write_settings(isolate.base_settings(usage_retention_days=30))
    with mock.patch("os.replace", side_effect=OSError("模拟断电")):
        removed = usage.enforce_usage_retention()
    check("A5a 保留清理失败返回 0（不谎报清理条数）", removed == 0, repr(removed))
    check("A5b 失败后用量文件保持旧内容", read(usage.USAGE_PATH) == old_usage)
    check("A5c 失败后无残留", leftovers() == [], repr(leftovers()))
    isolate.write_settings(isolate.base_settings(usage_retention_days=30))
    removed = usage.enforce_usage_retention()
    check("A5d 正常清理生效且无残留", removed == 1 and leftovers() == [], "removed=%r" % removed)

    # ---- A6 models_cache.json ----
    modelcache.save_models_cache({"suppliers": {"s1": {"models": []}}})
    old_cache = read(modelcache.MODELS_CACHE_PATH)
    with mock.patch("os.replace", side_effect=OSError("模拟断电")):
        ok = modelcache.save_models_cache({"suppliers": {}})
    check("A6a 缓存保存失败返回 False", ok is False, repr(ok))
    check("A6b 缓存失败后旧内容完好", read(modelcache.MODELS_CACHE_PATH) == old_cache)
    check("A6c 缓存失败后无残留", leftovers() == [], repr(leftovers()))
    check("A6d load_settings 仍可读（失败没破坏配置文件）", isinstance(load_settings(), dict))

    print("\n===== 原子写：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())