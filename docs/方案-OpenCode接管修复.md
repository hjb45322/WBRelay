# 方案 ・ OpenCode 接管修复与协议矩阵补全

> 状态：实施中（2026-10-13）
> 关联：`AGENTS.md`（R5 / R7）、`docs/规则-隔离与数据保护.md`（验收）、`docs/方案-架构拆分.md`（`wbrelay/ides/` 子包）
> 触发：接管 OpenCode 桌面端后「有模型、没有档位选择」且「发消息必失败」

---

## 一、问题

| # | 现象 | 影响 |
|---|---|---|
| S1 | OpenCode 里能看到模型列表，但**没有推理档位（low/high/max）可选** | 档位功能整体缺失 |
| S2 | 发消息必失败：`no usable supplier for model '<model-id>' - 请在看板检查供应商是否启用` | OpenCode 不可用；**并且把同一进程内的 Codex 会话一起打断 60 秒** |

两个现象互不相关，是两处独立缺陷。

---

## 二、取证（只读，2026-10-13）

环境：OpenCode 桌面端 1.18.18（`%LOCALAPPDATA%\Programs\@opencode-aidesktop`，build 2026-08-13；
配置 `~/.config/opencode/opencode.json`）。

### 2.1 实机日志与用量

`%APPDATA%\WBRelay\wb.log`：

```
... [WBRelay] OpenCode 已指向 WBRelay: http://127.0.0.1:8789/v1（13 个模型）
... [WBRelay] supplier <sup-id> failed: wire_api=responses 不能服务 chat 请求 -> next
... [WBRelay] supplier <sup-id> failed: wire_api=responses 不能服务 chat 请求 -> next
```

同一分钟的用量记录出现 502（`realm="supplier:all_failed"`，message 含「不能服务 chat 请求」），
随后 60 秒内是 503（`realm="none"`，message 为 `no usable supplier ...`）；冷却过期后同一模型立即恢复。

### 2.2 网关侧定位

| 位置 | 事实 |
|---|---|
| `wbrelay/web/api_v1.py` `_supplier_send` | `wire_api=responses` 且客户端不是 `/v1/responses` 时直接 `raise ValueError` —— **协议矩阵缺一格** |
| `wbrelay/web/api_v1.py` `_supplier_dispatch` | 该异常落入 `except Exception` → `supplier_mark_cool(sid, 60s)`：把「能力缺失」当成「供应商故障」 |
| `wbrelay/routing.py` `pick_supplier` | 冷却中的供应商被跳过 → 返回 `None` |
| `wbrelay/web/api_v1.py` `_dispatch` | 回 503，文案「请在看板检查供应商是否启用」与事实不符（供应商是启用的，只是冷却中） |
| `wbrelay/ides/opencode.py` | 注入与同步两处**重复**构造模型条目，写入 `supportsResponsesApi` / `supported_reasoning_levels` |

### 2.3 客户端侧取证（客户端安装目录 `resources/app.asar` 逐行核对）

| 结论 | 证据 |
|---|---|
| `supported_reasoning_levels` / `supportsResponsesApi` 在客户端**全库 0 命中** | 全文检索 |
| provider 配置里模型的合法字段：`name / limit / cost / reasoning / tool_call / provider / options / headers / **variants**` | `Model2` schema |
| 配置 → 目录：只有 `variants` 会变成档位（`reasoning` 字段在配置路径下不下发） | 配置变换函数 |
| 档位列表 = `Object.keys(model.variants)` | 客户端 UI 的 model 选择器 |
| `options.baseURL` → `api.url`；`@ai-sdk/openai` 走 `/responses`、`@ai-sdk/openai-compatible` 走 `/chat/completions` | 客户端 provider 迁移与路由选择函数 |

上游侧（只读核对 `<上游项目目录>`）：同时提供 `/v1/responses` 与 `/v1/chat/completions`，问题不在上游。

### 2.4 根因

- **R-1（档位）**：注入了客户端不认识的字段。档位必须写成 `variants`。
- **R-2（通信）**：协议矩阵缺「chat 客户端 × responses 上游」一格。
- **R-3（连带伤害）**：能力缺失被当成供应商故障 → 冷却 60 秒 + 误导文案，波及同进程内其它客户端。

---

## 三、协议矩阵（修复目标）

| 客户端 | 上游 `wire_api=responses` | 上游 `wire_api=chat` |
|---|---|---|
| `/v1/responses`（Codex） | 透传（已有） | 翻译（已有） |
| `/v1/chat/completions`（OpenCode 默认） | **翻译（本方案）** | 透传（已有） |

四格都要有测试覆盖：`tests/test_protocol_matrix.py`。

---

## 四、改动

### 4.1 `wbrelay/ides/opencode.py`

- 抽出**唯一**的模型条目构造点 `_opencode_model_entries()`，注入与同步共用（消除漂移）。
- 档位写 `variants`：`{"<effort>": {"reasoningEffort": "<effort>"}}`，来源 `models_view()` 的 `reasoning_efforts`（空则不写）。
- 移除 `supportsResponsesApi` / `supported_reasoning_levels`（客户端不读，留着只会让后来者继续写错）。
- provider 保持 `npm=@ai-sdk/openai-compatible`（即 chat 客户端路径）——由网关侧补翻译，不依赖客户端内部路由。

