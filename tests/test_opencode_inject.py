# -*- coding: utf-8 -*-
"""OpenCode 注入/同步的配置形态回归（协议矩阵第 4 象限的客户端侧）。

被测行为（`wbrelay/ides/opencode.py`）：

  O1  注入写出的模型条目带 `variants`（档位），来源是 models_view 的 reasoning_efforts
  O2  不再写 `supportsResponsesApi` / `supported_reasoning_levels`（客户端不读，见
      docs/方案-OpenCode接管修复.md §2.3；留着只会让后来者继续写错）
  O3  `limit` 保留；未启用 / 不可路由的模型被略过并计数
  O4  注入不动配置文件里其它 provider（别人的配置原样保留）
  O5  `_opencode_sync_catalog()` 只更新 wbrelay.models，且幂等（两次调用内容一致）

隔离：只用临时目录 + 进程内假数据；不联网、不碰真实 `~/.config/opencode`。

用法：python tests/test_opencode_inject.py
"""
import io
import json
import os
import sys
import time

import isolate

DATA_DIR = isolate.bootstrap()
PORT = isolate.port()

from wbrelay import settings as wb_settings                            # noqa: E402
from wbrelay.ides import opencode as oc                                # noqa: E402

KEY = "sk-test-FAKE-KEY-0000"
SUP_ID = "sup_demo_open"
BASE_URL = "http://127.0.0.1:9/v1"          # 不联网：注入只要 base_url 字符串
M_FULL = "demo-model-with-efforts"
M_PLAIN = "demo-model-no-efforts"
M_HIDDEN = "demo-model-disabled"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def config_path():
    return os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.json")


def write_config(obj):
    p = config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, indent=2))
    return p


def read_config():
    with io.open(config_path(), encoding="utf-8") as fh:
        return json.load(fh)


def write_cache(by_model):
    suppliers = {}
    for sid, models in by_model.items():
        suppliers[sid] = {"name": "演示供应商 " + sid, "base_url": BASE_URL,
                          "models": models, "error": None, "ok": True}
    isolate.write_models_cache({
        "fetched_at": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "suppliers": suppliers, "errors": {}, "ok_count": len(by_model),
        "total_suppliers": len(by_model)})


def model_row(mid, efforts):
    row = {"id": mid, "context_window": 1000, "max_output_tokens": 100}
    if efforts:
        row["reasoning_efforts"] = efforts
    return row


def main():
    problems = isolate.isolation_problems()
    check("O0a 数据目录已隔离（临时目录）", not problems, "；".join(problems))
    check("O0b 家目录也在临时根内（不会碰真实 ~/.config）",
          os.path.expanduser("~").startswith(os.path.abspath(isolate.tmp_root())),
          os.path.expanduser("~"))

    sup = isolate.demo_supplier(SUP_ID, "演示供应商 open", BASE_URL, KEY,
                                models=[M_FULL, M_PLAIN, M_HIDDEN], wire_api="responses")
    data = isolate.base_settings(suppliers=[sup])
    data["group_models_enabled"] = {BASE_URL: {M_HIDDEN: False}}
    isolate.write_settings(data)
    write_cache({SUP_ID: [model_row(M_FULL, ["low", "high", "max"]),
                          model_row(M_PLAIN, []),
                          model_row(M_HIDDEN, ["low"])]})
    # 清掉配置缓存：上面的写盘绕过了 save_settings
    wb_settings._settings_cache["mtime"] = None
    wb_settings._settings_cache["data"] = None

    write_config({
        "$schema": "https://example.invalid/config.json",
        "provider": {
            "other-provider": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "别人的供应商",
                "options": {"apiKey": "sk-test-FAKE-OTHER", "baseURL": "https://example.invalid/v1"},
                "models": {"some-model": {"name": "some-model"}},
            }
        },
    })

    # ---- O1 / O2 / O3：注入形态 ----
    ok = oc._opencode_inject(PORT)
    check("O1a 注入返回 True", ok is True)
    cfg = read_config()
    provider = (cfg.get("provider") or {}).get("wbrelay") or {}
    models = provider.get("models") or {}
    entry = models.get(M_FULL) or {}
    check("O1b 档位写成 variants（low/high/max → reasoningEffort）",
          entry.get("variants") == {"low": {"reasoningEffort": "low"},
                                    "high": {"reasoningEffort": "high"},
                                    "max": {"reasoningEffort": "max"}},
          repr(entry.get("variants")))
    check("O1c 模型条目仍带 name / limit",
          entry.get("name") == M_FULL and entry.get("limit") == {"context": 1000, "output": 100},
          repr(entry))
    raw = io.open(config_path(), encoding="utf-8").read()
    check("O2a 不再写 supportsResponsesApi", "supportsResponsesApi" not in raw)
    check("O2b 不再写 supported_reasoning_levels", "supported_reasoning_levels" not in raw)
    check("O2c provider 仍是 openai-compatible（chat 客户端路径，翻译由网关负责）",
          provider.get("npm") == "@ai-sdk/openai-compatible", repr(provider.get("npm")))
    check("O2d baseURL / apiKey 指向本机网关",
          (provider.get("options") or {}).get("baseURL") == "http://127.0.0.1:%d/v1" % PORT
          and bool((provider.get("options") or {}).get("apiKey")), repr(provider.get("options")))
    check("O3a 无档位的模型不写 variants", "variants" not in (models.get(M_PLAIN) or {}),
          repr(models.get(M_PLAIN)))
    check("O3b 组级停用的模型被略过", M_HIDDEN not in models, repr(sorted(models)))

    # ---- O4：不动其他 provider ----
    other = (cfg.get("provider") or {}).get("other-provider") or {}
    check("O4a 其它 provider 原样保留",
          other.get("name") == "别人的供应商"
          and (other.get("options") or {}).get("baseURL") == "https://example.invalid/v1"
          and "some-model" in (other.get("models") or {}), repr(other)[:200])
    check("O4b $schema 保留", cfg.get("$schema") == "https://example.invalid/config.json")

    # ---- O5：同步幂等 ----
    first = oc._opencode_sync_catalog()
    before = io.open(config_path(), "rb").read()
    second = oc._opencode_sync_catalog()
    after = io.open(config_path(), "rb").read()
    check("O5a 同步返回（可路由模型数, 隐藏数）",
          first == (2, 1) and second == (2, 1), "%r / %r" % (first, second))
    check("O5b 同步幂等：两次调用文件内容一致", before == after)
    cfg2 = read_config()
    prov2 = (cfg2.get("provider") or {}).get("wbrelay") or {}
    check("O5c 同步后 npm / options / variants 不变",
          prov2.get("npm") == "@ai-sdk/openai-compatible"
          and (prov2.get("options") or {}).get("baseURL") == "http://127.0.0.1:%d/v1" % PORT
          and ((prov2.get("models") or {}).get(M_FULL) or {}).get("variants", {}).get("max")
          == {"reasoningEffort": "max"}, repr(prov2)[:160])

    print("\n断言：PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for n in FAIL:
            print("  - " + n)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
