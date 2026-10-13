# 方案 ・ IDE 接管：与反代项目的对比与优化

> 状态：待确认（2026-10-14）
> 关联：`AGENTS.md`（R2 / R5 / R6 / R7、Python 3.9、原子写、只读降级）、`docs/规则-隔离与数据保护.md`（验收）、
> `docs/方案-隔离机制.md`（IDE 注入三套开关）、`docs/方案-DSH接管修复.md`（**正在实施中**）、`docs/方案-OpenCode接管修复.md`
> 触发：对比「反代项目接管 IDE 的方式」与「本项目的接管方式」，找可借鉴点；并回答「另外 2 个没有添加的 IDE 能否加进来」。
> 本轮范围：**只出方案，不改代码**。全部取证为只读访问。

---

## 一、结论速览

### 1.1 两边的做法不是同一种东西

| | 反代项目（`wb_agents.py`） | 本项目（`wbrelay/ides/`） |
|---|---|---|
| 触发模型 | **纯按需**：看板点「一键配置」才写，点「一键还原」才回 | **跟随网关生命周期**：启动自动注入、退出自动还原 |
| 落盘配置 | 独立账本 `integration-state.json` | 复用 `settings.json` 的 `ides.<id>.enabled` |
| 适配器接口 | `files()` + `probe()` + `apply()`，`apply` 是**纯函数**（算内容，不写盘） | `detect/backup/inject/restore/sync_catalog/restart`，**各自写盘** |
| 备份/还原 | **逐字节**原件备份 + 记「我写进去的 sha256」 | 字段级快照（Codex 只存 `base_url`/`token`），只有 DSH/OpenCode 是文本级 |
| 客户端自愈 | **重启客户端**（看板显式点击，R5） | 反代项目没有这个能力 |
| 只读边界 | 无 | `--readonly` / `--no-ide-inject` 双闸门 + 原语级兜底 + 零写入测试 |

### 1.2 三句话结论

1. **反代项目整体不如我们「稳」，但比我们「细」**：它的注入接口、备份/还原、外部改动感知、看板信息量都更完整，值得我们借鉴；
   它的 DSH 与 OpenCode 两个适配器反而是**旧的**（DSH 还在写 0.2 已不读的 `settings.yaml`），这两处不能反向抄。
2. **最值得抄的 5 件事**：①检测面三重探针（装了就认得出）②`apply()` 纯函数 + 两阶段提交 ③逐字节还原
   ④外部改动感知（sha256）⑤看板补「路径 / 上次接管时间 / 外部改动」并加「其他客户端手动接入表」。
3. **2 个未接入的 IDE**：**Crush 可以加**（OpenAI 兼容，我们协议已支持，成本低）；
   **Claude Code 不能只加适配器**——它只说 Anthropic Messages，而我们的网关**没有 `/v1/messages`**，必须先补协议入口，应单独立项。

---

## 二、取证

### 2.1 反代项目怎么做的

源码位置：`<反代项目目录>\wb_agents.py`（909 行，单文件，纯标准库）；路由层 `wb_proxy.py`；前端 `dashboard.html`；
设计说明 `docs/CONTRIBUTING-智能体配置.md`。

**（1）客户端注册表（`wb_agents.py:627-705`）**——5 个客户端：

| id | label | 写入位置 | 协议 |
|---|---|---|---|
| `claude-code` | Claude Code | `~/.claude/settings.json`（认 `CLAUDE_CONFIG_DIR`） | anthropic |
| `codex` | Codex CLI | `~/.codex/config.toml` + `~/.codex/auth.json`（认 `CODEX_HOME`） | openai |
| `opencode` | OpenCode | `~/.config/opencode/opencode.json` | openai |
| `dsh` | DeepSeek Harness | `~/.dsh/settings.yaml` + `~/.dsh/.credentials.yaml` | openai |
| `crush` | Crush | `~/.config/crush/crush.json` | openai |

每个条目只有 6 个字段：`id / label / desc / protocol / files(home) / probe(home) / apply(paths, ctx)`。
`apply` **不写盘**，只返回 `{绝对路径: 最终文本}`（`CONTRIBUTING-智能体配置.md` §3）。

**（2）探针（`probe`）是「三重检测」**：配置目录存在 **或** 配置文件存在 **或** `shutil.which("<命令名>")` 命中 PATH
（`CONTRIBUTING-智能体配置.md` §3.1）。

**（3）注入上下文 `ctx`**：`base_url` / `base_url_v1` / `api_key` / `model` / `models[]`，由前端表单决定
（网关地址 + API Key（可选「全局」或指定某个 Key）+ 默认模型）。

**（4）写入流程 `integrate()`（`wb_agents.py:769-863`）两阶段**：

1. 先算：`apply()` 产出全部目标文件的新内容；
2. 再写：逐个 `_atomic_write_text()`（tmp + `os.replace`），**任一失败就把已写的按备份原子回滚**，新文件则删掉；
3. 账本 `integration-state.json` 记 `applied_at / base_url / model / files[{path, existed, backup, sha256, bytes}]`；
4. 备份是**原件字节**，落在 `agent-backups/<client-id>/<时间戳>-<文件名>`，每文件保留 10 份（`BACKUP_KEEP=10`），
   但**首次接入前的那一份永久锁定**，后续重复 apply 不覆盖它。

**（5）还原 `restore()`（`wb_agents.py:866-909`）**：逐字节回写原件；「原来不存在、是我们建出来的」文件直接删除，
并向上清理空目录（`_prune_empty_parents`，只删空目录、止于 home），避免探针把「只剩我们配置的目录」误判成已安装。

