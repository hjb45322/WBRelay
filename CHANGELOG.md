# 更新记录

本文件记录面向使用者的变更。

## 未发布 — 值守工具支持冷启动 + `start.bat` 修复

> 只改开发工具与启动脚本，**不影响网关产品代码**，EXE 无需重打包。

### 新增

- **`python tools/restart-gateway.py --start`（冷启动）**：端口上没有网关时从零拉起。
  用于「升级换 EXE 后启动」——入口**写死**为 `dist\WBRelay.exe`（优先）或 `relay.py`，
  仍然**没有 `--cmd`**；端口已有监听则拒绝并提示改用普通重启；与重启模式共用
  「一次性交接文件 + 随机 token + 脱离会话的 worker + `/api/state` 就绪轮询」全套机制，
  worker 侧还会再查一次端口（竞态保护）。
  背景：此前「工具只做重启」+「禁止托盘退出后手工启动」两条叠加，导致升级 EXE **没有合规入口**。

### 修复

- **`start.bat` 指向不存在的文件（双击无效）**：它写的是仓库根目录的 `WBRelay.exe`，而打包产物
  一直在 `dist\`。现改为 `dist\WBRelay.exe`，并在文件缺失时提示「先打包 / 改用源码方式」，
  不再静默失败。

### 验证

`tests/test_restart_gateway.py` 新增 G11–G16（冷启动拒绝 / 计划 / worker 不结束进程 /
竞态 / 静态审计 / start.bat 回归），**64 通过 / 0 失败**；另有真机 `--check` 干跑（见
`docs/方案-网关冷启动.md` 第六节）。

## 1.1.0 — 2026-10-14 版本更新检测（默认手动、零自动外联）

### 新增

- **看板「设置 → 系统与集成」新增「检查更新」**：点一下查询 GitHub 上的最新发布版本，
  有新版时显示版本号与发布页链接。**默认不自动联网**（无定时轮询、启动时不请求），
  只发一个 GET、5 秒超时、64 KiB 响应上限，不传任何本机信息，不下载、不安装、不改文件，
  结果不落盘；失败（离线 / 超时 / 被墙）静默降级，不影响网关任何功能。
  可用 `settings.json` 的 `update_check = "off"` 或环境变量 `WBRELAY_NO_UPDATE_CHECK=1` 关闭。
  新增 `GET /api/update`（只读，`--readonly` 下同样可用）。
  设计取舍与验收见 `docs/方案-版本更新检测.md`。

- `wbrelay/update.py`：只用标准库（复用 `net._urlopen` 的系统代理处理）；
  版本比较按数字段（`v1.2.3` / `1.2.3-rc1` 都识别，预发布后缀按忽略处理 —— 刻意的简化）。

### 变更

- `tools/scan_sensitive.py`：HOST 白名单加入 `api.github.com` —— 更新检测的只读端点由产品代码
  写全 URL，属公开 API（与已在白名单里的 `github.com` 同源），不是泄漏。

## 1.0.2 — 2026-10-14 EXE 版本资源 + 第三方许可声明

### 变更

- **`WBRelay.exe` 补上版本资源（体验）**：此前它的「属性 → 详细信息」全为空
  （`CompanyName` / `ProductName` / `FileVersion` / `FileDescription` 一个都没有），
  加上没有数字签名，Windows 便把它与「来路不明的程序」归为一类。修法：`WBRelay.spec`
  在打包时从 `wbrelay/__init__.py` 的 `__version__` **生成**版本资源并交给 `EXE(version=…)`
  —— 版本号保持单一来源，不需要在两处手改。**未做**：数字签名（需要 CA 证书，另议）。

- **新增 `THIRD-PARTY-NOTICES.md`（合规）**：`WBRelay.exe` 内含 CPython（PSF）、
  PyInstaller（GPLv2+ 带例外）、Pillow（MIT-CMU）、pywebview（BSD-3）、**pystray（LGPLv3）**。
  此前仓库只有一份 MIT `LICENSE`、没有任何第三方声明。现补一份声明文件，并写明
  「源码运行不涉及该文件」「需要更严格合规可打无 GUI 版本」。

- **Release 附件约定**：发布时随附 `WBRelay-<版本>.exe` + `LICENSE` + `THIRD-PARTY-NOTICES.md`。

## 未发布 — 发布门禁加固（无扩展名文本 + 标识大小写）

### 修复

- **发布扫描漏扫无扩展名文件（中）**：`tools/scan_sensitive.py` 的工作树取数按扩展名白名单过滤，
  而 `os.path.splitext(".gitignore")` 返回的是空扩展名（前导点不算分隔符）—— `.gitignore` /
  `.gitattributes` / `LICENSE` / `Dockerfile` 这类文件**整份不扫**：真凭据、真实域名、个人路径
  写进它们都不会被报出来。修法：新增 `is_scanned_text()`（白名单 **或** 无扩展名）与
  `_read_text_or_none()`（前 8 KiB 做 NUL 探测 → 二进制跳过），`scan_tree()` 统一走两者并接受
  `root` 参数（测试用临时目录，不往仓库里写探针文件）。历史扫描本来就没有这个洞
  （`scan_commit()` 走 `git grep -I`，覆盖全部文本文件）。验证：`tests/test_scan_sensitive.py`
  新增 C11 —— 临时目录里 `.gitignore` / `Dockerfile` 中的假凭据命中，无扩展名二进制 / `.png` /
  `*.egg-info` 不报。

- **供应商标识绊线大小写敏感（中）**：清单摘要按原文大小写生成、比对也用原文窗口，于是驼峰写法
  的条目挡不住全小写 / 全大写变体 —— 工作树里一处全小写的遗留行正好躲过门禁（扫描 exit 0）。
  修法：新增 `vendor_digest()`（`str.lower()` 折叠），`hash_entry()` / `match_identifiers()`
  （含 `--vendor` 明文条目）统一走它；`tools/release_identifiers.txt` 里两条绊线摘要按新规则重算
  （**不重算 = 绊线静默失效**）。修后该遗留行在**历史里被命中 3 处**（3 个提交各一行）——
  这正是绊线仍然活着的证据。验证：同文件新增 C12（原文 / 全小写 / 全大写 / 更长 token 命中，
  连字符变体不误报）。

- **构建生成物目录未排除（低）**：`*.egg-info` 不在 `SKIP_DIRS` 里，补齐「无扩展名也扫」之后
  `PKG-INFO` 开始被扫，命中了 2026-10-12 那次构建留下的旧元数据。它与 `build/` `dist/` 同类：
  构建生成物、登记在 `.gitignore`、发布副本只拷 git 跟踪文件 —— 改用 `SKIP_DIR_SUFFIXES`
  （`.egg-info` / `.dist-info`）一并排除（源头 `README.md` 本身仍在扫描范围内）。
  **残留事项**：本机那份陈旧 `wbrelay_gateway.egg-info/PKG-INFO` 里仍有 5 行旧命名，
  它不随发布、下次构建即刷新（本次不动它）。

### 变更

- `.gitignore`：删掉一行旧命名的本地临时目录忽略规则（仓库里没有该目录，`git grep` 全仓库
  只此一处命中）；其余规则与 CRLF 逐字节不变。

- **CI 补齐 6 个「写了但从不执行」的测试**：`test_settings_concurrency` /
  `test_usage_cache_concurrency` / `test_log_rotation` / `test_http_error_handling` /
  `test_dsh_inject` / `test_ide_takeover` 此前只列在 `CONTRIBUTING.md` 的**手工自检清单**里，
  `ci.yml` 没有对应步骤 —— 它们对应的修复（并发丢更新 / 日志轮转 / HTTP 异常收敛 /
  DSH 与 IDE 接管）一旦回归，CI 不会变红。现接入 `windows` job：本地实测 6 个全绿
  （58 / 31 / 21 / 26 条断言 + 2 个 unittest 脚本），新增耗时约 22s。
  该改动**只动 CI 配置，未改动任何产品代码**。
- **历史重写 + 强推（2026-10-14，所有者决定）**：那一行原本在**每个**旧提交里，且已随
  v1.0.0 / v1.0.1 公开过。做法：`git filter-branch --tree-filter`（把该行从每个提交的
  `.gitignore` 中删掉）+ `--tag-name-filter cat`（重建两个标签，tagger 与说明原样保留），
  随后删除 `refs/original/*`、`git reflog expire --expire=now --expire-unreachable=now --all`、
  `git gc --prune=now`，最后强推 `main` 与两个标签。**旧 SHA 作废**（提交 `4d69136` /
  `7624338` / `5bb0009`，标签对象 `0a05540` / `0c5f9bb`），提交信息、作者与日期均保留。
  验证：本地与**远端全新克隆**各跑一次发布扫描 —— 工作树 0 命中、历史 0 命中、
  `git fsck --unreachable --no-reflogs` 无残留对象。

  附带确认：Release 资源 `WBRelay-1.0.1.exe`（17,504,095 B）与本地 `dist\WBRelay.exe`
  **逐字节一致**（`sha256:627b09be…`），且二进制里不含该标识 —— 无需重传资源；
  两条 Release 仍挂在同名标签上，未受影响。

  > force push 的通用限制（如实说明）：它只让旧对象**失去引用**，GitHub 侧仍需其自身回收
  > 才不可按 SHA 直接访问 —— 这与所有重写历史的项目相同。本仓库内已无任何 ref、标签或
  > Release 指向旧对象。

## 1.0.1 — 2026-10-14 用量看板性能回归修复

### 修复

- **用量看板在「本周 / 本月 / 全部」与按供应商、模型筛选下要等 5~8 秒（性能回归）**：
  1.0.0 把汇率改成配置项 `usd_cny_rate` 时，`_cost_per_request()` 结尾的
  `round(cost_usd * 7.2, 4)` 换成了 `round(cost_usd * usd_cny_rate(), 4)` —— 而它处在
  **逐行的聚合循环**里，`usd_cny_rate()` 每次都要 `load_settings()` → **深拷贝整份
  `settings.json`**：3.4 万行就是 3.4 万次深拷贝，实测占该循环耗时的 96%。
  1.0.0 的 `_cost_per_request` docstring 其实已经写了「rate 为本次聚合统一使用的汇率
  （同上，避免每行重复读配置）」，但函数签名里没有 `rate` 参数 —— 属漏做。
  修法：`_cost_per_request(r, profs=None, rate=None)` 新增 `rate` 形参；
  `usage_summary()` / `usage_recent()` 在循环外取一次汇率并传入。

  实测（同一份 3.4 万行数据、同一解释器、只读隔离目录）：

  | 操作 | 1.0.0 | 1.0.1 |
  |---|---|---|
  | `usage_summary(全部)` | 5.80s | **0.31s** |
  | `/api/usage?range=all` | 5.68s | **0.22s** |
  | `range=all&supplier=<占比最大的供应商>` | 5.70s | **0.24s** |
  | `range=today` | 0.37s | **0.03s** |

  回归测试：`tests/test_cost_rate.py` 新增 R9 —— 把 `wbrelay.usage.load_settings` 换成
  计数器，断言 N 行聚合对配置的读取次数是**常数级**（修复前 = 每行一次）；该脚本同时
  纳入 CI（此前只在 `CONTRIBUTING.md` 的自检清单里，CI 没跑，所以这次回归没被拦住）。

- **自动刷新与筛选互相打架（前端）**：5 秒定时刷新不等上一次返回，慢响应会越堆越多；
  而且旧的刷新晚于「筛选 / 切范围」的请求返回时，会用**未筛选**的数据覆盖新结果，
  并经 `renderRecent()` 把明细页码改回旧值（数字与表格来回跳）。
  修法：自动刷新改成「上一轮没回来就不发新的」（在途计数），`loadUsage()` 与明细翻页
  共用请求序号，**只有最新一次的结果允许渲染**。

- **KPI「成功率」分母重复计失败**：`kpi.requests` 本身已含失败行，前端又加了一次
  `errors` 当分母。改为 `(requests - errors) / requests`，与「按模型 / 按供应商」
  表格里 `requests - errors` 的口径统一。

### 变更

- 版本号 `1.0.0` → `1.0.1`（`wbrelay/__init__.py`、`pyproject.toml`、README 状态行）
- 方案文档：`docs/方案-用量看板性能.md`
- CI：单元与护栏测试步骤新增 `python tests/test_cost_rate.py`

## 1.0.0 — 2026-10-14 首次公开发布

从私人定制版重建为可公开发布的独立项目。**版本号由开发期的 `2.2.0` 改为 `1.0.0`** ——
`2.2.0` 是未公开发布的迭代号，公开首版按 `1.0.0` 计。

### 修复

- **CI 不再对标签触发（2026-10-14）**：首次发 `v1.0.0` 时，推标签也触发了整套 CI，
  而 `actions/checkout` 检出标签**不创建 `refs/tags/*`** —— `tools/scan_sensitive.py`
  于是把标注标签对象当成「refs 之外的对象」判失败（本地有标签引用，不会复现）。
  修法：workflow 的 `on.push` 加 `branches: ["**"]`，标签推送不再触发 CI；
  扫描器保持严格（悬空的标签对象仍会报，只是不在 CI 里误伤）。

- **集体署名邮箱换成不可注册的保留域（2026-10-14）**：原用
  `noreply@users.noreply.github.com`，而 GitHub 上**真有个 2011 年注册的 `noreply` 账号**，
  导致所有提交在网页上被挂到那个陌生人名下（个人身份没泄漏，但归错了人）。改用
  `noreply@wbrelay.invalid` —— RFC 2606 保留域名，永远无法注册，不可能被任何账号认领。
  `tools/make_publish_copy.py` 的 `AUTHOR_EMAIL` 与 README / 标识清单的说明同步更新。
- **非 GBK 控制台下脚本崩溃（2026-10-14，首次开源推送时暴露）**：
  `tools/*.py` 与多数 `tests/*.py` 直接 `print` 中文，在 **cp1252** 控制台（英文 Windows、
  GitHub Actions 的 Windows 运行器）上抛 `UnicodeEncodeError` 并退出码 1 —— 首次推送时
  **32 个 CI 步骤里 26 个因此全红**。修法：`tests/isolate.py`（被 23 个测试共享）与
  `tools/` 下 5 个脚本统一把 stdout / stderr 设为 `errors="replace"`（装不下的中文降级成 `?`
  而不是崩溃），CI workflow 另设 `PYTHONIOENCODING=utf-8` 让远端日志保留中文。
  验证：把这 32 个步骤在 `PYTHONIOENCODING=cp1252` 下重跑，**32/32 通过**。

### 新增

- **隔离开关**：`--data-dir` / `WBRELAY_DATA_DIR`、`--no-ide-inject`、`--readonly`、
  `--allow-legacy-data`、`--headless`
- 默认数据目录 `%APPDATA%\WBRelay`（身份接管后，见下方「身份接管」），与既有实例互不影响
- 安全阀：数据目录指向旧版目录时**默认拒绝启动**
- `tools/collect_manifest.py` / `tools/verify_untouched.py`：受保护对象的哈希基线校验
- `tests/smoke_headless.py`：无副作用启动冒烟测试（CI 可用）
- `tools/restart-gateway.py`：值守进程式网关重启（停 → 起 → 等就绪，实测约 4.5s 恢复），
  避免手工重启期间 Codex 整段失联
- 设置页「配置文件位置」：只读展示数据目录 / settings.json / usage.jsonl / model_profiles.json /
  models_cache.json / wb.log / ide_backups 的实际路径，带「已存在 / 未生成」徽标与一键复制
  （`GET /api/settings` 新增只读字段 `paths` / `path_exists`）
- **本机剪贴板接口**：`GET /api/clipboard` + `wbrelay/clipboard.py`（`ctypes` 只读 `CF_UNICODETEXT`，仅标准库；
  非 Windows / 读取失败返回 `supported=false`）。看板「粘贴」按钮优先走本接口，**不再触发浏览器「允许读取剪贴板」权限框**，
  取不到时回退浏览器剪贴板 API。只读、内容不进日志、文本截断 64K 字符
- 方案文档：`docs/方案-OpenCode接管修复.md`（OpenCode 档位字段与协议矩阵的取证、改动与验收）
- 方案文档：`docs/方案-DSH接管修复.md`（DSH 0.2 配置面取证、接管重写与验收）
- **Crush 接管**（`wbrelay/ides/crush.py`）：新增第 4 个 IDE 适配器。官方 JSON 配置在**全局数据层**
  （Windows `%LOCALAPPDATA%\crush\crush.json`、Unix `~/.local/share/crush/crush.json`，认 `CRUSH_GLOBAL_DATA` /
  `XDG_DATA_HOME`），自定义 provider 的 `type` 必须是 `openai-compat`。Crush 是终端 TUI，**不做进程管理**
  （看板不给「重启」按钮）。取证见 `docs/方案-Crush接管.md`
- **接管归属判定**（`GET /api/ides` 新增 `owner` / `config_paths` / `backup_exists` / `applied_at` / `external_change`）：
  按各客户端**实际生效的入口地址**判定四态 —— `WBRelay 接管中` / `WBRelay 残留（端口不一致）` /
  `其他工具或手工接管` / `未接管`，并标记「配置已被外部改写」（写入时记 sha256、读时比对）
- **看板 IDE 卡片重做**：新增「归属」行与四态徽标；**每 5 秒轮询**（只在设置页 / 模型配置页可见时轮询，
  页面隐藏跳过，响应无变化就不重渲染）；卡片组下方新增「其他客户端手动接入」，给出
  `http://127.0.0.1:<运行时端口>/v1` 与一键复制
- **IDE 探针三重检测**：`detect` 从「只认配置文件」扩展为「配置目录 ∪ 配置文件 ∪ PATH 可执行文件」，
  「装了但还没生成配置」不再显示未检测到
- 方案文档：`docs/方案-Crush接管.md`、`docs/方案-IDE接管对比与优化.md`
- 方案文档：`docs/方案-模型配置页布局.md`、`docs/方案-供应商卡片布局.md`、`docs/方案-供应商编辑弹窗.md`
- **界面内使用说明**：顶栏右侧「?」打开说明弹窗（五节：三步上手 / 五个页签职责 / 路由规则 / 常见操作 / 文件与排错），
  支持 Esc、点遮罩、「知道了」三种关闭方式；方案见 `docs/方案-使用说明.md`
- **窗口几何记忆**：拖拽调整过的窗口大小 / 位置 / 最大化状态写入 `%APPDATA%\WBRelay\window.json`（原子写），
  下次启动按原样打开；读盘做范围校验（损坏 / 越界 / 跑到屏幕外一律回落默认 1280×950），
  尺寸变化用 0.8s 去抖合并落盘，关到托盘与退出时立即落盘

- **`update_settings(mutator)` 持锁原子更新原语**（`wbrelay/settings.py`）：`mutator(data)` 拿到的是**锁内重读**的最新配置副本，整段「读 → 改 → 原子写」在同一把锁内完成，两个并发保存不再互相覆盖；返回 `(ok, result)`，`mutator` 抛异常则原样上抛且**尚未落盘**（配置不会被写坏）；保存失败（只读模式 / 写盘失败）返回 `(False, result)`，**不伪称成功**；内部哨兵 `_NO_SAVE` 表示「本次无需写盘」
- **`usd_cny_rate` 配置项**（默认 `7.2`，范围 `0.1`~`100`）：用量看板「成本」列原先硬编码汇率 `7.2`，现改为可在设置里调整；成本**始终是估算值**（OpenRouter 公开价 × 汇率），不是账单，前端 tooltip 里也标注了「估算」

- **并发与安全回归测试**：`tests/test_usage_cache_concurrency.py`（用量缓存并发，20 断言）、
  `tests/test_settings_concurrency.py`（配置共享 dict 与 read-modify-write 丢更新，31 断言）、
  `tests/test_http_error_handling.py`（HTTP handler 异常收敛，26 断言）—— 三个脚本都自带数据目录隔离
  自检，只写自建临时目录、不读真实 `%APPDATA%`、不占用 8789；本轮起所有测试脚本在中文 Windows 的
  **默认 GBK 控制台下也能直接跑**，不需要设 `PYTHONIOENCODING` / `PYTHONUTF8`

### 变更

- **冻结所有权测试的编码缺陷（2026-10-14）**：	ests/frozen_onefile_ownership.py 的list_processes() 用系统编码解码 PowerShell 输出，在**中文仓库路径**下进程路径变成乱码，归属比对永远不中（还会跳过它自己的清理、留下野实例）。现在 PowerShell 侧固定 [Console]::OutputEncoding = UTF8、Python 侧按 UTF-8 解码；实测 --exe dist\WBRelay.exe 8/8 PASS（父子进程都认得出、退出后端口释放）
- **DSH 接管重写（2026-10-14）**：DeepSeek Harness 0.2 起，配置面是 profile 用户 patch 层
  （`~/.dsh/profiles/<profile>/cordis.patch.yml`）+ 凭据 `refs:`，不再是 `~/.dsh/settings.yaml`
  （旧版遗留，只在启动时被一次性迁移）。旧实现因此**恒判「未检测到」**，注入的 `base_url` / `api_key`
  也早已不是 DSH 的字段。现在：检测按 profile 判定；注入写 `llm-pi-ai` 路由
  （`baseURL` / `apiKeyEnv` / `models` / `reasoningEfforts`）与 `agent-default-model`；
  凭据只加 `refs.WBRELAY_API_KEY` 一个键（不再整份复制 `.credentials.yaml`）；重注入幂等、
  不覆盖用户在 DSH 里换过的默认模型、切换 provider 时移除旧模型的 `reasoningEffort` 行；
  关掉开关逐字节还原。测试：`tests/test_dsh_inject.py`（56 断言）- **发布标识清单只存哈希（2026-10-13 发布门禁加固）**：`tools/release_identifiers.txt` 不再写明文标识，
  改为 `sha256:<hex>` 摘要 + 字符长度（清单本身会随发布一起走）；新增
  `python tools/scan_sensitive.py --hash <原文>` 生成条目，扫描器改用「token 滑窗哈希比对」命中
- **历史扫描覆盖 reflog 与悬空对象（2026-10-13）**：除 `git rev-list --all` 外还扫 `--reflog --all`，
  并用 `git fsck --unreachable --no-reflogs` 单列「refs 之外的对象」—— 被 amend 顶掉的提交
  不在 `git log --all` 里，但仍能被 `git show` 原样取出，有残留就阻断发布
- **署名门禁（2026-10-13）**：作者邮箱未在清单里登记为 `approved` 的提交一律阻断；愿意公开本人署名
  就用 `--hash <邮箱> --author` 生成条目，不愿公开则在发布副本里用集体署名重建历史
- **资产来源说明（2026-10-13）**：`assets/` 图标由生成式 AI 辅助设计后以**代码参数化重绘**落地
  （`wbrelay/app.py` 的 `_make_icon()` / `_make_ico_bytes()`，Pillow 绘制 + 手工构造 ICO），
  仓库内不含任何 AI 生成的原始位图素材，全部图标都能从代码重新生成；许可随代码（MIT）。
  README「资产许可」与 `assets/图标皮肤方案.md` 记录了完整规格（色值 / 尺寸矩阵 / 坐标）
- **署名改为集体署名（2026-10-13）**：全部分支的作者 / 提交者改写为
  `WBRelay contributors <noreply@users.noreply.github.com>`（2026-10-14 见上，已换为
  `@wbrelay.invalid`），`refs/original/` 备份 ref、reflog
  与旧提交对象已随 `gc --prune=now` 删除 —— 任何个人身份都不随仓库或发布副本公开；
  标识清单里没有 `approved|author` 条目（私有仓库会提示「未登记为 approved 的署名」，
  那是应然状态，发布副本扫描 exit 0）
- **PATH 规则放宽为任意盘符绝对路径（2026-10-13）**：不再只认用户目录；系统目录与测试夹具走
  `ALLOWED_PATH_PREFIXES` 显式白名单（每条都写了理由），测试脚本里的本机 Python 安装路径已清掉
- **发布排除清单收敛为唯一来源（2026-10-13）**：新增 `tools/publish_excludes.txt`（README 只做索引），
  新增 `tools/make_publish_copy.py`（默认 dry-run；`--write` 才落盘，`--init-history` 用集体署名重建历史）
- **外部面板代理整体停用（2026-10-12，第三轮发布修复）**：`/proxy-wb` 无法在不访问真实上游的前提下
  证明「任意 GET 只读」，也不能保证请求头/请求体不被转发，因此按 R6 停用整个入口：任何方法一律 403 +
  策略说明，不建立上游连接、不转发 Cookie / Authorization / X-Panel-Token / 请求体；看板改为状态 + 策略说明，
  不再渲染 iframe
- **上游服务面板查看能力恢复（2026-10-14，所有者确认）**：`/proxy-wb` 从「一律 403」改回
  同源代理到 `http://127.0.0.1:<上游端口>`，看板重新渲染 iframe（高度自适应 + `loading="lazy"`）
  并恢复「自动登录」（口令只从浏览器 `localStorage` 取，**服务端永不落盘**）。比历史版更严：
  目标只允许本机回环且不接受外部目标、不跟随重定向；只转发 `Content-Type` 与 `X-Panel-Token`
  （Cookie / Authorization / Referer / Origin 一律不转发，也不再启用 cookie jar）；
  路径穿越 / 外部绝对 URL / 编码绕过 / 反斜杠 / 非法 query → 400；请求体 1MB、响应体 8MB 上限
  （先拒后读，不整体读进内存）；15s 超时 + 句柄一定关闭；失败一律脱敏后的 502。
  设置项 `upstream_proxy_enabled`（默认 true）可整体关掉。**启停 / 开机自启 / 进程守护均未恢复**
  （`/api/upstream/start|stop` 仍 409，`set_autostart_reg()` 仍报错，无守护线程）。
  方案与残余风险见 `docs/方案-上游服务面板恢复.md` 与 `SECURITY.md` §5
- **`/v1/*` 补回环 Host / 同源 Origin 门禁（第三轮）**：默认无鉴权不变；无 Host / 无 Origin 的
  Codex、curl 请求照常放行，恶意 Host 或跨站 Origin → 403
- **重启工具收紧（第三轮，R7）**：删除 `--force` / `--cmd` / `--move`；隐藏 worker 必须凭一次性交接文件
  拉起，结束进程前独立复核 PID / 可执行路径 / 命令行 / 启动时间 / 端口监听 / `/api/state`；
  只逐个结束已确认归 WBRelay 的 PID（**不带 `/T`**），出现不明后代就停止并报告；启动命令只允许本仓库
  `relay.py` 或仓库内 `WBRelay.exe`
- **隔离启动器数据目录路径保护（第三轮）**：启动前解析绝对路径与 junction/symlink，
  拒绝 `%APPDATA%` 整棵树（含 `WBRelay` / `WBRelay-OSS` / `WBRelay-legacy-*`）、仓库目录、`~\.codex`、
  `WBRELAY_FORBIDDEN_PATHS`；拒绝 8789 并检查端口空闲（不自动顺延、不杀进程）
- **发布扫描显式标识清单（第三轮）**：`tools/release_identifiers.txt` 逐项登记 `approved` /
  `pending-owner-confirmation`；未批准标识命中让发布扫描失败（fail closed），只输出类型/路径/行号/计数
- **外部上游改为只读（2026-10-12，第二轮发布修复）**：删除「启动 / 停止外部上游」能力与对应界面按钮；
  旧接口 `/api/upstream/start|stop` 只返回 409 + 策略说明，不启动/停止任何进程，也不操作计划任务
- **设置保存不再隐式改写注册表**：只有请求显式带 `autostart` 且值真的变化时才同步 HKCU Run；
  只改其它设置不再可能误删启动项
- **`--readonly` 零写入补齐**：只读模式下不再生成/覆写 `DATA/WBRelay.ico`，不写注册表，
  IDE 接管的底层写入原语也一并拒绝
- **SSE 终止判定收紧**：Chat 只认 `data: [DONE]`（`chat.completion` 只是 chunk 的 object 值），
  Responses 只认 `response.completed` / `failed` / `incomplete` / `cancelled`；截断不再被当成正常结束
- **上游读取边界统一**：`/models` 拉取、供应商测速、OpenRouter 档案同步、catalog URL 导入
  全部带超时 + 响应体上限（超限不整体读进内存）、句柄一定关闭、错误先脱敏
- **身份接管（2026-10-11）**：本项目正式继承 `WBRelay` 身份——默认数据目录由 `%APPDATA%\WBRelay-OSS`
  改名为 `%APPDATA%\WBRelay`；接管前旧实例数据归档为 `%APPDATA%\WBRelay-legacy-<时间戳>`（不删）；
  安全阀改为拒绝指向 `WBRelay-OSS` / `WBRelay-legacy-*` 历史目录
- 架构再拆分（P1）：`web.py`（1202 行）→ `wbrelay/web/` 子包（server + api_v1 / api_admin / api_state / api_usage / api_upstream）；
  `ides.py`（655 行）→ `wbrelay/ides/` 子包（base / codex / opencode / dsh）；
   上游面板集成 → `wbrelay/integrations/upstream.py`（默认不激活）
- Codex 接管**默认关闭**（原先默认开启）
- `GET /api/settings` 不再明文回传管理令牌，改为 `admin_token_set` 布尔值；
  自动生成的新令牌只在本次 `POST /api/settings` 响应中回传一次
- 上游口令不再落盘（改为浏览器本地保管）
- `POST /api/codex/sync` 默认只同步模型清单，重启 Codex 需显式传 `restart=true`
- 以下配置项取代原本硬编码的本机路径：
  `upstream_dir` / `upstream_task` / `upstream_catalog_path` / `codex_provider` / `codex_aumid`
- Codex 模型清单文件名 `upstream-models.json` → `wbrelay-models.json`
- 设置页布局重构：左侧分区导航（滚动高亮 / 点击跳转）+ 右侧设置卡片 + 底部常驻保存条；
  主项（分区标题带）与子项（缩进设置行）分级；布尔项统一为开关样式；数字项加单位后缀
- 设置页移除「备份 / 恢复」UI（后端 `/api/backup`·`/api/restore` 与 `wbrelay/backup.py` 保留）
- 模型配置页布局重构：标题 / IDE 客户端卡片 / **带边框的「重新拉取」容器**（按钮 + 说明 + 统计）/
  **带边框的分组容器**；统计串由一坨括号文字改为 6 枚「标签 + 数值」胶囊；每个上游分组改为独立白卡片
  （1px 边框 + 圆角 + 阴影），分组内表格去掉自带外框、分组头折叠时分隔线透明
- 供应商卡片操作区重构：由「开关 + 4 个无边框灰字按钮 + 50px 测速空槽」改为 **状态组 ｜ 操作组 ｜ 危险操作组**
  （1px 竖分隔线）；按钮改为小号有边框按钮；测速结果固定 56px 槽位（`未测` → `测速中…` → `123 ms` 绿 / `失败` 红），
  结果出现不再顶动其它按钮；≤980px 操作区整行落到卡片下方右对齐
- 供应商编辑弹窗重构：拆为**连接** / **模型与运行**两组；`.row2` 修正为真两列（超时 / 启用同排）；
  模型清单说明从标签移出；Base URL 加「必填」徽标；名称 / 接口地址 / API Key 三个输入框后加**粘贴**按钮
- 命名统一（2026-10-12）：产品名 **WBRelay** + 功能副标题「多供应商自动路由」；看板品牌 / 窗口标题 / 托盘统一为 `WBRelay · 多供应商自动路由`；对外包名 `wbrelay-oss` → **`wbrelay-gateway`**；版权行与作者改为 `WBRelay contributors`；历史标识 `WBRelay-OSS` 仅保留在接管前的叙述与安全阀里（见 `docs/方案-文案与术语统一.md` 六）
- 规则调整（2026-10-12）：内部规则 R5 收窄为**只约束外部进程**；新增 **R7：会话内重启网关必须走值守进程**
  ——网关是本机 Codex 的上游，重启瞬间会话会随之中断，所以「停 → 起 → 等就绪」只能由
  `tools/restart-gateway.py` 的脱会话值守进程完成（禁止手工 `taskkill` / 裸启动绕过）

- **`load_settings()` 现返回深拷贝**（2026-10-13）：此前返回进程内共享的缓存 dict，「读 → 改 → `save_settings(data)`」这种写法会让调用方的顺手改动**污染全局配置**——不落盘、不报错，但下一次保存时又被当成「当前状态」整体写回；嵌套结构（`suppliers` / `api_keys` / `group_models_enabled` / `model_prices`）同样受影响。现一律 `copy.deepcopy`，并新增内部 `_load_settings_cached()`（返回共享对象，仅供 `settings.py` 内部只读派生查询使用）
- **日志轮转改为多代保留**（2026-10-13）：`wb.log` 超过 2MB 时滚动，旧内容不再被单个 `.old` 覆盖，而是顺次下移到 `wb.log.1` / `wb.log.2` / `wb.log.3`，**最多保留 3 代**；滚动改持锁完成，消除并发写窗口
- **HTTP 服务改用 `_GatewayServer`**（2026-10-13，`wbrelay/app.py`）：`ThreadingHTTPServer` 子类，差异只有两处且都只影响异常与退出路径 —— `daemon_threads = True`（进程退出不再等待残留请求线程）；覆写 `handle_error()`：客户端主动断开静默忽略，其余异常只记「类型 + 脱敏后的消息」，**不打印堆栈**

- **界面「使用说明」随界面同步更新**（2026-10-14）：第 3 步改成「内置四款一键写入 / 其它客户端用『其他 IDE』卡片」；
  「上游服务」改成「把上游面板原样嵌进来看（同源代理）」；「常见操作」补 IDE 接管归属四态与「其他 IDE」卡片的地址/Key 用法；
  「文件与排错」补「客户端拉不到模型怎么办」与协议范围（只有 OpenAI 的 chat / responses）
### 修复

- **Codex 接管后关掉开关并不能回到接管前（2026-10-14）**：`_codex_inject()` 会改 5 处（`model_provider`、
  `[model_providers.<name>]` 段名与段内 `name`、`base_url`、`experimental_bearer_token`、新增顶层 `model_catalog_json`），
  而 `_codex_restore()` 只回写 2 处 —— 「关掉接管」后段名 / `model_provider` / 模型清单三处**残留**。
  现在注入前留存 `config.toml` 原文快照（`ide_backups/codex.json` 的 `{raw, existed}`），还原**逐字节回写**；
  老版字段级备份与 `codex_original_config.json` 仍能识别并迁移
- **Codex 注入可能改错 provider 段（2026-10-14）**：`base_url` 的替换正则是「全文件第一个匹配」，
  多 provider 的 `config.toml` 里可能改到别人的段。现改为最小 TOML 解析（表 / 键定位），只在目标
  `[model_providers.<name>]` 段内改键；重复表 / 重复键直接拒绝，不猜
- **IDE 接管失败被报成成功（2026-10-14）**：备份或写入失败时接口仍返回 200、开关停在「开」。
  现在 `inject` / `restore` 的返回值必须被检查：接管失败回滚开关并返回 500；恢复失败**保留备份**以便重试
- **IDE 配置文件没有读取护栏（2026-10-14）**：新增大文件（8MB）与非法 UTF-8 护栏，超限即拒绝且**一个字节都不写**；
  `--readonly` 下备份 / 注入 / 还原 / 同步全部零写入


- **`tools/restart-gateway.py` 在中文路径 + 打包版在用实例上完全不可用（2026-10-13，R7 入口失效）**：
  两个独立缺陷，都只在**正在运行的在用实例**上暴露 ——
  ① **进程信息解码**：`_cim_json` 用 `text=True`（本机 cp936）解码 PowerShell 管道，而 PowerShell 在管道里写的是
  **UTF-8**；含中文的路径被解成乱码（例如「代码仓库」→「浠ｇ爜浠撳簱」），
  身份核验里「exe 必须在本仓库目录内」必然失败。修法：让 PowerShell 先把 JSON 里所有非 ASCII 字符转成
  `\uXXXX`（`[Regex]::Replace` + 脚本块求值），管道里只剩 ASCII、`json.loads` 再还原；
  顺带给 stdout 加 `errors="replace"`，生僻字符不会把工具自己打崩。
  ② **打包版 WebView2 后代归属**：打包版 GUI 实测会拉起 6 个 `msedgewebview2.exe` 子进程 —— 主 / GPU / 网络 /
  存储 / renderer 带 `--webview-exe-name=WBRelay.exe`，而旧规则只认端口 / 数据目录标记；crashpad-handler 连这个参数都没有。
  修法：把 `--webview-exe-name=<目标 exe 名>` 作为证据，并新增**两遍核验** —— 与已确认的 WebView 兄弟进程
  共享 `--user-data-dir` 的 WebView2 进程跟随确认（`classify_own_children()`，父进程侧与 worker 侧共用；
  「认不出来就不碰」的原则不变，仍然没有 `--force`）。
  验证：`tests/test_restart_gateway.py` 新增 G8j / G8k / G8l / G8m（WebView 证据 + 两遍核验）与
  G10a~G10d（非 ASCII 字符串经 CIM 管道往返逐字符一致 + 真实 CIM 只读往返），**49 通过 / 0 失败**；
  并在在用实例（打包版 + 中文仓库路径）上实测值守重启：23:40:06 派发 → 23:40:15 就绪，网关中断约 2 秒。

- **OpenCode 接管修复：档位缺失 + 协议矩阵缺一格（2026-10-13）**
  - **档位（有模型、没有 low/high/max 可选）**：`wbrelay/ides/opencode.py` 注入的是
    `supportsResponsesApi` / `supported_reasoning_levels` —— 客户端（OpenCode 1.18.18 桌面端）
    **全库 0 命中**、完全不读，档位列表取自 `Object.keys(model.variants)`。现改为写 `variants`
    （来源 `models_view` 的 `reasoning_efforts`），并把注入 / 同步收口到唯一的
    `_opencode_model_entries()`（原先两处重复，容易改一处漏一处）。
  - **通信（发消息必失败，且连带打断 Codex 60 秒）**：协议矩阵缺「chat 客户端 × responses 上游」
    一格 —— `_supplier_send` 直接 `raise ValueError("wire_api=responses 不能服务 chat 请求")`，
    又落进「供应商故障」分支被 `supplier_mark_cool` 冷却 60 秒，503 文案还误报「请检查供应商是否启用」。
    现补齐第四象限：`wbrelay/protocol.py` 新增 `chat_to_responses()` / `responses_to_chat_completion()` /
    `usage_to_chat()`，`wbrelay/web/api_v1.py` 新增 `_responses_stream_to_chat()`（流式逐事件翻译）
    与非流式转换；冷却记原因（`state._cooldown_reasons`），503 区分「无供应商」与「都在冷却（原因 + 剩余秒数）」，
    `/api/state` 供应商条目新增只读字段 `cool_reason`。
  - 验证：新增 `tests/test_protocol_matrix.py`（**28 断言**：四象限 + 非流式转换 + 截断记账 +
    「已开始输出绝不切换」+ 冷却文案）与 `tests/test_opencode_inject.py`（**16 断言**：档位/variants、
    不再写无效字段、组级停用略过、他人 provider 原样保留、同步幂等）；既有回归全绿
    （routing 33 / SSE 21 / e2e 25 / hardening 53 / readonly 43 …）。方案见 `docs/方案-OpenCode接管修复.md`

- `/api/ides/{id}/toggle` 原先无法把关闭状态的 IDE 打开（门禁判断条件错误）
- 设置页上游端口徽标恒为灰色，无法区分「自动检测成功」与「退回默认 8788」
- 设置页主题选择框从不回显当前主题，恒显示第一项「浅色」
- 「配置文件位置」在旧网关进程（未返回 `paths`）下会误报「未生成」，现显示「未提供」并提示重启
- 供应商卡片「删除」按钮不是危险色（`class="ghost danger"` 里 `.ghost` 覆盖了 `.danger`，四个按钮同灰）
- 「编辑供应商」说明与实际行为不符：API Key 掩码示例写作 `sk-S***1XNS`（实际是「前 4 位 + 6 个星号 + 后 4 位」），
  且没说明「原样不动 = 保持原 Key」
- 模型配置页窄屏下列表区被固定的标题 + IDE 卡片 + 拉取容器压到仅剩约 27px
- `tests/preview_readonly_server.py` 在 M3 拆分后仍 `import relay.Handler`，预览服务启动即报 `AttributeError`
  （改为 `wbrelay.web.server.Handler`，并强制追加 `--readonly`）
- 打包版与源码版同时跑时被误判成「其他进程占用 8789」→ 自动顺延到 8790 并**改写 Codex 配置**；
  现双开判定补一条 `/api/state` 探针（源码模式跑的是 `python.exe`，只看进程名认不出来），
  命中即退出并提示「另一个 WBRelay 占用」
- `wbrelay/catalog.py`：模板链全部失效时会生成**缺 `shell_type`** 的 catalog（Codex 会直接拒绝启动），
  现逐条兜底 `shell_command`；新增 `validate_codex_catalog()`，落盘前自检不通过则**保留上一版不覆盖**
  （新增 `tests/test_catalog_guard.py`，12 条断言）
- `tests/test_usage_filters.py` / `tests/test_supplier_label.py`：M3 拆分后仍引用 `relay.Handler` /
  `relay.load_usage_rows` / `relay.DATA` 等旧顶层符号，脚本一跑就 `AttributeError`；改为引用
  `wbrelay.usage` / `wbrelay.settings` / `wbrelay.paths` / `wbrelay.web.server`（19 通过 / 16 通过）
- `tests/test_e2e.py`：三个真问题 —— ① `GW` 写死 `http://127.0.0.1:8789`（在用实例），按提示设
  `WBRELAY_E2E_ALLOW=1` 就会**覆写在用配置**；改为 `WBRELAY_E2E_GW` 可配、默认 `18790`，并**硬拦 8789**
  （需 `WBRELAY_E2E_ALLOW_LIVE=1`）。② 真实 8788 供应商在未设 `WB_KEY` 时带空 Key → 后端拒绝整个保存，
  连锁拖垮后续全部步骤；改为**没 Key 就不挂该供应商、相应步骤 SKIP**。③ 流式步骤未捕获 `HTTPError`，
  直接抛栈中断脚本；改为记失败项。修后隔离实跑（mocks 18788/18789 + 实例 18790）：**PASS=15 / FAIL=0**

- **看板用量明细行的 XSS（高）**：`dashboard.html` 用量明细里 `JSON.stringify(r)` 被原样拼进 `onmouseenter="showCostTip(event,model,<json>)"` 的 HTML 属性，遇到含引号 / 尖括号的上游模型名或供应商名即可**突破属性边界注入任意 JS**（恶意或被污染的上游返回内容即触发，触发者是在看板里的用户）。修法：属性里只放**数字索引**，行数据统一收到模块级 `USAGE_ROWS` 数组，`showCostTip` 签名由 `(e, model, r)` 改为 `(e, i)`，取数时按索引越界保护。验证：`tests/check_dashboard_js.py`（内联 JS 语法）+ 静态确认 `JSON.stringify` 不再进任何 HTML 属性
- **`load_usage_rows()` 增量缓存并发竞争（高）**：`os.stat` + 读文件 + 更新 `_USAGE_CACHE` 整个临界区没有锁，多个看板页面同时自动刷新时两个线程读到同一个旧 `size`、都 `seek` 到同一位置、都解析同一段新增行、都拼到缓存行上 → **同一行被重复计入，KPI 虚高**（实测 500 行被算成 1413 行，虚高 2.83 倍）。修法：整个临界区持 `state._lock`，锁内**重新读取**缓存键（`prev_mtime` / `prev_size` / `prev_rows`），绝不用进锁前读到的旧值去 seek。验证：`tests/test_usage_cache_concurrency.py` —— 修复前 8 通过 / 12 失败（`[FAIL] C1a 缓存行数 == 文件真实行数 | 缓存 1413 / 真实 500 / 期望 500`），修复后 **20 通过 / 0 失败**
- **路由表签名粒度过粗（中）**：`_route_table_sig()` 用 `"%d" % st_mtime` 把小数秒截断，于是**同一整数秒内两次字节数完全相同的写入**会得到一模一样的签名 → 路由表不重建 → 已停用的模型仍留在 `/v1/models` 里、请求仍被路由过去（实测 60 次重试内稳定复现）。修法：新增 `_file_sig()` helper 改用 `st_mtime_ns` + size（Windows NTFS 纳秒粒度足以区分同秒连续写入）。验证：`tests/test_routing_semantics.py` 新增 T12a~T12h（构造同秒同 size 的两次真实原子写），**33 通过 / 0 失败**
- **`load_settings()` 返回共享缓存 dict（中）**：调用方「读 → 改 → `save_settings(data)`」的顺手改动会**污染全局配置**——不落盘、不报错，但下一次保存时又被当成「当前状态」整体写回；嵌套结构（`suppliers` / `api_keys` / `group_models_enabled` / `model_prices`）同样共享。修法：一律返回 `copy.deepcopy`，新增内部 `_load_settings_cached()`（共享对象，仅供内部只读派生查询）。验证：`tests/test_settings_concurrency.py` A1~A4（顶层与深层嵌套互不影响、缓存未被污染）
- **配置 read-modify-write 无锁 → 并发丢更新（中）**：`save_settings()` 只在写盘瞬间持锁，调用方「读 → 改 → 写」全程无保护，两个并发 POST 即可互相覆盖（A 拿着旧快照，B 已清空 `api_keys` 并保存，A 醒来把含旧 Key 的快照写回，B 的删除被静默还原）。修法：新增持锁原语 `update_settings(mutator)`（持同一把 RLock 完成整段 read-modify-write，内含 `_NO_SAVE` 哨兵），并改造 9 处调用点。验证：`tests/test_settings_concurrency.py` B1~B4 + C1~C2（事件握手固定竞争窗口，与机器速度无关；含并发 `POST /api/settings` 与 `/api/keys` 互不覆盖）
- **测试脚本在 GBK 控制台下必然崩溃（中）**：`tests/test_package_layout.py` 直接 `print()` 中文断言名，在中文 Windows 默认 GBK 控制台 + `sys.stdout.encoding='gbk'` 下遇到无法编码的字符即 `UnicodeEncodeError`，脚本**跑不完就崩**；同源问题还有子进程管道编码不匹配 —— 子进程吐 GBK 字节、父进程按 UTF-8 解码得乱码，中文关键字断言**假失败**。修法：加 `_out()` 安全输出（`UnicodeEncodeError` 时按控制台编码 replace 降级），并补 `_child_env()` 给子进程设 `PYTHONIOENCODING=utf-8`。验证：默认 GBK 控制台实测 `tests/test_package_layout.py` **23 通过 / 0 失败**，退出码 0
- **`wbrelay/app.py` 未覆写 `handle_error`（低）**：`http.server` 的默认实现把**完整 traceback** 写到 stderr —— 源码模式下泄漏内部绝对路径与调用栈，`--noconsole` 的 EXE 模式下这些输出又直接丢失，排障时反而看不到；且客户端主动断开（Codex 取消请求 / 浏览器切页）也会打出一整屏堆栈。修法：新增 `_GatewayServer` 子类（`daemon_threads=True` + `handle_error` 收敛）：`BrokenPipeError` / `ConnectionResetError` / `ConnectionAbortedError` 静默忽略，其余异常只记「类型 + 脱敏后的消息」并走统一 `_log()` 出口，**不打印堆栈**。验证：`tests/test_http_error_handling.py` **26 通过 / 0 失败**（stderr 与 `wb.log` 均无堆栈、假 Key 已脱敏、服务器异常后仍正常 200）
- **成本汇率硬编码（低，已修复）**：`wbrelay/usage.py` 的 `_cost_per_request()` 把美元兑人民币汇率写死成 `7.2`，使用者无法按实际汇率校准。修法：新增 `usd_cny_rate` 配置项（默认 `7.2`，范围 `0.1`~`100`），前端 tooltip 标注为**估算值**。验证：`tests/test_cost_rate.py`（32 断言，覆盖默认口径回归 / 费率变化 / 非法值回落 / 聚合一致性 / 历史行不含费率）
- **日志轮转只保留一代且有竞争窗口（低，已修复）**：`wbrelay/log.py` 的滚动用 `os.replace` 把 `wb.log` 覆盖成单个 `wb.log.old`，只留一代历史，且「查大小 → 改名 → 追加写」不是原子的，并发写日志时存在内容丢失窗口。修法：改为多代保留（`wb.log.1` / `wb.log.2` / `wb.log.3`，最多 3 代）并加锁。验证：`tests/test_log_rotation.py`（46 断言，覆盖多代保留 / 无行丢失 / 并发写 / 迁移兼容 / 只读零写入）

### 移除

- 所有硬编码私有路径（上游项目目录、固定 Python 安装路径、私有 catalog 路径）
- 上游项目目录的自动探测；现仅认显式配置
