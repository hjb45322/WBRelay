# -*- coding: utf-8 -*-
"""catalog 生成链路的护栏测试（纯本地、不碰 ~/.codex、不写仓库）

覆盖两个真缺陷（对应 docs/事故记录.md 2026-10-11 21:45 同类问题）：
  T1 校验函数：能抓出「缺 shell_type / 缺 slug / 清单为空」
  T2 模板链全失效时 build_codex_catalog 仍产出带 shell_type 的条目（兜底）
  T3 自检不通过时 sync_codex_catalog **不覆盖**生效文件（保留上一版）
  T4 自检通过时正常原子写入

用法：python tests/test_catalog_guard.py
"""
import io
import json
import os
import shutil
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: E402

# 隔离：catalog 会读 settings（provider 名 / upstream_catalog_path），
# 必须让它在自建临时数据目录里读到「空配置」，绝不读真实 %APPDATA%\WBRelay
isolate.bootstrap()

from wbrelay import catalog as cat  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def fake_models_view(refresh=False):
    return {"suppliers": {"s1": {"models": [
        {"id": "gpt-6-luna", "context_window": 1000000, "max_output_tokens": 128000,
         "reasoning_efforts": ["low", "high"], "supports_vision": True},
        {"id": "deepseek-v4-pro", "context_window": 200000, "max_output_tokens": 48000,
         "reasoning_efforts": ["high"], "supports_vision": False},
    ]}}}


def main():
    problems = isolate.isolation_problems()
    check("T0  数据目录已隔离（不读真实 %APPDATA%）", not problems, "；".join(problems))
    tmp = tempfile.mkdtemp(prefix="wbrelay-catalog-test-")
    real_catalog_path = cat.CODEX_CATALOG
    real_tpl, real_relay, real_up = (cat._load_template_from_existing_catalog,
                                     cat._load_relay_template, cat._load_upstream_catalog_map)
    real_view, real_route = cat.models_view, cat._route_models_set
    real_build = cat.build_codex_catalog
    try:
        target = os.path.join(tmp, "wbrelay-models.json")
        cat.CODEX_CATALOG = target

        # ---- T1 校验函数 ----
        check("T1a 缺 shell_type 被抓出",
              len(cat.validate_codex_catalog({"models": [{"slug": "m1"}]})) == 1)
        check("T1b 缺 slug 被抓出",
              len(cat.validate_codex_catalog({"models": [{"slug": "", "shell_type": "shell_command"}]})) == 1)
        check("T1c 空清单被抓出", len(cat.validate_codex_catalog({"models": []})) == 1)
        check("T1d 合法条目通过",
              cat.validate_codex_catalog({"models": [{"slug": "m1", "shell_type": "shell_command"}]}) == [])

        # ---- T2 模板链失效 → 兜底 ----
        cat._load_template_from_existing_catalog = lambda: {}
        cat._load_relay_template = lambda: {}
        cat._load_upstream_catalog_map = lambda: {}
        built = cat.build_codex_catalog({
            "gpt-6-luna": {"context_window": 1000, "max_output_tokens": 100, "reasoning_efforts": ["low"]},
            "deepseek-v4-pro": {"context_window": 2000, "max_output_tokens": 200, "reasoning_efforts": ["high"]},
        }, {"gpt-6-luna": True, "deepseek-v4-pro": True})
        entries = built.get("models") or []
        check("T2a 无模板时仍产出条目", len(entries) == 2, "entries=%d" % len(entries))
        check("T2b 每条都补上 shell_type",
              all(str(m.get("shell_type") or "").strip() for m in entries),
              repr([m.get("shell_type") for m in entries]))
        check("T2c 兜底结果通过自检（不会再产出 Codex 起不来的 catalog）",
              cat.validate_codex_catalog(built) == [])

        # ---- T3 自检不通过 → 不覆盖 ----
        with io.open(target, "w", encoding="utf-8") as fh:
            fh.write('{"models": [{"slug": "OLD", "shell_type": "shell_command"}]}')
        before = io.open(target, encoding="utf-8").read()
        cat.models_view = fake_models_view
        cat._route_models_set = lambda: {"gpt-6-luna"}
        cat.build_codex_catalog = lambda models, enabled: {"models": [{"slug": "bad"}]}   # 故意缺字段
        written, hidden = cat.sync_codex_catalog()
        after = io.open(target, encoding="utf-8").read()
        check("T3a 自检失败时返回 0（未写入）", (written, hidden) == (0, 0), "返回=%r" % ((written, hidden),))
        check("T3b 生效文件保持上一版内容", after == before, "前 %d 字节 / 后 %d 字节" % (len(before), len(after)))

        # ---- T4 自检通过 → 正常写入（恢复真实实现，模板仍为空 → 走兜底分支）----
        cat.build_codex_catalog = real_build
        written, hidden = cat.sync_codex_catalog()
        written_ok = False
        if os.path.isfile(target):
            data = json.load(io.open(target, encoding="utf-8"))
            written_ok = bool(data.get("models")) and cat.validate_codex_catalog(data) == []
        check("T4a 自检通过时正常写入", written == 2, "written=%s" % written)
        check("T4b 落盘内容通过自检", written_ok)
        check("T4c 未留下 .tmp 残留", not os.path.isfile(target + ".tmp"))
    finally:
        cat.CODEX_CATALOG = real_catalog_path
        cat._load_template_from_existing_catalog = real_tpl
        cat._load_relay_template = real_relay
        cat._load_upstream_catalog_map = real_up
        cat.models_view = real_view
        cat._route_models_set = real_route
        cat.build_codex_catalog = real_build
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n===== catalog 护栏：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