**（6）外部改动感知**：账本里存了「我们上次写进去的内容 sha256」；`overview()` 重新哈希现文件比对，
不一致就在看板标 `external_change`（`CONTRIBUTING-智能体配置.md` §6.4）。

**（7）护栏**：`MAX_CONFIG_BYTES = 8 * 1024 * 1024`，超限直接拒绝编辑；非 UTF-8 直接报错不写。

**（8）前端（`dashboard.html:7957` 起）**：独立「智能体配置」页 =
顶部连接表单（网关地址 / API Key / 默认模型）+ 客户端卡片网格（状态徽标「已配置 / 已检测到 / 未检测到」、配置路径、
上次配置时间、外部改动警示、一键配置 / 一键还原）+「重新检测」按钮 + 页脚「其他客户端」手动接入表
（OpenAI 兼容 Base URL、Anthropic Messages Base URL，各带复制按钮）。

**（9）生命周期**：`wb_agents` 只在三个 handler 里被调用（`wb_proxy.py:11232/11306/11324`），
**启动不注入、退出不还原**——写进去就长期有效，直到用户点还原。

**（10）网关侧前置能力**：反代项目自己实现了原生 Anthropic Messages（`wb_proxy.py:8262-8919`，约 660 行），
所以 Claude Code 那张卡才点得通。
### 2.2 本项目怎么做的

**（1）适配器注册表（`wbrelay/ides/__init__.py:30-57`）**——3 个客户端，每个 8 个字段：
`id / name / detect / backup / inject / restore / restart / sync_catalog / restartable`。

**（2）生命周期（`wbrelay/app.py:249-263`）**：启动时 `ides_backup_and_inject(port)`；
`atexit` + `SIGINT/SIGBREAK/SIGTERM` 处理器调用 `ides_restore()`；headless 与浏览器降级分支退出时也各调一次
（`app.py:274 / 296`）。也就是「**网关在 → 客户端指向网关；网关走 → 客户端回原样**」。

**（3）开关**：持久化在 `settings.json` 的 `ides.<id>.enabled`（`wbrelay/ides/base.py:ides_settings`），
看板开关即时生效（`POST /api/ides/{id}/toggle`，`wbrelay/web/api_state.py:84-112`）。

**（4）各适配器现状**：

| 适配器 | 检测（`detect`） | 备份（`backup`） | 还原（`restore`） | 是否写默认模型 |
|---|---|---|---|---|
| `codex` | `config.toml` 存在 | `ide_backups/codex.json` = `{base_url, experimental_bearer_token}` | **只回写这 2 个字段** | 否 |
| `opencode` | `opencode.json` 存在 | `{"raw": 原文}` | 逐字节回写原文 | 否 |
| `dsh` | 找得到 profile 的用户 patch 面 | patch 原文（逐字节）+ `refs` 旧值；**不整份复制 `.credentials.yaml`** | patch 逐字节回写 + refs 键还原/删除 | 是（`agent-default-model`） |

**（5）只读边界**：`--readonly` / `--no-ide-inject` 两个全局闸门（`ide_write_globally_blocked()`），
外加每个适配器写入原语里的第二道防线（`_dsh_write` / `_opencode_write` / `_write_codex_config` 各自判 `READONLY`），
并有 `tests/test_readonly_zero_write.py` 钉住「零写入」。**这是反代项目没有、我们必须保住的东西。**

**（6）我们独有的能力**：看板「↻ 重启」显式点击重启客户端（Electron 客户端杀进程 + 重新拉起，
`restart_dsh()` / `restart_opencode()` / `restart_codex()`），R5 要求只由显式点击触发。

**（7）看板（`dashboard.html:1215-1275`）**：设置页一条卡片 strip，每张卡只有 名称 + 「已检测到 / 未检测到」徽标 + 开关 + 重启按钮。

### 2.3 本机环境事实（只读核对）

