# 贡献与问题反馈

感谢愿意帮忙改进 WBRelay。请先读完本文件再看代码。

## 一、代码约束（违反的改动会被回滚）

| 约束 | 说明 |
|---|---|
| Python 3.9 | 不使用 3.10+ 语法（`match`、`X | Y` 类型联合等） |
| 标准库优先 | 网关核心**只用标准库**；GUI 依赖（pywebview / pystray / Pillow）必须是可选 extras 且可缺省降级 |
| 原子写 | 配置类文件一律同目录临时文件 + `os.replace`（见 `wbrelay/atomicio.py`） |
| 端口 | 默认 8789；开发与测试**一律显式指定非 8789 端口** |
| 数据隔离 | 测试必须用自建临时数据目录（`tests/isolate.py`）+ mock 上游，禁止读写真实用户数据 |
| 进程边界 | 外部上游项目**只读**（不写、不改、不启停、不代理转发）；不自动启停任何外部程序；
本网关自身的重启走 `tools/restart-gateway.py`（没有 `--force` / `--cmd` / `--move`，
不按端口或 basename 认进程） |

## 二、本地开发

```powershell
python relay.py --data-dir $env:TEMP\wbrelay-dev --no-ide-inject --port 18789 --headless
```

或直接用隔离启动器：`powershell -ExecutionPolicy Bypass -File tools\run-isolated.ps1 -Headless`

## 三、提交前自检

```powershell
python tests/check_dashboard_js.py
python tests/exercise_core.py
python tests/test_readonly_zero_write.py
python tests/test_atomic_writes.py
python tests/test_routing_semantics.py
python tests/test_settings_concurrency.py
python tests/test_usage_cache_concurrency.py
python tests/test_http_error_handling.py
python tests/test_cost_rate.py              # 成本汇率配置项 usd_cny_rate
python tests/test_log_rotation.py           # 日志多代轮转与并发写
python tests/test_secret_redaction.py
python tests/test_hardening.py
python tests/test_package_layout.py
python tests/test_sse_terminators.py
python tests/test_protocol_matrix.py        # 协议矩阵四象限（chat/responses × chat/responses）
python tests/test_opencode_inject.py        # OpenCode 注入形态：档位 variants / 无效字段 / 幂等
python tests/test_upstream_readonly.py
python tests/test_upstream_read_limits.py
python tests/test_settings_autostart.py
python tests/test_restart_gateway.py
python tests/test_data_path_policy.py
python tests/test_scan_sensitive.py
python tests/frozen_onefile_ownership.py --self-test
python tests/smoke_headless.py 18789
python tests/test_e2e.py
```

> 这些脚本在**任何控制台代码页下都不会因编码崩溃**：`tests/isolate.py` 与 `tools/*.py` 统一把
> stdout / stderr 设成 `errors="replace"`，装不下的中文降级成 `?` 而不是抛 `UnicodeEncodeError`；
> 拉起子进程时也会固定子进程 IO 编码，否则中文断言名会把脚本自己打成 `UnicodeEncodeError`，
> 或让中文关键字断言因为管道编码不匹配而**假失败**。
>
> 想看**正常的中文**输出，用 UTF-8 控制台或设 `PYTHONIOENCODING=utf-8`（**CI 已这么配**：
> GitHub Actions 的 Windows 运行器默认是 cp1252，不配就全是 `?`）。中文 Windows 自带的
> GBK 控制台无需任何设置。

提交前还要跑发布扫描（会用**显式标识清单**；未批准标识存在时**必须失败**，不能把失败当误报）：

```powershell
python tools/scan_sensitive.py --identifiers tools/release_identifiers.txt
```

扫描器还会检查两件与发布直接相关的事：`.git` 里有没有 **refs 之外的对象**
（被 amend 顶掉的提交不在 `git log --all` 里，但仍能被 `git show` 取出），
以及**署名**是否已由所有者确认。任一项有未决项，退出码就是 1。

新增上游 / 供应商 / 客户标识时**不要把明文写进清单**（清单会随发布一起走），
用下面的命令生成 `sha256` 条目再登记；`pending-owner-confirmation` 的条目
要等仓库所有者逐项确认后才能改成 `approved`：

```powershell
python tools/scan_sensitive.py --hash <标识原文>          # vendor 条目
python tools/scan_sensitive.py --hash <邮箱> --author     # 署名条目
```

内部资料（本机接管 / 事故 / 审查记录与两个私有校验工具）集中在 `_internal/`，由 `.gitignore`
覆盖 —— 不进版本控制、不随发布，`git add -A` 不会带出。`tools/publish_excludes.txt` 是
「哪些路径算内部资料」的唯一来源（README 只做索引），扫描器据此把命中归类而不阻断。
想预览「将要公开哪些文件」时用 `python tools\make_publish_copy.py --dest <目录>`
（默认 dry-run，不写盘）。

改了 `dashboard.html` 之后必须同步包内副本（否则 `test_package_layout.py` 会失败）：

```powershell
python tools/sync_dashboard.py
```

## 四、不要提交的东西

- 任何真实凭据：`sk-` Key、Bearer 令牌、admin token、账号口令
- 真实上游域名 / 内网地址 / 个人绝对路径 / 用户名 / 邮箱
- 运行数据：`settings.json`、`usage.jsonl`、`*.log`、`backups/`、`ide_backups/`
- 含真实供应商名或凭据的截图

这些都在 `.gitignore` 之外**也要**在提交前自查一遍（`git status` + `git diff --cached`）。

## 五、问题反馈

- 功能问题 / 使用疑问：开 issue，附**脱敏后**的复现步骤与日志片段
- 安全问题：见 `SECURITY.md`，**不要**用公开 issue 披露未修复漏洞
- 报 bug 时请写清：版本（`/api/state` 的 `version`）、端口、是否开启 IDE 接管、是否只读模式
