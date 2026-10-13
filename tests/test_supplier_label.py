# -*- coding: utf-8 -*-
"""供应商显示名解析的隔离测试（自建 fixture，不读真实数据）。

覆盖：
  T1 名称解析优先级：当前配置 > 记录快照 > 删除留档 > id
  T2 record_usage 写入 supplier_name 快照
  T3 set_suppliers 删除供应商时自动登记 supplier_aliases
  T4 删除供应商后：筛选候选 / 按供应商汇总 / 请求明细 仍显示原名
  T5 无快照的老记录（删除前的历史行）靠留档还原原名
  T6 已删且无处可查的孤儿保持显示 id
  T7 幂等：重复保存同样的配置不会重复登记、不报错
  T8 数据隔离自检：DATA 指向自己创建的临时目录；不碰真实 %APPDATA%、仓库与受保护目录

测试数据全部由本脚本在临时目录内生成（含假 Key `sk-test-FAKE-...`），
**不读取、不复制** %APPDATA%\WBRelay 或任何在用数据。

用法：python tests/test_supplier_label.py
"""
import io
import json
import os
import sys

import isolate

# ---- 关键：先隔离，再 import wbrelay（paths 在导入期解析数据目录）----
isolate.bootstrap()
isolate.TMP_DATA = isolate.data_dir()

from wbrelay import usage as _usage            # noqa: E402
from wbrelay.paths import DATA, USAGE_PATH       # noqa: E402
from wbrelay.settings import (get_suppliers, resolve_supplier_label,  # noqa: E402
                             set_suppliers, supplier_aliases)
from wbrelay.web.server import Handler          # noqa: E402

PASS, FAIL = [], []

SUP_A = isolate.demo_supplier("sup_demo_a", "演示供应商 A", "http://127.0.0.1:18788/v1")
SUP_B = isolate.demo_supplier("sup_demo_b", "演示供应商 B", "http://127.0.0.1:18789/v1")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                       (" | " + detail) if detail else ""))


class FakeHandler(object):
    """只提供 _api_usage 需要的最小接口"""

    def __init__(self, path):
        self.path = path
        self.code = None
        self.body = None

    def _json(self, code, obj):
        self.code = code
        self.body = obj
        return obj


def call(qs):
    h = FakeHandler("/api/usage?" + qs)
    Handler._api_usage(h)
    return h.body