### 4.2 `wbrelay/protocol.py`

- 新增 `chat_to_responses(payload)`：chat 请求 → responses 请求
  （system → `instructions`；messages → `input` 条目；assistant `tool_calls` → `function_call`；
  `role=tool` → `function_call_output`；`tools` 形态转换；`tool_choice` 转换；
  `max_tokens`/`max_completion_tokens` → `max_output_tokens`；`reasoning_effort` → `reasoning.effort`；
  `temperature` / `top_p` / `seed` 透传；`frequency_penalty` / `presence_penalty` / `stop` 显式丢弃并在文档说明）。
- 新增 `responses_to_chat_completion(obj, model)`：responses 响应 → chat completion 响应（非流式）。

### 4.3 `wbrelay/web/api_v1.py`

- `_supplier_send`：responses 上游 + chat 客户端 → **翻译请求体**（不再抛异常）。
- 新增 `_responses_stream_to_chat()`：responses SSE → chat SSE 逐事件翻译
  （`response.output_text.delta` → `choices[].delta.content`；
   `response.reasoning_summary_text.delta` / `response.reasoning_text.delta` → `delta.reasoning_content`；
   `response.output_item.added(function_call)` + `response.function_call_arguments.delta` → `delta.tool_calls`；
   `response.completed` → 终块 + usage 块 + `data: [DONE]`）。
- `_supplier_output`：新增「responses 上游 × chat 客户端」分支（流式 / 非流式）。
- `_dispatch`：503 文案区分「无供应商可路由」与「供应商都在冷却（含剩余秒数与最近失败原因）」。

### 4.4 `wbrelay/routing.py` / `state.py` / `settings.py` / `web/api_state.py`

- `_cooldown_reasons`（进程内）记录冷却原因；`supplier_mark_cool(sid, seconds, reason="")`。
- 新增 `supplier_cooldown_remaining()` / `supplier_cooldown_reason()` / `cooling_suppliers_for(model)`。
- 保存配置清冷却时**一并清原因**（核心语义 2「保存即清冷却」不变）。
- `/api/state` 的供应商条目增加只读字段 `cool_reason`。

### 4.5 测试

- `tests/test_protocol_matrix.py`（新）：四格全覆盖 + 新增方向的截断记账 + 「已开始输出绝不切换」+ 冷却文案。
- `tests/test_opencode_inject.py`（新）：注入与同步写出的条目含 `variants`、不含无效字段、`limit` 保留、同步幂等。

### 4.6 文档

- 本文件；`README.md` 增加「协议矩阵」小节；`CHANGELOG.md` 记录本条修复。

---

## 五、语义边界（不可破坏）

1. **失败必切换**：尚未向客户端输出前的失败仍按数组顺序切下一家（本方案不改）。
2. **配置保存即清冷却**：`save_settings` 同时清 `_cooldowns` 与 `_cooldown_reasons`。
3. **全败必报错**：返回最后一个错误，用量记 `realm=supplier:all_failed`（本方案不改）。
4. **一旦开始向客户端输出，绝不切换供应商**：新增方向同样遵守。
5. **截断判定**：新增方向沿用既有翻译方向的收尾策略——上游缺终止事件时补终块 + `[DONE]`，
   但用量记 `error=True`（与 `_chat_stream_to_responses` 一致）。两端收尾策略的统一见「七、后续项」。
6. **R5 / R7 不变**：不自动启停外部进程；网关重启只走 `tools/restart-gateway.py`。

---

## 六、验收

| 项 | 方法 |
|---|---|
| 单测全绿 | `python tests/test_protocol_matrix.py`、`tests/test_opencode_inject.py`、`tests/test_routing_semantics.py`、`tests/test_sse_terminators.py`、`tests/test_e2e.py`、`tests/test_readonly_zero_write.py` |
| 隔离断言 | 各测试内置 `isolate.assert_isolated()`；P1–P6 哈希前后一致 |
| 无敏感信息入库 | `python tools/scan_sensitive.py` |
| 实机 | 看板 OpenCode 卡片**显式点击**「↻ 重启」→ 档位出现 → 发消息成功、用量 `realm=supplier:<id>` |
| 回归 | Codex 会话不再被 60 秒冷却误伤 |
| 值守重启 | 在用实例（打包版 + 中文仓库路径）实测：23:40:06 派发值守进程 → 23:40:15 就绪，网关中断约 2 秒；期间修掉 `tools/restart-gateway.py` 两处 R7 入口缺陷（管道解码 / WebView2 后代归属），见 CHANGELOG |

---

## 七、后续项（不在本轮）

- 两个翻译方向对「上游截断」的收尾策略统一（当前：翻译方向补终止符 + 记 error）。
- `wire_api=auto`（上游能力协商）：需要上游能力探测与缓存，暂不做。