| 对象 | 事实 |
|---|---|
| OpenCode 配置 | `~/.config/opencode/opencode.json` **存在** |
| DSH | `~/.dsh` **存在** |
| Claude Code | `~/.claude/` **不存在**；`claude` 不在 PATH |
| Crush | `%APPDATA%\crush\`、`~/.config/crush\` **都不存在**；`crush` 不在 PATH |

> 影响：**这两个客户端本机都没装**，按本项目「先取证再写代码」的工作流（DSH / OpenCode 两个修复方案都是逐行取证出来的），
> 现在无法做实机验证。Crush/Claude Code 的路径与字段必须先用官方文档或实机安装取证，**不能照抄反代项目**（理由见 §4.3）。

---

## 三、逐项对比

| # | 维度 | 反代项目 | 本项目 | 判断 |
|---|---|---|---|---|
| 1 | 触发 | 手动一键配置 / 一键还原 | 开关 + 启动注入 + 退出还原 | **各有道理**，见 §5.O11 |
| 2 | 探针 | 目录 ∪ 文件 ∪ `which` | 只认配置文件 / profile 目录 | **借鉴他们的**（O1） |
| 3 | 适配器接口 | `apply()` 纯函数，聚合层统一提交 | `inject()` 自己写盘 | **借鉴他们的**（O2） |
| 4 | 多文件事务 | 两阶段 + 失败回滚 | 各文件独立写（DSH 靠顺序降低风险） | **借鉴他们的**（O2） |
| 5 | 备份粒度 | 原件逐字节 + 保留 10 份 | 字段级快照（Codex/OpenCode），DSH 逐字节 | **借鉴，但要分级**（O3） |
| 6 | 还原完整性 | 逐字节回到首次接入前 | Codex **还原不彻底**（见 §5.O3 依据） | **借鉴他们的**（O3） |
| 7 | 外部改动感知 | 记账 sha256，看板警示 | 仅 Codex 还原时有一个「base_url 是否还是我们的」守卫 | **借鉴他们的**（O4） |
| 8 | 大文件 / 编码护栏 | 8MB 上限 + UTF-8 校验 | 无 | **借鉴他们的**（O5） |
| 9 | 看板信息量 | 路径 / 时间 / 文件数 / 外部改动 / 重新检测 | 只有名字 + 徽标 + 开关 | **借鉴他们的**（O6） |
| 10 | 手动接入指引 | 页内表格 + 复制按钮 | 只在 README 里 | **借鉴他们的**（O7） |
| 11 | 密钥落盘 | 明文写进客户端配置 | 明文写进客户端配置（DSH 用 refs 已经很干净） | 参考（O9） |
| 12 | 客户端重启 | 无，让用户自己重启 | 看板显式点击重启 | **我们更好**，保持 |
| 13 | 只读边界 | 无 | 双闸门 + 原语级兜底 + 零写入测试 | **我们更好**，保持 |
| 14 | DSH 适配 | 写 `~/.dsh/settings.yaml`（0.2 已不读） | profile patch + 托管块 + refs | **我们更好**，不能反向抄 |
| 15 | OpenCode 适配 | `models[id] = {}`（无档位、无 limit） | `variants` + `limit`（已逐行取证） | **我们更好**，不能反向抄 |
| 16 | Codex 适配 | 追加 `[model_providers.wb-proxy]` + 改 `auth.json` | 重命名既有 provider 段 + 改 `base_url` + 加 `model_catalog_json` | **他们的更干净**（O2/O3 顺带处理） |
---

## 四、借鉴什么、保持什么、不要抄什么

### 4.1 值得借鉴（8 项）

| # | 借鉴点 | 出处 | 对我们的价值 |
|---|---|---|---|
| 1 | 探针三重检测（目录 ∪ 文件 ∪ PATH） | `CONTRIBUTING-智能体配置.md` §3.1 | 「装了却没跑过」不再显示未检测到，开关不再点不动 |
| 2 | `apply()` 纯函数 + 聚合层统一提交 | `wb_agents.py:769-847` | 多文件原子性、可测试、可预览、可「无变化不写」 |
| 3 | 两阶段写入 + 失败回滚 | `wb_agents.py:804-847` | DSH 的 patch + credentials 不再可能写半套 |
| 4 | 逐字节还原 + 清理空目录 | `wb_agents.py:866-902` | 还原真的回到接管前；避免「只剩我们的配置」被误判成已安装 |
| 5 | 记「我写进去的 sha256」→ 外部改动感知 | `CONTRIBUTING-智能体配置.md` §6.4 | 接管期间用户手改配置能被看见，不被静默覆盖 |
| 6 | 8MB 上限 + UTF-8 校验 | `wb_agents.py:26,53-73` | 防止把用户的大文件读进来再整体重写 |
| 7 | 卡片信息量（路径 / 时间 / 外部改动 / 重新检测） | `dashboard.html:8074-8160` | 出问题能自查，公开项目少一半「接管没生效」issue |
| 8 | 页内「其他客户端」手动接入表 | `dashboard.html:3376-3396` | 一行字 + 一个复制按钮，覆盖所有我们没适配的客户端 |

### 4.2 我们更好，必须保持

| # | 我们独有的东西 | 为什么不能丢 |
|---|---|---|
| 1 | `--readonly` / `--no-ide-inject` 双闸门 + 原语级兜底 + `tests/test_readonly_zero_write.py` | 反代项目没有等价边界；这是公开项目的安全底线 |
| 2 | 看板显式点击重启客户端（R5） | 接管后不用手动关开应用，反代项目只能提示用户自己重启 |
| 3 | DSH 的「最小面备份 + 托管块 + refs」 | 反代项目会把 DSH 自己的凭据整份复制进备份；我们这个设计是刻意的（R-5） |
| 4 | 与网关生命周期绑定（启动注入 / 退出还原） | 用户停用工具后不留残留；见 §5.O10 的复审 |

### 4.3 绝对不能反向抄的两处

1. **DSH**：反代项目写 `~/.dsh/settings.yaml`（`wb_agents.py:570-599,680`）。`docs/方案-DSH接管修复.md` §2.2 已逐行取证：
   DSH 0.2 只在启动时把该文件当一次性迁移输入，写它既不生效又留垃圾文件。
   我们的 profile patch + 托管块方向是对的，**不要为了「对齐」退回去**。
2. **OpenCode**：反代项目只写 `models[mid] = {}`（`wb_agents.py:549-553`），没有 `variants`。
   `docs/方案-OpenCode接管修复.md` §2.3 取证：OpenCode 拿 `Object.keys(model.variants)` 当档位列表。
   我们的 `variants` + `limit` 是修复后的正确形态。

---

## 五、优化方案

> 编号 O1–O13。每条含：问题 → 依据 → 改法 → 验收。
> 红线不变：核心只用标准库、Python 3.9、一律原子写、只读下零写入、不自动启停外部进程。

### 5.1 P0：低成本加固（不改架构，可先做）

#### O1 探针从「只认配置文件」升级为「三重检测」

- **问题**：`_codex_detect()` = `isfile(config.toml)`，`_opencode_detect()` = `isfile(opencode.json)`。
  客户端**装了但还没生成配置**时，看板显示「未检测到」、开关置灰点不动——正是 DSH 那条 S1 的同类现象。
- **依据**：反代 `probe` = 目录 ∪ 文件 ∪ `shutil.which`。
- **改法**：
  1. 每个适配器补「安装面」判定：CLI 用 `shutil.which(name)`（纯读 PATH，无副作用，不违反 R5）；
     GUI 客户端复用已有的 exe 探测（`_dsh_exe()` 同款逻辑抽成公共 helper）。
  2. 再加一个「配置面候选」：目录存在也算（`~/.config/opencode`、`~/.dsh/profiles/<p>`）。
  3. 优先级顺序建议：**配置面 → 安装面**；配置面在则照旧按配置路径接管，只有配置面缺失时才谈「装了未初始化」。
- **验收**：新增单测——只给安装目录/只给 PATH 假 exe、不给配置文件时 `detected=True`；
  两个条件都没有时仍然 `False`；`--readonly` 下探针不产生任何写入。

#### O5 大文件与编码护栏

- **问题**：我们的适配器把配置文件整份读进内存再改写，没有大小上限，也没有显式的 UTF-8 校验路径。
- **依据**：反代 `MAX_CONFIG_BYTES = 8MB` + 非 UTF-8 直接报错不写。
- **改法**：在 `wbrelay/ides/base.py` 加 `_read_ide_text(path)`：
  - `os.path.getsize()` 超限 → 抛/返回失败，**一个字节都不写**；
  - `UnicodeDecodeError` → 同上；日志写清原因（不含文件内容）。
- **验收**：构造 9MB 假配置 → 拒绝且零写入；构造非法 UTF-8 → 拒绝且零写入；正常文件行为不变。

#### O6 看板卡片信息补全 + 「重新检测」

- **问题**：一张卡只有名字 + 徽标 + 开关，用户看不出「写到哪个文件、上次什么时候、是不是被人改过」。
- **依据**：反代卡片有 描述 / 配置路径 / 上次配置时间 / 文件数 / 外部改动警示。
- **改法**（**就地扩展 `#ides-strip`，不新增页面**，保持 `docs/方案-设置页布局.md` /
  `docs/方案-模型配置页布局.md` 已定稿的信息架构）：
  1. `/api/ides` 每项增加：`config_paths[]`、`applied_at`、`external_change`（字段名沿用我们自己的风格；
     `api_state.py:_api_ides` 是唯一出口）。
  2. 卡片加一行小字：配置路径（mono、超出省略）+ 最近接管时间；`external_change=true` 时加一个警示徽标。
  3. 卡片组右上加「重新检测」（现在只有进页面时 `loadIdes()` 一次，装完 IDE 得刷新整页）。