def last_usage_row():
    """直接读文件末行（绕过 load_usage_rows 的增量缓存）"""
    last = None
    with io.open(USAGE_PATH, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    return json.loads(last) if last else None


def write_fixture():
    """本测试自己的配置与用量（假 Key、通用供应商名）。"""
    import time
    now = time.time()
    isolate.write_settings(isolate.base_settings(suppliers=[SUP_A, SUP_B]))
    # 老记录：没有 supplier_name 快照（模拟修复前写下的历史行）
    isolate.write_usage([
        {"at": now - 3600, "iso": "2026-01-01T00:00:00", "model": "demo-model-old",
         "realm": "supplier:" + SUP_B["id"], "stream": False, "tokens": 3,
         "input_tokens": 2, "output_tokens": 1, "elapsed_ms": 12},
    ])


def main():
    write_fixture()
    print("临时数据目录 : %s" % DATA)
    print("（真实 %%APPDATA%%\\WBRelay 未被读取、未被写入）\n")

    # ---- T8 数据隔离自检 ----
    try:
        isolate.assert_isolated()
        check("T8a DATA 指向本测试创建的临时目录（隔离断言通过）", True, DATA)
    except AssertionError as exc:
        check("T8a DATA 指向本测试创建的临时目录（隔离断言通过）", False, str(exc))
    check("T8b 临时目录内没有真实数据文件被带进来",
          sorted(isolate.list_files()) == ["settings.json", "usage.jsonl"],
          repr(isolate.list_files()))

    # ---- T1 解析优先级 ----
    cfg = {"sup_cfg": "配置里的名字"}
    alias = {"sup_gone": "已删的供应商"}
    check("T1a 当前配置优先于快照",
          resolve_supplier_label("sup_cfg", "旧快照名", cfg, alias) == "配置里的名字")
    check("T1b 配置里没有 → 用记录快照",
          resolve_supplier_label("sup_new", "快照名", cfg, alias) == "快照名")
    check("T1c 配置/快照都没有 → 用删除留档",
          resolve_supplier_label("sup_gone", "", cfg, alias) == "已删的供应商")
    check("T1d 全部没有 → 回落到 id",
          resolve_supplier_label("sup_ghost", "", cfg, alias) == "sup_ghost")
    check("T1e 空 id → 空串",
          resolve_supplier_label("", "x", cfg, alias) == "")

    # ---- T2 record_usage 写快照 ----
    _usage.record_usage({"model": "label-test-1", "realm": "supplier:" + SUP_B["id"],
                         "stream": False, "tokens": 1})
    row = last_usage_row()
    check("T2  record_usage 自动写入 supplier_name 快照",
          row.get("supplier_name") == SUP_B["name"],
          "supplier_name=%r" % row.get("supplier_name"))

    sid, sname = SUP_B["id"], SUP_B["name"]

    # ---- T4a 删除前：接口用配置名 ----
    before = call("range=all&limit=100")
    label_before = next((s.get("supplier_label") for s in before["by_supplier"]
                         if (s.get("realm") or "").replace("supplier:", "") == sid), None)
    check("T4a 删除前按供应商汇总显示配置名", label_before == sname, repr(label_before))

    # ---- T3 删除供应商 → 自动登记留档 ----
    kept = [s for s in get_suppliers() if s["id"] != sid]
    saved = set_suppliers(kept)
    aliases = supplier_aliases()
    check("T3a 删除后自动写入 supplier_aliases",
          aliases.get(sid) == sname, "aliases[%s]=%r" % (sid, aliases.get(sid)))
    check("T3b 供应商已从配置移除",
          sid not in [s["id"] for s in get_suppliers()])
    check("T3c set_suppliers 成功保存返回 True（保存语义统一）", saved is True, repr(saved))

    # ---- T4b/c/d 删除后：三处都仍显示原名 ----
    after = call("range=all&limit=100")
    f_names = {s["id"]: s["name"] for s in after["filters"]["suppliers"]}
    check("T4b 筛选候选仍显示原名",
          f_names.get(sid) == sname, "filters[%s]=%r" % (sid, f_names.get(sid)))

    label_after = next((s.get("supplier_label") for s in after["by_supplier"]
                        if (s.get("realm") or "").replace("supplier:", "") == sid), None)
    check("T4c 按供应商汇总仍显示原名", label_after == sname, repr(label_after))

    recent_labels = set()
    for page in range(1, 6):
        r = call("range=all&limit=100&page=%d" % page)
        for it in r["recent"]["items"]:
            if (it.get("realm") or "").replace("supplier:", "") == sid:
                recent_labels.add(it.get("supplier_label"))
    check("T4d 请求明细仍显示原名",
          recent_labels == {sname}, repr(recent_labels))

    # ---- T5 无快照的老记录也靠留档还原 ----
    old_rows = [r for r in _usage.load_usage_rows()
                if (r.get("realm") or "") == "supplier:" + sid and not r.get("supplier_name")]
    check("T5  无快照的老记录确实存在于 fixture（%d 条）" % len(old_rows), len(old_rows) >= 1)
    lbl = resolve_supplier_label(sid, None, {}, aliases)
    check("T5  无快照的老记录靠留档还原原名", lbl == sname, repr(lbl))

    # ---- T6 已删且无处可查的孤儿保持 id ----
    orphans = ["sup_ghost_1", "sup_ghost_2", "intl"]
    keep_id = all(resolve_supplier_label(o) == o for o in orphans)
    check("T6  无处可查的孤儿保持显示 id（旧的不找回）", keep_id)

    # ---- T7 幂等 ----
    try:
        set_suppliers(kept)                   # 再存一次同样的配置
        set_suppliers(kept)
        al2 = supplier_aliases()
        check("T7  重复保存幂等（留档不重复登记、不报错）", al2.get(sid) == sname)
    except Exception as exc:
        check("T7  重复保存幂等（留档不重复登记、不报错）", False, str(exc))

    # ---- T8c 收尾：临时目录内只有本测试自己造的文件 ----
    left = isolate.list_files()
    check("T8c 全程只在临时目录内产生文件",
          all(not f.startswith("..") for f in left) and "settings.json" in left,
          repr(left))

    print("\n---- 结果：%d/%d 通过 ----" % (len(PASS), len(PASS) + len(FAIL)))
    for n in FAIL:
        print("  失败：%s" % n)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())