# -*- coding: utf-8 -*-
"""DSH 检测 / 注入 / 同步 / 恢复的配置形态回归。

被测行为（`wbrelay/ides/dsh.py`）：

  D1  检测面只看 profile（cordis.yml / cordis.patch.yml / package.json）；
      home 目录也作为安装探针；实际注入仍要求 profile 骨架
  D2  注入并入**已有**的 `- id: llm-pi-ai` 条目：写 `providers.wbrelay`
      （baseURL / api / apiKeyEnv）+ models + reasoningEfforts
  D3  重注入不覆盖用户在 DSH 里换过的默认模型
  D4  `_dsh_sync_catalog()` 只刷新 models，幂等，且能带出新模型
  D5  恢复：patch 逐字节回到注入前、refs 键删除、备份文件消失
  D6  只读：一个字都不写
  D7  二次注入不污染第一份原始快照
  D8  没有 `llm-pi-ai` 条目 → 追加独立托管条目；用户其它条目 / `!!js` 原样保留
  D9  凭据文件原本不存在 → 接管后新建、恢复后不留空壳
  D10 refs 里原本就有 WBRELAY_API_KEY → 覆盖后恢复成原值
  D11 profile 还没有 cordis.patch.yml → 接管时新建、恢复时删掉

隔离：只用临时目录 + 进程内假数据；不联网、不碰真实 `~/.dsh`。

用法：python tests/test_dsh_inject.py
"""
import hashlib
import io
import json
import os
import sys
import time

import isolate

DATA_DIR = isolate.bootstrap()
PORT = isolate.port()

from wbrelay import settings as wb_settings                            # noqa: E402
from wbrelay.ides import dsh                                           # noqa: E402

KEY = "sk-test-FAKE-KEY-0000"
SUP_ID = "sup_demo_dsh"
BASE_URL = "http://127.0.0.1:9/v1"          # 不联网：注入只要 base_url 字符串
M_FULL = "demo-model-with-efforts"
M_PLAIN = "demo-model-no-efforts"
M_NEW = "demo-model-new"
M_HIDDEN = "demo-model-disabled"
PASS, FAIL = [], []

CORDIS_YML = "[]\n"

PATCH_WITH_ENTRY = "\n".join([
    "# dsh profile patch（用户自己的层）",
    "- id: ui-settings-account",
    '  name: "@deepseek-ai/dsh-client-ui-settings-account"',
    "  config:",
    "    version: 1",
    "    step: done",
    "- id: llm-pi-ai",
    "  name: '@deepseek-ai/dsh-llm-pi-ai'",
    "  config:",
    "    providers:",
    "      my-gateway:",
    "        displayName: 我的中转站",
    "        api: openai-completions",
    '        baseURL: "https://example.invalid/v1"',
    "        apiKeyEnv: MY_GATEWAY_API_KEY",
    "- id: agent-default-model",
    "  name: '@deepseek-ai/dsh-agent-default-model'",
    "  config:",
    "    provider: deepseek-account",
    "    model: deepseek-flash",
    "    reasoningEffort: high",
    "",
])

PATCH_WITHOUT_PI_AI = "\n".join([
    "- id: ui-settings-account",
    '  name: "@deepseek-ai/dsh-client-ui-settings-account"',
    "  config:",
    "    version: 1",
    "    developerTools: true",
    "- id: config-editor",
    "  name: '@deepseek-ai/dsh-config-editor'",
    "  disabled: !!js \"!ctx.get('profileContext')\"",
    "- id: agent-default-model",
    "  name: '@deepseek-ai/dsh-agent-default-model'",
    "  config:",
    "    provider: deepseek-account",
    "    model: deepseek-flash",
    "",
])

PATCH_ONLY_UI = "\n".join([
    "- id: ui-settings-account",
    '  name: "@deepseek-ai/dsh-client-ui-settings-account"',
    "  config:",
    "    version: 1",
    "",
])

CRED_ORIGINAL = "\n".join([
    "version: 1",
    "",
    "refs:",
    '  OTHER_API_KEY: "sk-test-FAKE-OTHER-0001"',
    "",
    "records:",
    "  deepseek-account-platform/device:",
    "    kind: grant",
    "    payload:",
    "      id: 00000000-0000-4000-8000-000000000000",
    "",
])

LEGACY_SETTINGS = "version: 1\n\nsome-section:\n  a: 1\n"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def profile_dir():
    return os.path.join(dsh.DSH_PROFILES, "desktop")


def patch_path():
    return os.path.join(profile_dir(), "cordis.patch.yml")


def cred_path():
    return dsh.DSH_CREDENTIALS_PATH


def read(path):
    with io.open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def write(path, text):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reset_backup():
    try:
        os.remove(dsh.ide_backup_path("dsh"))
    except OSError:
        pass