- **验收**：`tests/check_dashboard_js.py` 通过；手工核对：装了没接管 / 已接管 / 接管后被手改 三种状态显示正确。

#### O7 「其他客户端」手动接入表

- **问题**：没适配的客户端（Cursor / Cline / Cherry Studio / 各类 SDK）现在只能去 README 里翻地址。
- **依据**：反代页脚那张表（`dashboard.html:3376-3396`）。
- **改法**：在 IDE 卡片组下面加一小块，展示 `http://127.0.0.1:<当前端口>/v1` + 复制按钮 + 一句「鉴权 Key 见设置页」。
  - 端口取运行时真实端口（`state._SERVER_PORT`），不是写死 8789。
  - **文案走 `docs/方案-文案与术语统一.md`**：可见文案不用「反代」二字。
  - Anthropic Messages 那一行**等 O11 落地再加**，现在加等于指一个不存在的入口。
- **验收**：JS 契约测试；只读模式下复制按钮照常可用（纯前端）。

#### O8（顺带）Codex 注入面的段定位收敛

- **问题**：`_replace_toml_string(text, "base_url", ...)` 的正则是
  `^(base_url\s*=\s*)"[^"]*"`（`MULTILINE`、`count=1`），命中的是**全文件第一个** `base_url`，与目标 provider 段没有绑定；
  多 provider 的配置里可能改到别人的段。
- **改法**：改成「先定位 `[model_providers.<name>]` 段、在段范围内替换」；没有该段时按 `_ensure_codex_provider_segment()` 补段。
- **验收**：单测——两段 provider 的假 config.toml，注入后只有目标段变化、另一段逐字节不变。

### 5.2 P1：结构重排（等 DSH 修复合并后再动）

#### O2 适配器接口改成 `build()` 纯函数 + 聚合层两阶段提交

- **问题**：每个适配器自己「读 → 改 → 写」，`--readonly` 判断、日志、原子写各自复制一份；
  多文件（DSH = patch + credentials）没有跨文件回滚。
- **依据**：反代 `apply(paths, ctx) -> {path: content}` + `integrate()` 统一提交（`wb_agents.py:769-863`）。
- **改法**：
  1. 适配器签名改成 `build(ctx) -> {path: content}`（`ctx` = `{port, base_url, api_key, models}`），**不写盘**。
  2. 聚合层负责：调用 `build()` → 按 §O3 的策略备份 → 逐个 `atomicio.write_text()` →
     任一失败按备份回滚 → 落 `ide_backups/<id>.json`。
  3. **保留**现有只读语义：`READONLY` / `NO_IDE_INJECT` 在聚合层**与** `atomicio` 原语两层拦截；
     `build()` 在只读下直接不产出。
  4. `sync_catalog()` 同样改成「算出新内容 → 内容不变则不写」（DSH 现在已经这么做，向上提升为通用约定）。
- **收益**：多文件原子性；新增一个客户端只需要写一个纯函数；测试可以直接断言产出的文本。
- **风险**：接口一动，`dsh.py` 正在被另一会话改 → **必须等它落地再动**，否则冲突。
- **验收**：三个适配器各自「注入 → 还原 = 字节级回到注入前」；`tests/test_readonly_zero_write.py` 全绿。

#### O3 备份分级 + 还原完整性

