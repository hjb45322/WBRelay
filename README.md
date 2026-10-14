# WBRelay

**多供应商自动路由网关**：Codex 只连一个本地口（默认 `http://127.0.0.1:8789/v1`），
网关按模型名把请求分流到多个上游供应商；供应商顺序即优先级，失败自动冷却并切下一个。

```
Codex ──► WBRelay（一个本地口） ──► 上游供应商 A（优先）
                                 ├─► 上游供应商 B（A 失败时）
                                 └─► 上游供应商 C（B 失败时）
```

> 状态：**1.1.0**，已公开发布（MIT）。架构与打包仍在演进；
> 内部资料与发布门禁见本文末尾「发布状态与内部资料」。

---

## 快速开始

```powershell
# 1) 启动（源码方式）
python relay.py

# 2) 打开看板
#    http://127.0.0.1:8789/
```

也可以装成包再运行（等价，看板资源随包分发）：

```powershell
pip install .              # 仅网关（纯标准库，不需要任何第三方依赖）
pip install ".[gui]"       # 需要独立窗口 / 系统托盘时再装 GUI 可选依赖
wbrelay                    # 等价于 python relay.py
```

首次启动会在 `%APPDATA%\WBRelay\settings.json` 生成默认配置，然后在看板「供应商」页添加供应商。

### 开发 / 测试必须带隔离参数

```powershell
python relay.py --data-dir <临时目录> --no-ide-inject --port 18789 --headless
```

| 参数 | 作用 |
|---|---|
| `--data-dir <路径>` | 指定数据目录（避免读写其他实例的数据） |
| `--no-ide-inject` | 禁止改写 Codex / OpenCode / DSH / Crush 等 IDE 配置 |
| `--readonly` | 只读模式：管理 API 拒绝写入，**不创建数据目录、不写任何文件**，不启动后台写线程 |
| `--allow-legacy-data` | 显式放行历史/归档数据目录（`WBRelay-OSS`、`WBRelay-legacy-*`，默认拒绝） |
| `--headless` | 不创建窗口与托盘（自动化与 CI 用） |
| `--port <n>` | 监听端口（默认 8789） |

对应环境变量：`WBRELAY_DATA_DIR` / `WBRELAY_NO_IDE_INJECT` / `WBRELAY_READONLY` / `WBRELAY_HEADLESS`。

隔离启动器（空白测试配置，不读取在用数据）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\run-isolated.ps1 -Headless
```

启动前会做两件事，任一不满足就退出（不自动顺延、也不结束任何进程）：

1. **数据目录路径策略**（`tools/data_path_policy.py`）：把 `--data-dir` 规范化成绝对路径并解析
   junction / symlink；命中 `%APPDATA%` 整棵树（覆盖 `WBRelay` / `WBRelay-OSS` / `WBRelay-legacy-*`）、
   仓库目录、`~\.codex` 或 `WBRELAY_FORBIDDEN_PATHS`（分号分隔，用来补原项目/上游目录）就拒绝。
2. **端口空闲检查**：拒绝 8789；指定端口被占用时退出。

---

## 数据与隐私

| 项 | 位置 |
|---|---|
| 配置 | `%APPDATA%\WBRelay\settings.json` |
| 用量 | `%APPDATA%\WBRelay\usage.jsonl` |
| 日志 | `%APPDATA%\WBRelay\wb.log` |
| 模型缓存 | `%APPDATA%\WBRelay\models_cache.json` |
| 窗口状态 | `%APPDATA%\WBRelay\window.json` |

- 所有数据都在本机，**不上传、无遥测**
- 用量记录只含模型名、供应商标识、token 计数、耗时与错误摘要，**不记录 prompt 与回复正文**
- 上游 API Key 只存在本机配置文件里，接口一律以掩码返回；日志出口统一脱敏。网关**自身**签发的本机 Key 可由「其他 IDE」卡片上的复制按钮按需取回（`GET /api/keys/current`，见 SECURITY.md）
- 上游面板口令（`wb_password`）**不落盘**（只由浏览器 `localStorage` 保管，自动登录时前端自己填）
- 对外网络请求仅两类：转发到你自己配置的上游；以及可选的 OpenRouter 模型档案同步
- 配置类写入一律**原子写**（同目录临时文件 + `os.replace`），断电不会留下半截配置
- 用量看板的「成本」列按 OpenRouter 公开价 × **`usd_cny_rate` 汇率**（默认 `7.2`，可设范围 `0.1`~`100`，设置页可改）折算成人民币 —— 这是**估算值，不是账单**：实际计费以你自己的供应商账单为准
- 日志轮转：`wb.log` 超过 2MB 时滚动为 `wb.log.1`，并顺次下移到 `wb.log.2` / `wb.log.3`，**最多保留 3 代**（更早的自动淘汰）；日志出口统一脱敏，单行截断到 800 字符

---

## 依赖

**网关核心只用 Python 标准库**（Python 3.9+）。

以下依赖**可选**，只有「源码/包运行」且需要独立窗口、系统托盘时才需要：

```
pywebview
pystray
Pillow
```

```powershell
pip install ".[gui]"     # 或 pip install -r requirements.txt
```

**缺失时自动降级为浏览器模式**：HTTP 服务与看板照常工作，网关会调用系统默认浏览器打开看板并在日志里说明缺少哪个包。

打包成 EXE 后，使用者无需安装 Python，也无需安装上述依赖。

---

## 项目结构

```
relay.py              入口 shim（保持 python relay.py / 打包命令兼容）
dashboard.html        看板（只读静态资源，唯一编辑源）
wbrelay/              网关包（标准库实现）
  paths.py            路径与运行模式（数据目录 / --readonly / --no-ide-inject）
  settings.py        配置读写（原子写）
  routing.py          路由表与冷却
  usage.py            用量记录与聚合
  catalog.py          Codex 模型清单生成
  web/                HTTP 层（server + 各 API 分组）
  ides/               Codex / OpenCode / DSH / Crush 配置接管（默认关闭）
  integrations/       外部上游服务的**只读**状态探测（不写、不改、不启停、不守护、不自启）
  static/             包内看板副本（由 tools/sync_dashboard.py 生成）