def entry_text(text, entry_id):
    lines = text.split("\n")
    rng = dsh._entry_range(lines, entry_id)
    if not rng:
        return ""
    return "\n".join(lines[rng[0]:rng[1]])


def model_row(mid, efforts):
    row = {"id": mid, "context_window": 1000, "max_output_tokens": 100}
    if efforts:
        row["reasoning_efforts"] = efforts
    return row


def write_cache(rows):
    suppliers = {SUP_ID: {"name": "演示供应商 dsh", "base_url": BASE_URL,
                          "models": rows, "error": None, "ok": True}}
    isolate.write_models_cache({
        "fetched_at": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "suppliers": suppliers, "errors": {}, "ok_count": 1, "total_suppliers": 1})


def main():
    problems = isolate.isolation_problems()
    check("D0a 数据目录已隔离（临时目录）", not problems, "；".join(problems))
    check("D0b 家目录也在临时根内（不会碰真实 ~/.dsh）",
          os.path.expanduser("~").startswith(os.path.abspath(isolate.tmp_root())),
          os.path.expanduser("~"))

    # ---- D1：检测面 ----
    check("D1a 没有 ~/.dsh 时判「未检测到」", dsh._dsh_detect() is False)
    write(cred_path(), CRED_ORIGINAL)
    check("D1b 只有凭据时 home 安装探针命中，但无 profile 不注入",
          dsh._dsh_detect() is True and dsh._dsh_inject(PORT) is False)
    os.remove(cred_path())
    write(dsh.DSH_SETTINGS_PATH, LEGACY_SETTINGS)
    check("D1c 旧版文件仅命中 home 探针，不注入 settings.yaml",
          dsh._dsh_detect() is True and dsh._dsh_inject(PORT) is False)
    os.remove(dsh.DSH_SETTINGS_PATH)
    write(os.path.join(profile_dir(), "cordis.yml"), CORDIS_YML)
    check("D1d 有 profile（cordis.yml）即判「已检测到」", dsh._dsh_detect() is True)

    # ---- 夹具：供应商 / 模型缓存 / 配置 ----
    sup = isolate.demo_supplier(SUP_ID, "演示供应商 dsh", BASE_URL, KEY,
                                models=[M_FULL, M_PLAIN, M_HIDDEN], wire_api="responses")
    data = isolate.base_settings(suppliers=[sup],
                                 api_keys=[{"id": "k1", "name": "测试 Key",
                                            "key": KEY, "enabled": True}])
    data["group_models_enabled"] = {BASE_URL: {M_HIDDEN: False}}
    isolate.write_settings(data)
    write_cache([model_row(M_FULL, ["low", "high", "max"]),
                 model_row(M_PLAIN, []),
                 model_row(M_HIDDEN, ["low"])])
    wb_settings._settings_cache["mtime"] = None
    wb_settings._settings_cache["data"] = None

    # ---- D2：注入（并入已有 llm-pi-ai 条目）----
    original = PATCH_WITH_ENTRY
    write(patch_path(), original)
    write(cred_path(), CRED_ORIGINAL)
    reset_backup()
    check("D2a 备份成功", dsh._dsh_backup() is True)
    check("D2b 注入成功", dsh._dsh_inject(PORT) is True)
    text = read(patch_path())
    entry = entry_text(text, "llm-pi-ai")
    default_entry = entry_text(text, "agent-default-model")
    first_model = dsh._dsh_model_entries()[0][0][0]
    check("D2c 托管块成对出现", dsh.WB_BEGIN in text and dsh.WB_END in text)
    check("D2d 路由并入了已有 llm-pi-ai 条目（不新增第二条条目）",
          "- id: llm-pi-ai" in entry and text.count("- id: llm-pi-ai") == 1, entry[:200])
    check("D2e 路由字段齐全（api / baseURL / apiKeyEnv / displayName）",
          "api: openai-completions" in entry
          and ('baseURL: "http://127.0.0.1:%d/v1"' % PORT) in entry
          and ("apiKeyEnv: " + dsh.REF_NAME) in entry
          and "displayName: WBRelay" in entry)
    check("D2f 用户自己的路由原样保留",
          "my-gateway:" in entry and "MY_GATEWAY_API_KEY" in entry
          and "https://example.invalid/v1" in entry, entry[:400])
    check("D2g 用户其它条目原样保留",
          "- id: ui-settings-account" in text and "    step: done" in text)
    check("D2h 模型清单=已启用且可路由（disabled 不进清单）",
          ('id: "%s"' % M_FULL) in entry and ('id: "%s"' % M_PLAIN) in entry
          and M_HIDDEN not in entry, entry[:400])
    check("D2i 档位写成 reasoningEfforts；无档位模型不写该键",
          '"low": "low"' in entry and '"high": "high"' in entry and '"max": "max"' in entry
          and entry.count("reasoningEfforts:") == 1)
    check("D2j 容量字段来自 models_view",
          "contextWindow: 1000" in entry and "maxTokens: 100" in entry)
    check("D2k 不写旧版字段（base_url / api_key）",
          "base_url:" not in text and "api_key:" not in text)
    check("D2l agent-default-model 指向 wbrelay/<首个可路由模型>",
          "provider: wbrelay" in default_entry
          and ('model: "%s"' % first_model) in default_entry, default_entry)
    check("D2l2 切换 provider 时移除旧模型的档位行（reasoningEffort）",
          "reasoningEffort:" not in default_entry, default_entry)
    check("D2m 不再生成旧版 settings.yaml", not os.path.isfile(dsh.DSH_SETTINGS_PATH))
    cred = read(cred_path())
    check("D2n 凭据写进 refs:（引用先有值）",
          "refs:" in cred and (dsh.REF_NAME + ": " + dsh._yaml_str(KEY)) in cred, cred[:200])
    check("D2o refs 里已有键保留", "OTHER_API_KEY" in cred)
    check("D2p records 段逐字节不动",
          cred.split("records:", 1)[1] == CRED_ORIGINAL.split("records:", 1)[1],
          repr(cred.split("records:", 1)[1]))

    # ---- D3：不覆盖用户换过的默认模型 ----
    edited = text.replace('model: "%s"' % first_model, 'model: "user-picked-model"')
    write(patch_path(), edited)
    check("D3a 重注入成功", dsh._dsh_inject(PORT) is True)
    text = read(patch_path())
    check("D3b 用户选定的默认模型不被扳回",
          'model: "user-picked-model"' in text and "provider: wbrelay" in text)
    check("D3c 重注入不产生重复托管块", text.count(dsh.WB_BEGIN) == 1 and text.count(dsh.WB_END) == 1)

    # ---- D4：同步（幂等 + 带出新模型）----
    first = dsh._dsh_sync_catalog()
    before = read(patch_path())
    second = dsh._dsh_sync_catalog()
    after = read(patch_path())
    check("D4a 同步返回（可路由模型数, 隐藏数）",
          first == (2, 1) and second == (2, 1), "%r / %r" % (first, second))
    check("D4b 同步幂等（两次内容一致）", before == after)
    check("D4c 同步不动默认模型", 'model: "user-picked-model"' in after)
    write_cache([model_row(M_FULL, ["low", "high", "max"]),
                 model_row(M_PLAIN, []),
                 model_row(M_NEW, ["low"]),
                 model_row(M_HIDDEN, ["low"])])
    wb_settings._settings_cache["mtime"] = None
    wb_settings._settings_cache["data"] = None
    third = dsh._dsh_sync_catalog()
    check("D4d 新模型进清单（计数与内容同步更新）",
          third == (3, 1) and ('id: "%s"' % M_NEW) in read(patch_path()),
          "%r" % (third,))

    # ---- D7：二次备份不污染第一份快照 ----
    dsh._dsh_backup()
    bak = json.loads(read(dsh.ide_backup_path("dsh")))
    check("D7a 仍在接管时保留第一份原始快照", bak.get("raw") == original)
    check("D7b 备份只记 refs 里那一个键的旧值（不整份复制凭据）",
          bak.get("cred_ref_had") is False and "records" not in json.dumps(bak, ensure_ascii=False))

    # ---- D5：恢复 ----
    dsh._dsh_restore()
    check("D5a patch 逐字节回到注入前", read(patch_path()) == original)
    check("D5b 凭据逐字节回到注入前", read(cred_path()) == CRED_ORIGINAL)
    check("D5c 备份文件已删除", not os.path.isfile(dsh.ide_backup_path("dsh")))

    # ---- D6：只读零写入 ----
    dsh.READONLY = True
    try:
        h1 = sha(read(patch_path()))
        h2 = sha(read(cred_path()))
        ok = dsh._dsh_inject(PORT)
        check("D6a 只读下注入返回 False", ok is False)
        check("D6b 只读下 patch / 凭据零写入",
              sha(read(patch_path())) == h1 and sha(read(cred_path())) == h2)
    finally:
        dsh.READONLY = False

    # ---- D8：没有 llm-pi-ai 条目 → 追加独立托管条目 ----
    write(patch_path(), PATCH_WITHOUT_PI_AI)
    write(cred_path(), CRED_ORIGINAL)
    reset_backup()
    check("D8a 备份成功", dsh._dsh_backup() is True)
    check("D8b 注入成功", dsh._dsh_inject(PORT) is True)
    text = read(patch_path())
    check("D8c 追加独立托管条目（含 plugin name）",
          "- id: llm-pi-ai" in text and "name: '@deepseek-ai/dsh-llm-pi-ai'" in text)
    check("D8d 已有 agent-default-model 条目不重复声明",
          text.count("- id: agent-default-model") == 1)
    check("D8e 用户 !!js 表达式与其它条目原样保留",
          "!!js \"!ctx.get('profileContext')\"" in text and "developerTools: true" in text)
    dsh._dsh_restore()
    check("D8f 恢复后与注入前一致", read(patch_path()) == PATCH_WITHOUT_PI_AI)

    write(patch_path(), PATCH_ONLY_UI)
    reset_backup()
    dsh._dsh_backup()
    dsh._dsh_inject(PORT)
    text = read(patch_path())
    check("D8g 没有 agent-default-model 条目时托管块补一条",
          text.count("- id: agent-default-model") == 1
          and "provider: wbrelay" in entry_text(text, "agent-default-model"))
    dsh._dsh_restore()
    check("D8h 恢复后与注入前一致", read(patch_path()) == PATCH_ONLY_UI)

    # ---- D9：凭据文件原本不存在 ----
    write(patch_path(), PATCH_WITH_ENTRY)
    try:
        os.remove(cred_path())
    except OSError:
        pass
    reset_backup()
    check("D9a 凭据文件不存在也能接管", dsh._dsh_backup() is True and dsh._dsh_inject(PORT) is True)
    check("D9b 新建的凭据文件带 version + refs",
          read(cred_path()).startswith("version: 1") and dsh.REF_NAME in read(cred_path()))
    dsh._dsh_restore()
    check("D9c 恢复后不留空壳文件", not os.path.isfile(cred_path()))
    check("D9d patch 也回到注入前", read(patch_path()) == PATCH_WITH_ENTRY)

    # ---- D10：refs 里原本就有 WBRELAY_API_KEY → 覆盖后要还原成原值 ----
    cred_existing = CRED_ORIGINAL.replace(
        '  OTHER_API_KEY: "sk-test-FAKE-OTHER-0001"',
        '  OTHER_API_KEY: "sk-test-FAKE-OTHER-0001"\n  WBRELAY_API_KEY: "sk-test-FAKE-OLD-0002"')
    write(patch_path(), PATCH_WITH_ENTRY)
    write(cred_path(), cred_existing)
    reset_backup()
    dsh._dsh_backup()
    dsh._dsh_inject(PORT)
    check("D10a 覆盖既有 ref 为当前网关 Key",
          (dsh.REF_NAME + ": " + dsh._yaml_str(KEY)) in read(cred_path()))
    check("D10b 备份记下了旧值",
          json.loads(read(dsh.ide_backup_path("dsh"))).get("cred_ref_had") is True)
    dsh._dsh_restore()
    check("D10c 恢复成原来的旧值（逐字节）", read(cred_path()) == cred_existing)
    # ---- D12：凭据文件是「真实排版」（version 与 records 之间没有空行、没有 refs 段）----
    cred_real = "\n".join([
        "version: 1",
        "records:",
        "  deepseek-account-platform/device:",
        "    kind: grant",
        "    payload:",
        "      id: 00000000-0000-4000-8000-000000000000",
        "",
    ])
    write(patch_path(), PATCH_WITH_ENTRY)
    write(cred_path(), cred_real)
    reset_backup()
    dsh._dsh_backup()
    dsh._dsh_inject(PORT)
    check("D12a 无 refs 段时新插入（不自造空行）",
          read(cred_path()) == "version: 1\nrefs:\n  " + dsh.REF_NAME + ": " + dsh._yaml_str(KEY) + "\nrecords:\n"
          + "  deepseek-account-platform/device:\n    kind: grant\n    payload:\n"
          + "      id: 00000000-0000-4000-8000-000000000000\n",
          repr(read(cred_path())))
    dsh._dsh_restore()
    check("D12b 还原逐字节（真实排版不留多余空行）", read(cred_path()) == cred_real,
          repr(read(cred_path())))
    # ---- D11：profile 还没有 cordis.patch.yml（首次接管会新建、还原会删掉）----
    write(cred_path(), CRED_ORIGINAL)
    try:
        os.remove(patch_path())
    except OSError:
        pass
    reset_backup()
    check("D11a 没有 patch 文件也能接管", dsh._dsh_backup() is True and dsh._dsh_inject(PORT) is True)
    check("D11b 新建的 patch 只有托管块（+ 补的默认模型）",
          dsh.WB_BEGIN in read(patch_path()) and "- id: llm-pi-ai" in read(patch_path()))
    dsh._dsh_restore()
    check("D11c 恢复后不留空壳 patch", not os.path.isfile(patch_path()))
    check("D11d 凭据也回到注入前", read(cred_path()) == CRED_ORIGINAL)
    print("\n断言：PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for n in FAIL:
            print("  - " + n)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