- **问题**：`_codex_restore()` 只回写 `base_url` 与 `experimental_bearer_token` 两个字段，
  而 `_codex_inject()` 改了 5 处（`model_provider` 名、`[model_providers.<name>]` 段名与段内 `name`、
  `base_url`、`experimental_bearer_token`、新增顶层 `model_catalog_json`）。
  结果：关掉接管开关后，**`model_provider` / provider 段 / `model_catalog_json` 三处不会回退**，
  「已还原」名不副实。
- **依据**：反代是原件逐字节回写（`wb_agents.py:866-902`）。
- **改法**：**不能一刀切字节级**——DSH 刻意不整份复制 `.credentials.yaml`（里面是 DSH 自己的登录凭据）。
  所以改成「**按文件声明备份策略**」：

  | 策略 | 适用文件 | 备份内容 | 还原方式 |
  |---|---|---|---|
  | `full` | `config.toml`、`opencode.json`、`cordis.patch.yml` | 原件字节 | 逐字节回写；原来不存在则删除并清理空父目录 |
  | `minimal` | `~/.dsh/.credentials.yaml` | 只存 `refs.<KEY>` 的旧值 | 按键还原/删除（维持现状） |

- **验收**：每个 IDE 一条「注入 → 还原 → `sha256(文件)` 与注入前一致」的用例；
  DSH 的 `.credentials.yaml` 仍然**不会**被整份复制进 `ide_backups/`（加断言钉住）。

#### O4 外部改动感知

- **问题**：接管期间用户自己改了配置，我们既不知道、还原时也不提示，直接覆盖。
  （现在只有 Codex 还原时有一个「base_url 还是不是我们的」守卫。）
- **依据**：反代记账 sha256 → `external_change`（`CONTRIBUTING-智能体配置.md` §6.4）。
- **改法**：`ide_backups/<id>.json` 里对每个文件记 `sha256`（我们**写进去的**内容的哈希，不是原件的）；
  `/api/ides` 重新哈希现文件比对，不一致 → `external_change=true`；
  看板标警示（O6），还原时在 toast 里说明「已还原到接管前，接管期间的改动会丢失」。
- **验收**：单测——注入 → 手工改文件 → `external_change=true`；改回原样 → `false`。

> **P0/P1 的排期约束**：`wbrelay/ides/dsh.py`、`tests/test_dsh_inject.py`、`docs/方案-DSH接管修复.md`
> 正处于另一个会话的在改状态（`git status` 可见）。O2/O3/O4 都要碰这三个文件的邻域，
> **必须等 DSH 修复合并进 master 之后再开工**；O1/O5/O6/O7/O8 与它基本不重叠，可先做。
### 5.3 P2：新增客户端（前置条件是先取证）

- **O12 Crush 适配器**：见 §6.1。
- **O11 网关补 Anthropic Messages（`/v1/messages`）**：见 §6.2。**这是独立立项，不是 IDE 接管的小改动。**
- **O13 Claude Code 适配器**：等 O11 落地后再做（那时适配器本身很小，写 `~/.claude/settings.json` 的 `env` 三段即可）。

### 5.4 P3：待确认（先不动，列出取舍）

- **O9 密钥引用化**：现在把 Key 明文写进客户端配置，和反代项目一样。
  DSH 已经用 `apiKeyEnv` + `.credentials.yaml` refs（干净）；OpenCode 支持 `{env:VAR}` 插值的话也能不落明文。
  鉴权默认全关，实际风险有限 → **优先级低，等确认语法再定**。
- **O10 生命周期语义复审**：我们要不要保留「退出即还原」？见 §九 未决问题 1。

---

## 六、那两个还没接的 IDE 能不能加

先把「另外 2 个」对齐清楚：反代项目一共 5 个客户端，我们是 3 个（Codex / OpenCode / DSH），
**差的就是 `claude-code`（Claude Code）与 `crush`（Crush）**。

### 6.1 Crush：**可以加，成本低**

**能不能**：能。它说 OpenAI 兼容协议，走 `/v1/chat/completions`，**我们已经有这个入口**（`server.py:21`、`api_v1.py`）。

**写成什么样**（反代项目 `wb_agents.py:602-624` 的形态，**待实机核对**）：

```json
{
  "providers": {
    "wbrelay": {
      "name": "WBRelay",
      "type": "openai",
      "base_url": "http://127.0.0.1:<port>/v1",
      "api_key": "<网关 Key>",
      "models": [{"id": "<model>", "name": "<model>", "context_window": 131072, "default_max_tokens": 8192}]
    }
  }
}
```

**必须先取证的两件事**：