WBRelay.spec          PyInstaller 打包定义
tools/                受保护对象哈希校验 / 网关重启工具 / 隔离启动器
tests/                单元 + mock 集成 + 端到端测试
docs/                 计划与设计文档
```

### 打包

```powershell
pyinstaller WBRelay.spec
# 等价命令：pyinstaller --onefile --noconsole --name WBRelay --add-data "dashboard.html;." relay.py
```

打包定义会从 `wbrelay/__init__.py` 的 `__version__` **生成** EXE 的版本资源
（「属性 → 详细信息」里的 ProductName / FileVersion 等）——版本号仍是单一来源，不需要手改第二处。
分发 EXE 时请随附 `LICENSE` 与 `THIRD-PARTY-NOTICES.md`（EXE 内含 CPython 与 GUI 依赖）。

### 构建 wheel（发布安装版）

```powershell
python tools/sync_dashboard.py        # 同步包内看板副本（CI 会校验一致性）
python -m pip wheel . -w dist-wheel   # 产物：dist-wheel/wbrelay_gateway-<版本>-py3-none-any.whl
```

---

## 文档

**公开发布副本只包含下表的面向使用者文档**；开发过程中的内部方案/事故/审查文档不在发布副本内
（完整清单见本文末尾「发布状态与排除清单」）。

| 文档 | 内容 |
|---|---|
| `README.md` | 快速开始、隔离与安全边界 |
| `CHANGELOG.md` | 变更记录 |
| `SECURITY.md` | 威胁模型、已实现的防护与漏洞报告渠道 |
| `CONTRIBUTING.md` | 贡献约束与提交前自检 |
| `LICENSE` | MIT |

---

## 测试

```powershell
python -m py_compile relay.py            # 语法
python tests/check_dashboard_js.py       # 看板内联 JS 语法
python tests/exercise_core.py            # 接口巡检（只读模式，零写入）
python tests/test_package_layout.py      # 包发现 / 静态资源 / spec 无用户数据
python tests/smoke_headless.py 18789     # 无副作用启动冒烟（只读零写入）
python tests/test_readonly_zero_write.py # --readonly 真正零写入
python tests/test_atomic_writes.py       # 原子写与失败回滚
python tests/test_routing_semantics.py   # 核心语义 + 重试边界（进程内 mock 上游）
python tests/test_secret_redaction.py    # 机密回显与日志脱敏（假密钥）
python tests/test_hardening.py           # 边界校验 / Host-Origin 门禁 / 代理上限
python tests/test_sse_terminators.py     # SSE 终止判定（按 wire protocol 分别判定）
python tests/test_protocol_matrix.py     # 协议矩阵四象限（chat/responses × chat/responses）
python tests/test_opencode_inject.py     # OpenCode 注入形态：档位 variants / 无效字段 / 幂等
python tests/test_dsh_inject.py          # DSH 0.2 接管：profile patch + 凭据 refs / 幂等 / 逐字节还原
python tests/test_upstream_readonly.py   # R6：外部上游只读（不能启停/操作计划任务）
python tests/test_settings_autostart.py  # 保存设置不得隐式改写注册表自启项（注册表函数全 mock）
python tests/test_scan_sensitive.py      # 敏感信息扫描器回归（个人路径三种写法）
python tests/test_catalog_guard.py       # catalog 护栏
python tests/test_window_state.py        # 窗口几何记忆
python tests/test_usage_filters.py       # 用量筛选
python tests/test_supplier_label.py      # 供应商显示名解析
python tests/test_settings_concurrency.py     # 配置并发安全：深拷贝 / update_settings 无丢更新
python tests/test_usage_cache_concurrency.py  # 用量缓存并发：KPI 不虚高
python tests/test_http_error_handling.py      # HTTP handler 异常收敛（不泄漏堆栈）
python tests/test_cost_rate.py              # 成本汇率配置项 usd_cny_rate（32 断言）
python tests/test_log_rotation.py           # 日志多代轮转与并发写（46 断言）
```

端到端测试**自己拉起 mock 上游与隔离实例**，不使用 8789、不触碰任何真实外部服务：

```powershell
python tests/test_e2e.py                 # mock 18788/18789 + 隔离网关 18790（端口被占用则拒绝运行）
```

无头浏览器取证（需要 Node + Chrome，可执行文件可用环境变量指定）：

```powershell
bash tests/run_label_e2e.sh              # 自建通用 fixture + 只读预览服务 18899
```

---

## 协议矩阵（客户端 × 上游）

网关对外同时提供两个 OpenAI 兼容入口，对内按供应商的 `wire_api` 选路；四种组合都由网关适配，
客户端不需要知道上游说的是哪种协议：

| 客户端入口 | 上游 `wire_api=responses` | 上游 `wire_api=chat` |
|---|---|---|
| `POST /v1/responses`（Codex 等） | 原样透传 | 网关翻译 responses → chat |
| `POST /v1/chat/completions`（OpenCode / 各类 OpenAI 兼容客户端） | 网关翻译 chat → responses | 原样透传 |

配套语义：

- 只在两侧协议不一致时翻译；一致时按字节透传。
- **一旦开始向客户端输出，绝不切换供应商**（半截回答不会被两家内容拼起来）。
- 上游缺少终止事件（连接被截断）时，翻译方向仍补终块与终止符收尾，但用量记 `error=true`，
  截断在看板「用量」里看得见，不会伪装成正常结束。
- 供应商失败进入冷却后，`/v1/*` 的 503 会说明「供应商都在冷却」并给出最近失败原因与剩余秒数；
  `/api/state` 的供应商条目带只读字段 `cool_reason`。

`wire_api` 的取值、翻译范围与取证过程见 `docs/方案-OpenCode接管修复.md`。
> 把网关地址贴进浏览器时可以直接访问 `http://127.0.0.1:8789/v1`：它返回**模型清单 + 可用端点**
> （内容与 `/v1/models` 相同，多几个提示字段），免得看到 404 以为接口没起来。
> `/v1/models/`、`/v1/chat/completions/` 这类带尾斜杠的写法也接受（2026-10-14 起）。

---

## IDE 客户端接管（默认关闭）

开关在「模型配置」页的 IDE 条上，每个客户端一个；默认全部关闭，`--no-ide-inject` 一票否决，
`--readonly` 下连备份都不写。接管 = 把本机网关地址、模型清单与必要的凭据写进客户端**自己的**
配置文件，改前先备份原文，关掉开关即还原。

| 客户端 | 配置面 | 写什么 |
|---|---|---|
| Codex | `~/.codex/config.toml` + `~/.codex/model-catalogs/wbrelay-models.json` | provider / base_url / 模型清单 |
| OpenCode | `~/.config/opencode/opencode.json` | provider（`@ai-sdk/openai-compatible`）+ models + `variants` 档位 |
| DSH（DeepSeek Harness 0.2+） | `~/.dsh/profiles/<profile>/cordis.patch.yml` + `~/.dsh/.credentials.yaml` | `llm-pi-ai` 路由（`baseURL` / `apiKeyEnv` / `models` / `reasoningEfforts`）+ `agent-default-model` + `refs` 里的一个键 |
| Crush | `%LOCALAPPDATA%\crush\crush.json`（Unix `~/.local/share/crush/crush.json`） | `providers.wbrelay`（`type: openai-compat`）+ `models` 与 `reasoning_levels` 档位 + `models.large/small` 指向 |

DSH 细节：0.2 起**不再有** `~/.dsh/settings.yaml`（那是旧版遗留的一次性迁移输入）。WBRelay
只写自己用 `# >>> WBRelay managed block` 标记包住的那一段、`agent-default-model` 的
`provider`/`model` 两行、以及 `refs.WBRELAY_API_KEY` 一个键；不重排用户内容，也不整份复制
`.credentials.yaml`（它含 DSH 自己的登录凭据）。路由默认 `api: openai-completions`（走网关的
chat 入口，与 Codex 的 responses 入口互不影响）。取证、改动与验收见 `docs/方案-DSH接管修复.md`。
Crush 细节：官方 JSON 配置在**全局数据层**（Windows `%LOCALAPPDATA%\crush\crush.json`），新格式 `crushrc` 是 shell 风配置、
官方已把 JSON 标为 deprecated 但仍支持——本轮只接管 JSON，不执行 `crushrc`（那份文件里的 `$(...)` 会在加载时执行）。
自定义 provider 的 `type` 必须是 `openai-compat`（写 `openai` 是「经 OpenAI 代理」的语义）。取证见 `docs/方案-Crush接管.md`。

**接管归属**：每张卡片会显示「现在是谁在接管」——按各客户端**实际生效的入口地址**判定，分
`WBRelay 接管中` / `WBRelay 残留（端口不一致）` / `其他工具或手工接管` / `未接管` 四态，并标注「配置已被外部改写」。
判定只读解析配置文件，看板每隔 5 秒刷新一次（只在设置页 / 模型配置页可见时轮询）。
不是本表里的客户端可以直接手动接入：卡片组下方给出 `http://127.0.0.1:<端口>/v1` 与复制按钮。

---

## 外部上游服务的只读边界

- **网关不启不停外部服务**：面板只做状态展示与**只读呈现**（同源 iframe 代理），
  不启动、不停止、不操作它的计划任务；也没有开机自启与进程守护（上游项目只读）
- **面板可以内嵌查看**（2026-10-14 所有者确认恢复）：`/proxy-wb` 只把请求同源代理到
  `http://127.0.0.1:<上游端口>`，只转发 `Content-Type` 与 `X-Panel-Token`，路径穿越 / 外部目标 /
  超大请求体在连接前拒绝；上游服务自己的启停、开机自启、进程守护**均未恢复**
- 旧的 `/api/upstream/start` 与 `/api/upstream/stop` 已移除：请求它们只会拿到 409 + 策略说明，
  不会启动/停止任何进程（看板也不再渲染启停按钮）
- **不做**进程守护、**不登记**开机自启：外部进程只能由用户在它自己的窗口/服务里操作
- 「开机自动启动 / 进程守护 / 启停」在设置页置灰或移除并标注原因；接口收到对应请求会返回明确错误（不是静默忽略）

---

## 看板页面

| 页签 | 内容 |
|---|---|
| 供应商 | 供应商列表（拖拽排序 = 优先级）、启用/停用、测速、编辑 |
| 模型配置 | 拉取上游模型、按模型启停、档位与倍率、IDE 客户端接管状态 |
| 用量看板 | 汇总看板（请求/成功率/Token/成本）+ 每次请求明细 |
| 上游服务 | 只读状态 / 目录 / 服务端口 / **面板内嵌查看**（同源 iframe 代理，只读呈现，不启停；如需整体关掉，改 `settings.json` 的 `upstream_proxy_enabled`） |
| 设置 | 网络与路由 · 用量与统计 · 安全与访问 · 系统与集成 · 配置文件位置 |

> 「上游服务」页签只在设置页开启该管理功能时显示。

---

## 更新检测

看板「设置 → 系统与集成」里有一个 **检查更新** 按钮：**点它才会**向 GitHub 查询一次最新版本号。

| 项 | 行为 |
|---|---|
| 自动检查 | **没有** —— 无定时轮询、启动时也不联网（`update_check` 默认 `manual`） |
| 发出去的内容 | 只有请求本身：不带版本号、路径、供应商、用量或 Key（固定 User-Agent） |
| 收到后做什么 | 只读 `tag_name` 与发布页链接；**不下载、不安装、不改任何配置文件**，结果不落盘（失败时按统一规则记一行脱敏日志；只读模式下连日志也不写） |
| 失败时 | 5 秒超时；离线 / 被墙 / 任何异常都静默降级，不影响网关任何功能 |
| 关闭 | `settings.json` 里设 `"update_check": "off"`，或环境变量 `WBRELAY_NO_UPDATE_CHECK=1`（连请求都不发） |

> 这是 WBRelay **唯一**的主动出站请求。转发到供应商不算 —— 那是你让它做的事。
> 设计取舍见 `docs/方案-版本更新检测.md`。

---

## 安全

- 默认只监听 `127.0.0.1`，**默认不做鉴权**（本机单用户工具）；需要时可在设置里开启
  `require_api_key`（`/v1/*`）与 `require_admin_token`（`/api/*`）
- `/api/*`、`/v1/*` 与 `/proxy-wb` 额外做 Host（必须是回环地址 + 当前端口）与 Origin（必须同源）校验，
  降低浏览器跨站请求（CSRF）与 DNS rebinding 风险；无 Host / 无 Origin 的 Codex、curl 请求照常放行
- `/proxy-wb` **面板代理已恢复**（2026-10-14，所有者确认）：只把请求同源代理到 `http://127.0.0.1:<上游端口>`，
  只转发 `Content-Type` 与 `X-Panel-Token` 两个头（Cookie / Authorization / Referer / Origin 一律不转发），
  路径穿越 / 外部目标 / 超大请求体在连接前拒绝；上游服务自身的启停 / 开机自启 / 进程守护**仍未恢复**。
  残余风险与关闭开关见 `SECURITY.md`
- `/api/clipboard` 会读本机剪贴板文本，仅限同源页面调用，内容不写日志
- 细节与漏洞报告方式见 `SECURITY.md`
- **上游返回的内容只当数据，不当代码**：看板渲染供应商名 / 模型名 / 报错摘要时走转义或`textContent` 赋值，HTML 属性里**不会**拼进上游返回的字符串 —— 用量明细行只在属性里放一个**数字索引**，行数据放在页面内的数组里按索引取用。被污染或恶意的上游返回内容因此无法在看板里注入脚本（无 XSS 落点）；上游面板口令 `wb_password` 也**不落盘**、不进日志

---

## 重启网关（避免 Codex 失联）

网关自身就是本机 Codex 的上游（`config.toml` → `http://127.0.0.1:8789/v1`）：**在会话内重启时，会话会随网关一起中断**
（手工「托盘退出 → 再启动」期间 Codex 会整段失联）。因此 `tools/restart-gateway.py` 是**唯一**的重启入口
（`AGENTS.md` R7）：由人 / 协作方（Codex）显式运行，把「停旧进程 → 等端口释放 → 用原命令行拉起 →
轮询就绪」交给一个**脱离调用方会话的值守进程**，即使终端/会话中途断开，重启也会跑完（实测约 4.5 秒）。
禁止用手工 `taskkill` 或裸启动代替它。

```powershell
python tools/restart-gateway.py --check        # 只探测监听进程与启动方式，不动进程
python tools/restart-gateway.py                # 重启 8789 上的网关
python tools/restart-gateway.py --port 18789   # 指定端口
```

- **没有 `--force` / `--cmd` / `--move`**：不能绕过身份识别、不能执行任意命令、不做通用文件搬运
- 身份识别要求「命令行/可执行路径 + `/api/state` 语义」同时对上；隐藏 worker 只能由父进程用
  一次性交接文件（随机 token）拉起，并在结束进程前独立复核 PID / 可执行路径 / 命令行 / 启动时间 /
  端口监听 —— 识别不出就拒绝，不按端口或进程名猜
- 只逐个结束已确认归 WBRelay 的 PID（`taskkill /PID <pid>`，**不带 `/T`**）；发现任何不明后代就停止并报告
- 全程日志：`%LOCALAPPDATA%\Temp\wbrelay-restart-<时间戳>.log`
- 不写仓库内任何文件；不触碰上游服务等其它进程（不强杀非目标进程树）
- 若旧进程用相对路径启动且推断不出工作目录，会要求显式 `--cwd`

> 开发/测试一律用 `--data-dir <临时目录> --no-ide-inject --port <非 8789>`；不要用值守进程去动测试实例。

---

## 资产许可

`assets/` 下的图标是**代码生成的几何图形，无第三方素材**：

| 文件 | 来源 | 用途 |
|---|---|---|
| `wbrelay-icon.ico` | 由 `wbrelay/app.py` 的 `_make_icon()` + `_make_ico_bytes()` 用 Pillow 绘制圆角矩形渐变底、六边形与箭头/链路后手工打包成多尺寸 ICO | 打包 EXE 图标（`WBRelay.spec` 引用） |
| `wbrelay-icon-256.png` | 同一绘制逻辑导出 | README / 方案文档展示 |
| `brand-lockup.svg` / `.png` | 同一视觉规范的 SVG 矢量 + 位图导出 | 看板品牌栏 |
| `favicon.svg` | 同一视觉规范的 16×16 矢量 | 看板标签页 |
| `wbrelay-icon-compare.png` | 新旧尺寸渲染对比图 | 方案文档参考 |

图形本身（六边形 + 双向箭头 + 链路端点、`#0A1E4F`→`#1BA7E6` 渐变、`#FFFFFF` 主体）
由项目作者用生成式 AI 工具辅助设计后，以**代码参数化重绘**落地 —— 仓库里不包含任何 AI 生成的
原始位图素材，全部资产都能从 `wbrelay/app.py` 的绘制代码重新生成。

许可：以上资产与代码同许可（MIT，见 `LICENSE`）。`assets/图标皮肤方案.md` 记录了完整的色值、
尺寸矩阵与坐标规格，可据此复现或替换图标。

若你所在司法辖区对 AI 辅助产出的可版权性有不同认定，替换方式见
`assets/图标皮肤方案.md`（改 `wbrelay/app.py` 里的绘制参数重跑即可）。

---

## 平台

当前实现面向 **Windows**（使用了 `winreg`、计划任务、`SIGBREAK` 等）。
其他平台尚未支持。

---

## 发布状态与内部资料

> 2026-10-14：内部资料集中到 `_internal/`，由 `.gitignore` 覆盖（`/_internal/`）。
> **工作树里留下的就是可发布内容** —— `git add -A` 不会带出内部资料，
> 所以这个仓库本身可以直接作为公开仓库使用。发布门禁由 `tools/scan_sensitive.py`
> 与 CI 的 `release-scan` job 把守。

### 内部资料：`_internal/`

本机的接管 / 事故 / 审查 / 发布施工记录，以及两个只服务本机红线校验的工具，集中在 `_internal/`：

| 路径 | 内容 |
|---|---|
| `_internal/docs/事故记录.md` | 受保护对象校验的误报与排查过程 |
| `_internal/docs/审查-开源前最终审查.md` | 交给第三方模型的只读审查提示词 |
| `_internal/docs/方案-接管与退役旧实例.md` | 身份接管：注册表自启项、旧实例与快照原文 |
| `_internal/docs/方案-开源发布阻断修复.md` `-第二轮` `-第三轮` | 三轮发布阻断修复的施工与验证记录 |
| `_internal/tools/collect_manifest.py` `verify_untouched.py` | 受保护对象哈希基线的采集 / 校验 |

这些文件记录的是**本机环境的原文**（真实绝对路径、HKCU Run 自启项、旧实例字节数、本机日志片段），
所以它们：

- **不在版本控制里** —— `.gitignore` 覆盖 `/_internal/`，`git add -A` 不会提交，`git status` 也不显示；
- **登记在 `tools/publish_excludes.txt`** —— 扫描器照常逐条读它们，只是把命中归类为「内部资料」，
  不计入退出码 1（**仍然打印，不隐藏、不静默**）。

> 公开仓库里看不到这个目录，因此代码注释与 CHANGELOG 里提到 `docs/事故记录.md` 之类的地方
> 在公开仓库里是断链 —— 属预期情况（用户可见的界面与接口文案里不引用它们）。

### 署名

提交署名统一为集体署名 `WBRelay contributors <noreply@wbrelay.invalid>`，不公开任何个人姓名或邮箱。

> 为什么用 `.invalid`：它是 RFC 2606 保留域名，**永远无法被注册** —— 这个地址既不可能
> 指向真实邮箱，也不会被任何 GitHub 账号认领（提交因此不会挂到陌生人名下）。
> 早期用的 `noreply@users.noreply.github.com` 会被 GitHub 上真实存在的 `noreply` 账号认领，
> 已于 2026-10-14 换掉。

`tools/release_identifiers.txt` 里**没有** `approved|author` 条目：本仓库对外只保留集体署名，
扫描器因此不会把署名判为未决项。

### 私有上游标识（已处理）

私有上游 / 面板集成名称已从代码、界面与文档中移除，统一改为通用称谓「上游服务」；
配置键（`upstream_*`）、端口行为与对外接口语义均未改变。

`tools/release_identifiers.txt` 保留原词条作为**绊线**，但**只存 `sha256:<hex>` 摘要与字符长度，
不写明文**（这份清单本身会随发布一起走）；扫描器按 token 滑窗比对摘要，**比对大小写不敏感**
（摘要按 `str.lower()` 折叠生成）——名称以任意大小写再次出现，工作树扫描立即阻断。生成条目：

```powershell
python tools\scan_sensitive.py --hash <标识原文>          # vendor 条目
python tools\scan_sensitive.py --hash <邮箱> --author     # 署名条目
```

坦白说明：哈希只挡「明文与搜索引擎索引」，不是密码学保护 —— 猜得到原文的人可以自行验证。
需要更强保护就不要用仓库内清单，改用仓库外的清单：`--identifiers <仓库外路径>`。

> 2026-10-14：该标识在**旧提交**里留下的那一行，已按所有者决定通过「重写历史 + 强推」清除
> （旧 SHA 作废，`main` 与 `v1.0.0` / `v1.0.1` 指向重写后的提交）。Release 里的
> `WBRelay-1.0.1.exe` 与本地 `dist\WBRelay.exe` **逐字节一致**且不含该标识，因此未重传。

### 发布扫描（自己复核）

```powershell
python tools\scan_sensitive.py
```

| 范围 | 结果 |
|---|---|
| 工作树 | `CRED=0`；`PATH` 命中**全部**落在 `_internal/`（登记在 `tools/publish_excludes.txt`）—— 清单外的文件 0 命中。取数范围 = 扩展名白名单 **+ 无扩展名文本文件**（`.gitignore` / `LICENSE` 之类，二进制按 NUL 探测跳过）|
| Git 历史 | 逐提交 `file:line` 扫描（refs + reflog + 悬空对象），**0 命中** —— 2026-10-14 重写了历史：旧提交里的那处遗留行已从**每个提交**中抹掉，旧 SHA 全部作废（做法与验证见 CHANGELOG「未发布」一节）|
| 署名 | 集体署名；无「未登记为 approved」的邮箱 |
| 退出码 | `0`=可发布信号齐全；`1`=有命中 / 未批准标识 / 未确认署名 / 残留旧对象；`2`=清单缺失或不可用（fail closed） |

`tools/release_identifiers.txt` 是扫描器的显式输入：清单缺失 / 为空 / 状态非法 / 写了明文一律
fail closed（退出码 2）；命中只输出类型、路径、行号、计数，**不隐藏、不静默**。

> `tools/make_publish_copy.py` 保留作「发布内容预览」：它按 `tools/publish_excludes.txt` 把 git
> 跟踪的文件导出一份**不含 `.git`** 的副本（默认 dry-run），用于在任何时候确认「将要公开哪些文件」。
> 日常发版不再需要它 —— 直接在本仓库 `git add -A` / `commit` / `push` 即可。

### 发布前需所有者确认

- ~~提交作者姓名/邮箱是否以本人身份公开~~ —— **已决定采用集体署名**。
- 原项目 / 上游项目的真实路径是否注入 `WBRELAY_FORBIDDEN_PATHS`（仓库里不写敏感路径）。
- ~~`assets/` 商标/图标资产来源确认~~ —— **已确认**：AI 辅助设计 + 代码参数化重绘，无第三方素材。

---

## 许可

MIT，见 `LICENSE`。

打包后的 `WBRelay.exe` 是 PyInstaller 单文件包，**还内含** CPython、PyInstaller、Pillow、
pywebview 与 pystray（LGPLv3）——各自的许可、版权与替代方案见 `THIRD-PARTY-NOTICES.md`。
从源码安装（`pip install .` / `pip install ".[gui]"`）时这些依赖由使用者自行安装，按各自的许可使用。
