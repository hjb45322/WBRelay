# 方案 ・ DSH 接管修复（检测面 + 配置面重写）

> 状态：实施中（2026-10-14）
> 关联：`AGENTS.md`（R5 / R7、Python 3.9、原子写、只读降级）、`docs/规则-隔离与数据保护.md`（验收）、`docs/方案-架构拆分.md`（`wbrelay/ides/` 子包）
> 触发：DSH 桌面端已安装，但看板一直显示「未检测到」，接管开关点不动。

---

## 一、问题

| # | 现象 | 影响 |
|---|---|---|
| S1 | DSH 已安装（`%LOCALAPPDATA%\Programs\DeepSeek Harness\`），看板 IDE 条仍显示「未检测到」 | 开关被禁用，接管完全不可用 |
| S2 | 即使绕过检测强行注入，也不会生效 | 注入写的是 DSH 0.2 不读的旧文件/旧字段 |
| S3 | 接管后 DSH 里不会出现 WBRelay 的模型 | 模型清单从未同步（`_dsh_sync_catalog()` 恒返回 `(0, 0)`） |

---

## 二、取证（只读，2026-10-13/14）

### 2.1 本机事实

| 项 | 事实 |
|---|---|
| DSH 版本 | 桌面端 **0.2.0-rc.2**（`resources/app.asar!/dsh/desktop-runtime.json`，Electron 44.0.0） |
| 安装位置 | `%LOCALAPPDATA%\Programs\DeepSeek Harness\DeepSeek Harness.exe` |
| harness home | `~/.dsh/`：`profiles/`、`sessions/`、`storages/`、`.credentials.yaml` |
| **`~/.dsh/settings.yaml`** | **不存在**（整个用户目录 depth-3 检索 0 命中） |
| profile 配置面 | `~/.dsh/profiles/desktop/{cordis.yml, cordis.patch.yml, package.json}` |
| 现场状态 | `GET /api/ides` → `{"id":"dsh","detected":false}`；`ide_backups/` 里无 `dsh.json`；`wb.log` 全文无 DSH 行 |

### 2.2 客户端侧口径（从 `app.asar` 只读解出的内置文档与组合补丁）

| 结论 | 证据 |
|---|---|
| `settings.yaml` 是旧版遗留 | `@deepseek-ai/dsh-settings/README.zh.md`：「早期版本留在 harness home 中的 `settings.yaml` 会被导入一次……文件在第一次写入前改名为 `settings.yaml.imported`」 |
| 配置面是 profile 用户 patch 层 | `@deepseek-ai/dsh-app-boot`：profile 位于 `$DSH_HOME/profiles/<name>`，patch 层顺序 = bundle patch → profile 的 `cordis.patch.yml` → home 级 `cordis.patch.yml`；用户 patch 在长会话面上**热重载**；「**按 id 定位的 patch 会替换匹配到的整个配置，不做深度合并**」 |
| 路由面是 `llm-pi-ai` 的 `providers` 字典 | `@deepseek-ai/dsh-base/cordis.patch.yml`：`- id: llm-pi-ai` 休眠挂载，注释写明「until a `llm-pi-ai:` settings section supplies provider profiles… Supplying those profiles is exactly what the web Models page does」 |
| 路由字段 | `@deepseek-ai/dsh-llm-pi-ai/README.zh.md`：`apiKeyEnv / displayName / api / baseURL / models / modelOverrides / compat / defaultContextWindow / defaultMaxTokens / retryPolicy`；非目录内路由**必须**给 `api` + `baseURL`，模型条目要 `contextWindow` / `maxTokens`（缺省回落 262144 / 32768） |
| 可用 `api` 取值 | `openai-completions`、`openai-responses`、`anthropic-messages`、`azure-openai-responses`、`openai-codex-responses`、`bedrock-converse-stream`（`dsh-llm-pi-ai/lib/index.js` 字面量） |
| 凭据面 | `@deepseek-ai/dsh-credentials-local/README.zh.md`：`~/.dsh/.credentials.yaml` 为 `version / refs / records` 三段式，**可直接编辑、自动重载**；`apiKeyEnv: X` 解析顺序 = 启动环境 → `refs` → 项目 `.env` → home `.env`；空值被拒绝、重复键被拒绝 |
| 默认模型面 | `@deepseek-ai/dsh-agent-default-model`：config = `{provider, model, reasoningEffort?}`；保存的选择写进当前 profile patch |

### 2.3 根因

- **R-1（检测）**：`_dsh_detect()` 只认 `~/.dsh/settings.yaml`；DSH 0.2 从不产生该文件 → 恒 `False`。
- **R-2（注入）**：`_dsh_inject()` 往不存在的 `settings.yaml` 写 `base_url` / `api_key` —— 目标文件错、字段名也错（正确字段是 `providers.<route>.baseURL`）。
- **R-3（模型）**：`_dsh_sync_catalog()` 恒返回 `(0, 0)`，自定义路由的 `models` 从不写入 → DSH 模型选择器里 0 个模型。
- **R-4（默认模型）**：接管不改 `agent-default-model` → 新会话仍走 `deepseek-account/deepseek-flash`。
- **R-5（凭据）**：旧实现把 `.credentials.yaml` **整份**复制进 `%APPDATA%\WBRelay\ide_backups\dsh.json`，恢复时整份回写；该文件含 DSH 自己的真实凭据（登录 grant / platform token），既扩大暴露面，也会在 DSH 轮换 token 后用旧快照覆盖。
- **R-6（日志撒谎）**：`_dsh_write()` 在只读模式返回 `False`，调用方不看返回值仍打印「已指向 WBRelay」并 `return True`。

---

## 三、目标配置面（接管后应有的形态）

`~/.dsh/profiles/desktop/cordis.patch.yml`：

```yaml
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      # >>> WBRelay managed block（由 WBRelay 自动维护，请勿手工编辑）>>>
      wbrelay:
        displayName: WBRelay
        api: openai-completions
        baseURL: "http://127.0.0.1:8789/v1"
        apiKeyEnv: WBRELAY_API_KEY
        models:
          - id: "gpt-6.1-sol"
            contextWindow: 262144
            maxTokens: 32768
            reasoningEfforts:
              low: low
              high: high
      # <<< WBRelay managed block <<<
- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: wbrelay
    model: "gpt-6.1-sol"
    # 旧模型的 reasoningEffort 行由接管一并移除（档位属于旧模型，可能与新模型能力不匹配）
```

`~/.dsh/.credentials.yaml`：`refs:` 下新增一个键

```yaml
version: 1

refs:
  WBRELAY_API_KEY: "wb-…"
```

---

## 四、改动

### 4.1 `wbrelay/ides/dsh.py`（整篇重写，唯一代码改动点）

- **路径**：`$DSH_HOME`（非空白优先）否则 `~/.dsh`；profile 发现 = `profiles/*/` 中带 `cordis.yml` / `cordis.patch.yml` / `package.json` 的目录，`desktop` → `web` → 首个。
- **`_dsh_detect()`**：能找到可接管的 profile 即「已检测到」；`settings.yaml` 不再参与判定（旧版遗留，只读探测仅用于日志提示）。
- **`_dsh_backup()`**：只备份「即将改动的最小面」——profile patch 原文 + `refs` 里 `WBRELAY_API_KEY` 的旧值；**绝不整份复制 `.credentials.yaml`**。若检测到当前 patch 已有托管块（上次接管残留），保留第一份原始快照，不被二次注入污染。
- **`_dsh_inject(port)`**：
  1. 先写凭据 ref（保证引用先有值），再写 patch；任一步无法安全进行即放弃，且**不写任何文件**。
  2. 路由块写进**已有的**顶层 `- id: llm-pi-ai` 条目（DSH 设置页写的就是它）的 `providers:` 下；没有该条目时追加一条独立托管条目。
  3. 托管块用 `# >>> WBRelay managed block … >>>` / `# <<< WBRelay managed block <<<` 包裹，重注入时先整体摘除再重写 → 幂等、可精确撤销。
  4. 模型清单来自 `_collect_models_for_ide()` ∩ `_route_models_set()`（与 OpenCode 适配器同一口径：只列「已启用且可路由」的模型），档位来自 `reasoning_efforts` → pi-ai 的 `reasoningEfforts`。
  5. `agent-default-model`：只改 `provider` / `model` 两行，并移除属于旧模型的 `reasoningEffort` 行（该字段可选，留着可能与新模型的能力对不上）；**若已经指向 `wbrelay` 则整个条目原样保留**（不覆盖用户在 DSH 里换过的模型）。
  6. 不改用户其余内容（其它条目、注释、`!!js` 表达式原样保留）；文本级改写，**零 YAML 依赖**（核心只用标准库）。
- **`_dsh_restore()`**：patch 原文回写（逐字节）+ `refs` 键删除/还原；两者都成功才删备份文件。
- **`_dsh_sync_catalog()`**：只刷新托管路由里的 `models`（端口从现有托管块解析），内容不变则不写；未接管（无托管块）时直接返回 `(0, 0)` 不写盘。
- **`restart_dsh()`**：找不到 exe 一律不杀进程；杀/起仍只由看板**显式点击**触发（R5）。
- 所有写入走 `atomicio.write_text`（tmp + `os.replace`）；`--readonly` / `--no-ide-inject` 双闸门不变；`_dsh_write()` 返回值必须被检查。

### 4.2 测试

- 新增 `tests/test_dsh_inject.py`（D1–D11，56 断言）：检测面、注入形态、模型口径、`agent-default-model` 切换（含「已指向则不覆盖」与移除旧档位行）、其它内容保留、幂等、同步带出新模型、恢复逐字节、只读零写入、patch / 凭据文件原本不存在时的清理、既有 refs 的覆盖与还原。
- 既有 `tests/test_readonly_zero_write.py` 的 IDE 零写入探针覆盖 DSH（`ides_backup_and_inject` / `ides_restore` 已在被调清单里）。

### 4.3 文档

- 本文件；`AGENTS.md` §八 索引登记；`README.md` 的 IDE 接管段落补 DSH 配置面；`CHANGELOG.md` 记录本条修复。

---

## 五、语义边界（不可破坏）

1. **核心语义不动**：失败必切换 / 保存即清冷却 / 全败必报错与本方案无关。
2. **R1 / R2**：只碰 `~/.dsh`（DSH 自己的配置面），不碰 `%APPDATA%\WBRelay-legacy-*` 与 `~/.codex`。
3. **R5**：不自动启停任何外部进程；DSH 重启只在看板显式点击时发生。
4. **R7**：本方案不改网关进程管理；改动生效仍走 `tools/restart-gateway.py`。
5. **只读零写入**：`--readonly` / `--no-ide-inject` 下 `~/.dsh` 与数据目录均零写入。
6. **不改 OpenAI/Codex 语义**：DSH 走 `openai-completions`（chat）路径，与 Codex 的 responses 路径互不影响。

---

## 六、验收

| 项 | 方法 |
|---|---|
| 单测全绿 | `python tests/test_dsh_inject.py`、`tests/test_opencode_inject.py`、`tests/test_readonly_zero_write.py`、`tests/test_routing_semantics.py`、`tests/exercise_core.py`、`tests/test_package_layout.py` |
| 隔离断言 | 每个测试内置 `isolate.assert_isolated()`；`~/.dsh` 用临时根替代 |
| 无敏感信息入库 | `python tools/scan_sensitive.py` |
| 实机 | 看板显示「已检测到」→ **显式点击**接管 → patch 只多出托管块、`agent-default-model` 两行、`refs` 一个键 → DSH 模型选择器出现 WBRelay 模型 → 发消息成功（用量 `realm=supplier:<id>`） |
| 回滚 | 关闭开关 → patch 逐字节回到注入前、`refs` 键删除、`ide_backups/dsh.json` 消失 |
| 只读 | `python relay.py --readonly --no-ide-inject --port <非 8789> --headless` 下 `~/.dsh` 哈希前后一致 |

---

## 七、实机验收记录（2026-10-14，打包实例）

| 步骤 | 结果 |
|---|---|
| 打包 | `pyinstaller WBRelay.spec` 重建 `dist\WBRelay.exe`（两次：00:28:04 / 00:30:12）；从 PYZ 取 `wbrelay.ides.dsh` 验证新标记与新函数在包内 |
| 值守重启（R7） | 两次：00:28:34 派发 → 00:28:43 就绪；00:30:20 派发 → 00:30:29 就绪（新 pid 82104），期间网关中断约 2 秒 |
| 检测 | `GET /api/ides` → `{"id":"dsh","detected":true,"enabled":false}`（修复前恒 false） |
| 接管 ON | patch 只多出托管块 + `agent-default-model` 两行（并移除旧 `reasoningEffort` 行）；`.credentials.yaml` 只多 `refs.WBRELAY_API_KEY` 一个键；`ide_backups\dsh.json` 1137 B，**不含**整份凭据 |
| 接管 OFF | patch 与凭据**逐字节**回到接管前（SHA256 `DD1B8C1B…` / `3468A520…`），备份文件删除 |
| DSH ↻ 重启 | `{"ok": true, "killed": "(无)", "launched": true}` → DSH 桌面端拉起（5 个进程）；启动后 patch 未被改写（哈希不变） |
| 端到端 | 按 DSH 的协议打 `POST /v1/chat/completions`（Bearer = refs 里那个 key）：200，1.2s，用量 `realm=supplier:wb-cn` |
| 冻结产物 | `tests/frozen_onefile_ownership.py --exe dist\WBRelay.exe` 8/8 PASS |

> 实机测试期间发现并修掉一个真缺陷：凭据文件在「`version:` 与 `records:` 之间没有空行」的真实排版下，
> 还原后会多出一个空行（不是逐字节）。修法：插入 `refs:` 段时**不自造空行**，保持文件原有分段排版；
> 新增 `tests/test_dsh_inject.py` D12 作为回归用例。

---

## 八、已知限制与后续项（不在本轮）

- **与 DSH 设置页抢写同一文件**：DSH 的「设置 → 模型」写入带 revision，外部改 `cordis.patch.yml` 会让它下一次保存报 `settings/conflict`；接管后建议先在 DSH 里刷新/重启再编辑模型页。本轮不做协商或锁。
- **恢复是「整文件回写」**：与 OpenCode 适配器同一策略；若接管期间用户在 DSH 里改了其它设置，关闭开关会回到注入前的快照。后续可考虑按托管块增量撤销。
- **`api:` 取值**：本轮固定 `openai-completions`（网关侧 chat 路径已由 OpenCode 修复补齐翻译）。`openai-responses` 作为备选，需要实机对比后再定。
- **多 profile**：只接管「找到的第一个可接管 profile」（`desktop` 优先），不做多 profile 广播。