1. **路径**。反代项目写死 `~/.config/crush/crush.json`（POSIX 习惯）。
   Crush 这类 Go 程序通常走 `os.UserConfigDir()`——**Windows 上是 `%APPDATA%`，不是 `~/.config`**。
   本机已核：`%APPDATA%\crush\` 与 `~/.config\crush\` **都不存在**，`crush` 也不在 PATH。
   → 我们要用「显式环境变量（如 `XDG_CONFIG_HOME`）优先 → 平台默认」的写法，和 DSH 适配器现在处理 `DSH_HOME` 的方式一致，
   **不要照抄反代那条路径**。
2. **字段**。`type: "openai"` / `base_url` / `api_key` / `models[].context_window` / `models[].default_max_tokens`
   都要以官方文档或实机为准；我们的模型清单能提供 `context_window` / `max_output_tokens`（对齐时注意字段改名）。

**建议**：作为「多客户端扩展」的**第一个**案例来做——正好验证 §5.2 的接口改造确实把新增客户端的成本降下来了。
预估：适配器 ~120 行、测试 ~150 行、看板不需要改（注册表驱动）。

### 6.2 Claude Code：**不能只加适配器，先得给网关补一个协议入口**

**为什么不能只加适配器**：

- Claude Code 只会说 Anthropic Messages：`ANTHROPIC_BASE_URL` 指向**根路径**，请求打到 `${BASE}/v1/messages`；
  鉴权用 `x-api-key` 或 `Authorization: Bearer`。
- 我们的网关对外只有三个入口：`/v1/models`、`/v1/responses`、`/v1/chat/completions`
  （`wbrelay/web/server.py:20-22`、`api_v1.py:2`，README「协议矩阵」也只有两个客户端列）。
  **没有 `/v1/messages`。**
- 所以现在就算把 Claude Code 卡片加上、开关点亮，注入进去的地址也是 404——**等于给用户一个点得动但用不了的开关**。

**要做到什么程度**（参照反代项目 `wb_proxy.py:8262-8919`，约 660 行 + 两个测试文件 11KB/18KB）：

| 面 | 内容 |
|---|---|
| 请求 | `system`（字符串 / 文本块数组）、`text` / `image` / `document` / `tool_use` / `tool_result` 内容块、`tools` + `tool_choice`、`stop_sequences`、`model` 名校验 |
| 出站 | 复用现有 `_dispatch(path, payload)` 选路：内部转成 chat 或 responses，**核心语义不变**（失败必切换 / 保存即清冷却 / 全败必报错，用量 `realm=supplier:<id>` / `supplier:all_failed`） |
| 响应 | 非流式信封 + **原生 SSE 事件序列**（`message_start` → `content_block_start` / `content_block_delta` → `content_block_stop` → `message_delta` → `message_stop`） |
| 鉴权 | `x-api-key` 与 `Authorization: Bearer` 两种都认；错误一律 Anthropic 错误信封 |
| 可选 | `/v1/messages/count_tokens` → 返回**估算值**（说清楚不是官方分词器的精确值），不要伪造精确性 |

**边界要提前写在文档里**（照抄反代项目踩过的坑，避免过度承诺）：
服务端工具（`web_search` 等）上游没有执行器 → 不伪造调用；
`thinking` / `redacted_thinking` 块不回放（上游不提供可验证签名）；
`top_k` / `cache_control` / `context_management` / `betas` 忽略。

**建议路径**：

1. 单独立项 `docs/方案-协议-Anthropic Messages.md`（含逐行取证与 SSE 事件契约），**不要并进 IDE 接管重构**；
2. 落地后：先在 O7 的「其他客户端」表里放出 Anthropic Base URL（这一行本身就是给 Cursor / Anthropic SDK 用户的），
3. 再加 `claude-code` 适配器（届时写 `~/.claude/settings.json` 的 `env.ANTHROPIC_BASE_URL`（根路径，**不带 `/v1`**）
   + `ANTHROPIC_AUTH_TOKEN` + 可选的 `ANTHROPIC_MODEL` 系列变量）。

**结论**：Crush 排前、Claude Code 排后，两者都**不能**照抄反代项目的实现（路径/字段/协议规模都要自己取证与实现）。

---

## 七、建议顺序与验收

| 序 | 内容 | 前置 | 验收 |
|---|---|---|---|
| 1 | O1 探针 + O5 护栏 + O8 Codex 段定位 | 无 | 对应单测新增并通过；只读零写入全绿 |
| 2 | O6 卡片信息 + O7 手动接入表 | 无 | `tests/check_dashboard_js.py`；手工三态核对 |
| 3 | **等 DSH 修复合并** | 另一会话 | `git status` 干净、全量测试全绿 |
| 4 | O2 接口改造 + O3 备份分级 + O4 外部改动感知 | 3 | 三个 IDE 各自「注入 → 还原 = 字节级一致」；只读零写入全绿 |
| 5 | O12 Crush（先取证） | 4（可选） | 实机：模型出现在 Crush 里、能发消息、用量 `realm=supplier:<id>`；还原逐字节 |
| 6 | O11 Anthropic Messages 立项 | 单独立项 | 见该方案自己的验收 |

**每步统一跑**（`docs/规则-隔离与数据保护.md` §六）：隔离断言（P1–P6 SHA256）、单测 + mock 集成、
`python tools/scan_sensitive.py`。

---

## 八、红线证明（本方案阶段）

| 红线 | 本轮情况 |
|---|---|
| R6 上游项目只读 | 本轮只做 `Get-ChildItem` / `Get-Content` / `Select-String` 三类只读访问，**未写入、未创建文件、未注册、未启停进程与计划任务** |
| R1–R5 / R7 | 未触碰 `%APPDATA%\WBRelay-legacy-*`、未写 `~/.codex`、未向原项目目录输出、未操作任何外部进程 |
| P1–P6 | 哈希未重采（本轮不做阶段验收）；除只读列举外未访问 |
| 代码 | **一行未改**。工作树里 `wbrelay/ides/dsh.py`（M）、`tests/test_dsh_inject.py`、`docs/方案-DSH接管修复.md` 是**另一个会话**的在改文件，本轮未触碰 |
| 文档 | 本文件只含占位符（`<反代项目目录>` / `<原项目目录>`），不含真实路径、域名、Key |

**待办（不在本轮）**：`AGENTS.md` §八 索引登记（本文件尚未登记）。另一个会话已把
`方案-OpenCode接管修复.md` / `方案-DSH接管修复.md` 两条登记进索引；本轮**刻意不动 `AGENTS.md`**，
避免与在飞改动抢同一个文件——登记建议放在 DSH 修复合并之后单独提交。

---

## 九、未决问题（需确认后再动手）

1. **退出即还原要不要保留**（O10）：现在网关一退出，客户端配置就回原样。
   好处是「停用即无残留」；代价是「临时停网关还想让 IDE 指向 WBRelay」的场景不成立，
   而且每次 R7 值守重启都会把 `~/.codex/config.toml` 整文件重写一遍（P2 的 mtime WARN 来源之一）。
   → **建议保持现状**，只补「还原失败要在看板可见」；如果你要「退出保留」，再单独设计一个 `settings.ide_restore_on_exit`。
2. **要不要「默认模型」下拉**（本方案 O8 之外）：反代项目有；我们的立场是
   「模型配置页里启用哪个模型，就是传给客户端的哪个」，避免两处配置打架。除非确有需要，**建议不做**。
3. **O9 密钥引用化**要不要做（OpenCode 的 `{env:VAR}` 语法需先核实）。
4. **本文件的发布归属**：目前只用了占位符，理论上可以随公开发布；
   若后续要写进真实路径/目录名，必须同时加进 `tools/publish_excludes.txt`。
---

## 十、本轮实施（2026-10-14 追加需求）

> 追加需求：①确认「注入还有个要开启的条件」②把 Crush 加进来 ③IDE 卡片加一行「当前是谁在接管」且**定期轮询**
> ④其他适合的优化一并做。DSH 修复已由并行会话提交（`8425778`），本节起 O2/O3/O4 的排期约束解除。

### 10.1 注入的三层闸门（回答「还有个要开启的条件」）

注入**不是单条件**，实际是三层同时成立（`wbrelay/ides/base.py`）：

| 层 | 闸门 | 位置 | 语义 |
|---|---|---|---|
| 1 | `--readonly` / `--no-ide-inject` | `paths.READONLY` / `paths.NO_IDE_INJECT`，经 `ide_write_globally_blocked()` | **全局**禁止一切 IDE 写入（零写入） |
| 2 | `settings.ides.<id>.enabled` | `ides_settings()` / `ide_enabled()` / `ide_write_allowed()` | **每个 IDE 一个开关**（`codex` 兼容旧 `codex_auto_switch`） |
| 3 | `adp["detect"]()` 为真 | 各适配器 | **没检测到就不注入**，看板开关置灰 |

启动注入（`app.py:251` → `ides_backup_and_inject`）的循环里就是 `ide_write_allowed(id)` + `detect()` 两个前置；
看板点开关（`api_state.py:_api_ide_toggle`）额外先过 `ide_write_globally_blocked()`，开之前还要 `detect()`。
**归属判定（§10.3）读的是磁盘真值，与这三层闸门无关**——开关只表达「意图」，真值只表达「现状」。

### 10.2 接管归属判定（owner）——本轮核心

**用户提法**：判断注入的那份文件是不是自己的。
**采纳，但升级为「按入口判 owner」**——不只看「有没有我们的标记」，而是解析**磁盘上实际生效的 base_url**。
理由：标记存在 ≠ 真正生效（DSH 旧版就踩过「写在客户端不读的位置」的坑）；
而「实际生效的入口指向谁」是端到端可验证的真值，还能顺带发现「端口改了没重注入」「崩溃残留」「被别的工具顶掉」三种情况。

**四态**：

| state | 判据 | 卡片文案 |
|---|---|---|
| `wbrelay` | 入口指向 `127.0.0.1:<当前端口>/v1` | WBRelay 接管中 |
| `wbrelay_stale` | 入口指向本机回环但**端口与当前实例不同** | WBRelay 残留（端口不一致） |
| `external` | 入口指向非回环地址 / 别的 provider | 其他工具或手工接管 |
| `none` | 解析不到入口 | 未接管 |

**判据来源（每个适配器只读解析，不写盘）**：

| IDE | 解析位置 |
|---|---|
| codex | `~/.codex/config.toml`：`model_provider` → `[model_providers.<name>].base_url` |
| opencode | `~/.config/opencode/opencode.json`：`provider.wbrelay.options.baseURL` |
| dsh | `~/.dsh/profiles/<p>/cordis.patch.yml`：托管块里的 `baseURL`（`apiKeyEnv: WBRELAY_API_KEY`） |
| crush | `%LOCALAPPDATA%\crush\crush.json`：`providers.wbrelay.base_url` |

**辅助真值**：`ide_backups/<id>.json` 是否存在 + 里面记的 `sha256`（我们上次写进去的内容）→ `external_change`。
所以卡片最终是三组信息：**意图（开关）/ 现状（owner）/ 可回退性（有备份 & 是否被外部改过）**。

### 10.3 冻结的 API 契约（`GET /api/ides`，前后端共用）

```json
{
  "ides": [
    {
      "id": "codex",
      "name": "Codex",
      "detected": true,
      "enabled": true,
      "restartable": true,
      "config_paths": ["<绝对路径>"],
      "backup_exists": true,
      "applied_at": "2026-10-14 00:12:03",
      "external_change": false,
      "owner": {
        "state": "wbrelay",
        "label": "WBRelay",
        "detail": "http://127.0.0.1:8789/v1",
        "port": 8789,
        "source": "config.toml"
      }
    }
  ],
  "detected": 1
}
```

- 新增字段全部**向后兼容**：老前端忽略未知字段照常工作；新前端在字段缺失时降级显示「—」。
- `owner.detail` 只放 host/port 与该入口的短标识，**不把整份配置或 Key 塞进响应**。
- `applied_at` / `external_change` / `backup_exists` 均从 `ide_backups/<id>.json` 读，**只读**：`--readonly` 下同样返回真值（读不算写）。

### 10.4 前端行为（T2）

1. 卡片加一行「归属」：`<徽标> <owner.detail>`；徽标按 §10.3 的四态 + `external_change` 上色。
2. **5 秒轮询** `/api/ides`：只在「设置页 / 模型配置页」可见时轮询；`document.hidden` 时跳过；
   **payload 与上次逐字节相同就不重渲染**（否则会打断用户正在点的开关）。
3. 卡片组下面加「其他客户端手动接入」小块：`http://127.0.0.1:<当前端口>/v1` + 复制按钮
   （端口取运行时真值；可见文案不出现「反代」二字，遵守 `docs/方案-文案与术语统一.md`）。
