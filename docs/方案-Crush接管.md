# 方案 ・ Crush 接管（取证 + 新适配器）

> 状态：已实施（2026-10-14）
> 关联：`AGENTS.md`（R2 / R5、Python 3.9、原子写、只读降级）、`docs/规则-隔离与数据保护.md`、
> `docs/方案-IDE接管对比与优化.md`（§10 本轮规格：Crush 是 O12）
> 前置：DSH 修复（`8425778`）、OpenCode 档位修复（`fe7301e`）已落地

---

## 一、结论速览

- Crush 走 **OpenAI 兼容**协议，请求打到 `/v1/chat/completions`——**网关已经有这个入口**，不需要动协议层。
- 配置面是**全局数据层的 JSON**：Windows `%LOCALAPPDATA%\crush\crush.json`、Unix `~/.local/share/crush/crush.json`。
- 官方把 JSON 格式标为 **deprecated**（新格式是 shell 风的 `crushrc`），但**仍然支持**；本轮只接管 JSON 层，不碰 `crushrc`。
- 自定义 provider 的 `type` 必须是 **`openai-compat`**；写成 `openai` 是「经 OpenAI 代理」的语义。
  **反代项目（`wb_agents.py:619`）写的就是 `type: "openai"`，属于错误示范，不能照抄。**

---

## 二、取证（只读，2026-10-14）

取证对象：公开仓库 `charmbracelet/crush`，**pin 到 commit `d6edc3222231a0177b88d073733413578c9a1e01`**（2026-10-13）。

### 2.1 配置面位置（源码 `internal/config/load.go`）

| 函数 | 优先级 |
|---|---|
| `GlobalConfig()`（1212 行） | `CRUSH_GLOBAL_CONFIG` → 平台默认（`crushrc`，shell 风） |
| `GlobalConfigData()`（1259 行） | `CRUSH_GLOBAL_DATA` → `XDG_DATA_HOME` → Windows `%LOCALAPPDATA%\crush\crush.json`；Unix `~/.local/share/crush/crush.json` |

源码注释原文：`// for windows, it should be in \`%LOCALAPPDATA%/crush/\``。

README「Configuration」一节另给出 shell 配置（`crushrc`）的优先级：
`./.crushrc` → `./crushrc` → `~/.config/crush/crushrc`，并写明 JSON 格式「still supported, but … deprecated」。

> **反代项目的 `~/.config/crush/crush.json` 是 POSIX 路径假设**，在 Windows 上并不存在；本轮不采用。

### 2.2 provider 契约（`schema.json`）

- `ProviderConfig` 字段：`id / name / base_url / type / api_key / oauth / disable / system_prompt_prefix /
  extra_headers / extra_body / provider_options / aws_auth_refresh / flat_rate / discover_models / models`
- `Model` 字段：`id / name / cost_per_1m_in / cost_per_1m_out / cost_per_1m_in_cached / cost_per_1m_out_cached /
  context_window / default_max_tokens / can_reason / reasoning_levels / default_reasoning_effort /
  supports_attachments / options`
- `SelectedModel` 字段：`model / provider / reasoning_effort / think / max_tokens / …`
- Config 顶层：`$schema / models / providers / mcp / lsp / options / permissions / tools / hooks / env`

README「Custom Providers」原文：

> `openai` should be used when proxying or routing requests through OpenAI.
> `openai-compat` should be used when using non-OpenAI providers that have OpenAI-compatible APIs.

### 2.3 安全边界（README 明确警告）

> Both `crushrc` and `crush.json` are trusted code; … any `$(...)` in `crush.json` runs at load time.

→ 我们对 Crush 的所有写入**只动自己的 `providers.wbrelay` 段**，绝不引入 `$(...)`、不执行 `crushrc`、不参与 shell 配置。

---

## 三、实现（`wbrelay/ides/crush.py`）

| 面 | 做法 |
|---|---|
| 路径解析 | `CRUSH_GLOBAL_DATA` → `XDG_DATA_HOME/crush` → Windows `%LOCALAPPDATA%\crush` → Unix `~/.local/share/crush`；不硬编码 |
| 探针 | 三重：配置文件 ∪ 配置目录 ∪ `shutil.which("crush")` |
| 备份 | `ide_backups/crush.json` = `{raw, existed}`（逐字节原文 + 「原来是否存在」）；已有备份则不覆盖（保留首份原始快照） |
| 注入 | 只写 `providers.wbrelay`（`type: openai-compat`、`base_url`、`api_key`、`models`、`discover_models: false`）；`models.large` / `models.small` 若未指向 `wbrelay` 则一并指向首个可路由模型 |
| 模型口径 | `_collect_models_for_ide()` ∩ `_route_models_set()`（与 OpenCode / DSH 同一口径：只列已启用且可路由的模型）；档位写 `reasoning_levels` |
| 同步 | `sync_catalog` 只刷新 `providers.wbrelay.models`；未接管时不动；内容没变不写盘 |
| 还原 | 逐字节回写原文；原本不存在的文件直接删除（回到「没有这个文件」）；成功才删备份 |
| 进程 | **不管理进程**：Crush 是跑在终端里的 TUI，`restartable=False`，看板不显示「重启」按钮，接口返回「请重启终端会话」 |
| 只读 | 写入一律经 `atomicio.write_text`，`READONLY` 下注入/还原/同步全部返回拒绝（零写入） |

---

## 四、验收

| 项 | 方法 | 结果 |
|---|---|---|
| 单元/契约 | `python tests\test_ide_takeover.py`（含 Crush 注入形态、还原逐字节、`openai-compat` 断言） | 22 项全绿 |
| 只读零写入 | `tests\test_readonly_zero_write.py` | 43 通过 / 0 失败 |
| 包与路径 | `tests\test_package_layout.py` / `test_data_path_policy.py` | 23 / 28 全绿 |
| 敏感扫描 | `python tools\scan_sensitive.py --identifiers tools\release_identifiers.txt` | 本方案文档**不新增**阻断命中；整体门禁仍因既存的 `VENDOR-PENDING` 绊线与 reflog 悬空对象**未通过**（见 `docs/方案-开源发布阻断修复-第三轮.md`） |

---

## 五、边界与已知限制（本轮不做）

1. **没有实机端到端验收**：本机未安装 Crush（已核 `%APPDATA%\crush\`、`~/.config/crush\`、PATH 均无）。
   本轮证据是**官方源码 + schema + README**；装上 Crush 后需要补一次「模型出现在选择器里 → 发消息成功 → 用量记 `realm=supplier:<id>`」的实机验收。
2. **只接管全局数据 JSON**：不接管项目/工作区级 `.crushrc`、也不接管 `crushrc`（shell 风新格式）。
   项目级配置优先级高于全局，**可以覆盖我们的 provider 选择**——归属判定只看全局 JSON，不追项目级覆盖。
3. **JSON 格式已 deprecated**：官方若未来移除 JSON 支持，本适配器要跟着迁到 `crushrc`；届时需要一个新的文本级编辑器。
4. **`supports_attachments` 写 false**：保守做法（不替用户承诺图片能力），模型若确有视觉能力可在 Crush 里自行打开。
5. **不改 `$schema`**：写入时不补也不改 `$schema` 字段，保持用户原样。