4. 未检测到 / 已检测到未接管 / 已接管 / 外部接管 四态要能一眼区分。

### 10.5 本轮范围

**做**：O1 探针三重检测、O3 Codex 还原完整性、O4 外部改动感知、O5 大文件与编码护栏、O6 卡片信息、
O7 手动接入表、O8 Codex 段定位、O12 Crush 适配器。

**不做（本轮，理由写清）**：
- **O2 接口重构（`build()` 纯函数 + 两阶段提交）**：收益是架构洁癖，风险是同时动三个适配器；
  当前多文件只有 DSH 一家且已按顺序写 + 逐字节还原。**单独立项，不与本轮混做。**
- O9 密钥引用化（鉴权默认关闭，收益有限）、O10 生命周期语义（待用户确认）、
  O11 Anthropic Messages / Claude Code（独立立项，见 §6.2）。

### 10.6 分工与写集（互不重叠）

| 线程 | 写集 | 交付 |
|---|---|---|
| T1 后端 | `wbrelay/ides/**`、`wbrelay/web/api_state.py`、`tests/**`（`check_dashboard_js.py` 除外） | Crush 适配器、owner 判定、探针/护栏、Codex 还原修复与段定位、`/api/ides` 字段、单测 |
| T2 前端 | `dashboard.html`、`wbrelay/static/dashboard.html`、`tests/check_dashboard_js.py` | 归属行 + 四态徽标 + 5 秒轮询 + 手动接入表 |
| 本会话（编排） | `docs/**`、`README.md`、`CHANGELOG.md`、`AGENTS.md` | 方案/取证文档、索引登记、最终验收与冲突复核 |

**线程规则**：不执行任何写 git（commit/stash/checkout/rebase）；不运行 `tools/restart-gateway.py`；
不碰 P1–P6；开发/测试一律 `--data-dir <临时目录> --no-ide-inject --port <非 8789>`。
### 10.7 实施结果（2026-10-14）

| 项 | 结果 |
|---|---|
| O12 Crush 适配器 | `wbrelay/ides/crush.py`（新）；取证见 `docs/方案-Crush接管.md` |
| owner 四态判定 | 四个适配器各自提供只读解析（`_codex_owner` / `_opencode_owner` / `_dsh_owner` / `_crush_owner`） |
| `/api/ides` 契约 | 实现为 §10.3 冻结契约 + `backup_exists` / `applied_at` / `external_change` 三个平铺字段 |
| O1 探针 | 四个适配器补「目录 ∪ 文件 ∪ PATH」；DSH 仍以 profile 面为准 |
| O3 Codex 还原 | 由「字段级逆操作」升级为 **原文逐字节回写**（`{raw, existed}`） |
| O4 外部改动 | `ide_backups/<id>.json` 记 `applied_at` + 每次写入后各文件的 `sha256` |
| O5 护栏 | `read_ide_text()` 统一 8MB 上限 + 严格 UTF-8；`validate_ide_text()` 挡住超限写 |
| O6 / O7 看板 | 归属行 + 四态徽标 + 5 秒轮询 + 「其他客户端手动接入」块（`dashboard.html` 与其包内副本同步） |
| O8 Codex 段定位 | 换成最小 TOML 解析（`_tables` / `_segment`），只在目标 provider 段内改键；重复表/重复键直接拒绝 |
| 测试 | 新增 `tests/test_ide_takeover.py`（22 项）；`test_dsh_inject` 58 / `test_opencode_inject` 16 / `test_readonly_zero_write` 43 / `exercise_core` 55 / `test_package_layout` 23 / `test_data_path_policy` 28 / `test_routing_semantics` 33 全绿 |

#### 实施期的两处决策（与 §10 初稿不同，以本节为准）

**决策 1：`/api/ides` 的 `enabled` 不再与 `detected` 相乘。**
初稿说「现有字段语义不许变」，实施时发现旧写法 `enabled = 开关 AND detected` 会把「用户已开启、但客户端当前没被检测到」
显示成**关闭**——与 `settings.json` 里的真实意图相反。现在 `enabled` 只反映开关真值，卡片在 `detected=false` 时仍然置灰不可点，
不存在误操作面。`tests/test_ide_takeover.py` 钉住了这条语义。

**决策 2：Codex 的还原改用整份原文快照，而不是「逐字段逆操作」。**
初稿倾向逐字段逆操作（避免把用户其它 provider 的凭据复制进数据目录）。实施时评估后改成 `{raw, existed}`：

- 逐字段逆操作**做不到**「关掉开关 = 回到接管前」：我们在注入时可能*新建* `[model_providers.<name>]` 段、
  *新增* 顶层 `model_catalog_json`、*改名* `model_provider`，这些都无法靠回写旧值撤销。
- OpenCode 适配器**本来就是**整份原文快照（`{"raw": …}`），Codex 只是对齐既有做法。
- 备份落在 `%APPDATA%\WBRelay\ide_backups\`（每用户受保护目录，且**已在 `AGENTS.md` 里列为敏感不提交对象**），
  还原成功即删除。
- **DSH 的 `.credentials.yaml` 不适用这条**：那里面是 DSH 自己的**轮换登录凭据**（不是用户手写的配置），
  整份复制既扩大暴露面、又会在轮换后用旧快照覆盖，所以 DSH 仍然只记目标 `refs` 键。

两者差别一句话：**备份「用户自己写的配置」可以整份存，备份「别人签发、会轮换的凭据」只存最小面。**