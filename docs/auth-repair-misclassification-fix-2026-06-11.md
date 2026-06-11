# Auth Repair 误判事故 — 根因分析与修复设计（2026-06-11）

- 状态：设计稿已评审，第一阶段安全修复已落地（截图 best-effort / 软失败不落 `auth_invalid` / auth-repair 熔断 / rotate 默认不 force）
- 影响面：整个子号账号池被批量误标 `auth_invalid`，CPA 失去可用 active 凭证，Codex 请求 401/503，系统表现为"全卡死"
- 现场：`accounts.json` total=9 / standby=1 / **auth_invalid=8** / active=0

---

## 1. 一句话结论

**根因不是"账号真的都坏了"，而是 auth_repair 把 `Page.screenshot: Timeout`（一个纯调试用的临时浏览器异常）当成账号级认证失效处理，批量写成 `auth_invalid`，进而让 CPA 失去可发布的 active 凭证，下游 Codex 请求退化为 401 → 503。**

这是一条**单向不可逆**的脆弱耦合：临时的、本可自愈的环境故障，被永久地记录成账号级的凭证失效；而 `auth_invalid` 会被踢出轮转、停止同步 CPA，损伤不会自愈、反而沿账号池扩散。

---

## 2. 现场证据（均已在代码 / 日志中核实）

### 2.1 状态机轨迹（`state_log.jsonl`）
今天 15:18–19:15，8 个子号全部走同一条路：

```
standby -> active        reason = sync_account_states:in_team     # 发现仍在 Team，拉回 active
active  -> auth_invalid   reason = auth_repair:exception           # 修复中抛异常 → 判死
```

涉及账号：yun8 / yun10 / yun6 / yun7 / yun11 / yun5 / yun9 / yun12（共 8 个，`reason=auth_repair:exception` 八条）。

### 2.2 错误细节完全一致（`accounts.json` 各 auth_file 的 `auth_last_error_detail`）
```
Page.screenshot: Timeout 30000ms exceeded.
Call log:
  - taking page screenshot
```
不是 OpenAI 的 401/403，不是 token revoked，不是额度耗尽——是 **Playwright 截图超时**。

### 2.3 被误杀账号不是“已证明凭证失效”
8 个号的 auth_file **全部存在**，`refresh_token` 记录的本地过期时间到 **2026-06-20 / 06-21**（还剩约 10 天），其中 yun10 的 5h 额度还剩 **99%**。这不能等同于“token 实测有效”，但足以证明它们是**误伤候选**：需要只读 refresh / smoke / quota 探测确认，而不应直接当成凭证损坏永久排除。

### 2.4 触发源：Xvfb 泄漏
停服时发现 **75 个孤儿 `Xvfb` 进程，合计 RSS 4.6 GB**。`display.py:14` 在每次进程 import 时用 `xvfbwrapper` 起一个随机显示器，但清理绑在 atexit / 优雅退出上，而进程实际被硬杀重启 → 每次重启漏一个 Xvfb。累积导致渲染资源被拖垮，是本次截图超时的物理触发源。

---

## 3. 根因三层

### 第一层 — 直接根因：诊断截图失败被当成登录失败
`codex_auth.py:293-295`：
```python
def _screenshot(page, name):
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(SCREENSHOT_DIR / name), full_page=True)   # 无 try/except，full_page，默认 30s
```
截图只是调试存档（写到 `screenshots/`，无人实时消费），其失败**不代表** token 失效、也不代表无法登录。但它会抛异常，一路冒泡，最终在 `manager.py:1179` 被兜底捕获为 `error_type="exception"`。

### 第二层 — 核心设计问题：auth_repair 对 `exception` 处理过重
- `manager.py:295` 的 `AUTH_REPAIR_AGGRESSIVE_RELEASE_TYPES` **显式包含 `"exception"` 和 `"login_failed"`**，与 `add_phone`、`human_verification`、`non_team_plan` 这类**真·硬阻塞**并列。即：一个"未分类的兜底异常"被当成"和需要人工验证一样严重"。
- 最终状态写入（`manager.py` `_record_auth_repair_failure` 末尾）：
  ```python
  final_status = STATUS_STANDBY if (seat_released or not is_team_member) else STATUS_AUTH_INVALID
  update_account(email, status=final_status, _reason=f"auth_repair:{error_type}")
  ```
  **陷阱**：只要账号"还在 Team 里 + 这次没成功释放席位"，默认就落 `AUTH_INVALID`。`auth_invalid` 在这里被当成"我修不好、它还占着席位"的**垃圾兜底态**，语义被滥用为"凭证失效"。

`auth_invalid` 不是普通错误，它会联动影响：是否参与轮转 / 是否同步到 CPA / 是否被当占席异常 / 是否被后续清理替换。一次 Playwright 卡顿因此被放大成整池不可用。

### 第三层 — 系统联动放大：CPA 只发布 active 子号
`cpa_sync.py` 的 `_active_auth_publish_decision` 只处理 active 凭证；非 active 直接跳过。8 个号被误标后本地 `active=0` → CPA 无可发布 auth → Codex 请求退化为 `401 → 503 → 重试 → 更多 503` → "全卡死"。

---

## 4. 完整因果链

```
Xvfb 泄漏(75个/4.6GB)
   │  资源耗尽，页面渲染变慢
   ▼
_screenshot(full_page, 30s, 无 try/except) 超时抛异常
   │  manager.py:1179 兜底捕获
   ▼
error_type = "exception"  （未分类兜底）
   │  "exception" ∈ AGGRESSIVE_RELEASE_TYPES，且 still-in-team + 未释放席位
   ▼
status = auth_invalid     （单向、不可逆、会传染）
   │  CPA 只发布 active
   ▼
active=0 → CPA 无可用 auth → Codex 401 → 503 → 雪崩

放大回路：每次失败重试 3 次完整 OAuth，每次开浏览器；硬杀又漏新 Xvfb
         → 越失败越泄漏越慢越失败
```

> 注意优先级：**Xvfb 泄漏只是本次触发源**；只要"任何临时异常 → auth_invalid"这条链还在，下次换成网络抖 / 代理慢 / OpenAI 页面卡 / 验证码晚到，会一模一样地复发。因此**误判逻辑是高杠杆根因，截图与 Xvfb 是触发器**。

---

## 5. 影响边界

| 范围 | 判定 |
|---|---|
| 被误杀 | 恰好是 `auth_last_error_detail` 含 `Page.screenshot: Timeout` 的 8 个号，auth_file 完好、token 有效 ~10 天 → **可逆** |
| 未受影响 | yun3（standby，未走该路径）；所有账号数据 / token 未损坏；母号未被波及 |
| 资源 | 4.6 GB Xvfb 泄漏（事故处置时已清理） |
| 代码改动面 | `_screenshot` 为纯诊断（40+ 调用点，返回值从不被使用）→ 改它不影响任何业务逻辑 |

---

## 6. 解决方案（按"降低误伤"，不粗暴恢复、不盲目轮转）

### 主规则（贯穿全局）— Token 有效性闸门
**没有强证据证明账号凭证损坏，就绝不允许写 `auth_invalid`。**
本地 auth_file 存在、refresh_token 未到本地过期时间，只能说明“值得只读复核”，不能直接说明“实测有效”。真正的凭证有效性必须来自 refresh/token exchange、cheap smoke、quota API 等只读探测。枚举软/硬错误类型会随时间漂移；只读 token 探测才是最终闸门。

### 方案 1 — 截图 best-effort（切断本次触发点，已实施）
只改截图函数，不动状态机：
```python
def _screenshot(page, name):
    try:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / name), timeout=5000)  # 去 full_page，加短超时
    except Exception as exc:
        logger.warning("[截图] %s 失败，忽略: %s", name, exc)
```
影响面：所有 `_screenshot` 调用点（诊断用途，无返回值依赖）。

实际落点：
- `codex_auth._screenshot`
- `invite.screenshot`

### 方案 2 — `exception` 不允许直接打 auth_invalid（核心设计边界）
错误分级：

- **硬失败**（可 auth_invalid / 暂停）：明确 refresh/token invalid、明确 401/403 token invalid、`non_team_plan`、`add_phone`、`human_verification`
- **额度失败**（不应 auth_invalid）：`no_quota`、5h quota exhausted、weekly exhausted，应进入额度/冷却语义，而不是凭证失效语义
- **软失败**（不改 auth_invalid）：`Page.screenshot timeout`、Playwright timeout、网络异常、页面加载慢、OpenAI 5xx
  - 仅写 `auth_last_error` + `auth_retry_after`
  - 保持原状态（active/standby）
  - **不踢席位、不删凭证、不从 CPA 删除**

> **实现要点（务必）**：软失败必须**提前 return**，绝不进入 `seat-release + final_status` 那段逻辑——否则会掉进 `else` 分支照样写成 `AUTH_INVALID`（见 §3 第二层陷阱）。已把 `"exception"`/`"login_failed"` 移出 `AUTH_REPAIR_AGGRESSIVE_RELEASE_TYPES`，并为软失败新增"只记错、不改状态"的早退分支。

CPA 边界：active 账号若处在临时 auth_repair 冷却中，CPA 同步返回 `keep_remote`，不上传新凭证、不删除远端副本，避免把临时修复故障扩大到 CPA。

### 方案 3 — auth_repair 批量异常熔断（必须与方案 2 同船，已实施）
1 小时内多个账号因**同种** `auth_repair:exception` 失败 → 暂停自动修复 / 轮转，提示：
```
检测到多个账号认证修复异常，请检查代理 / Playwright / OpenAI 页面状态
```
理由：方案 2 把 exception 变成"软失败可重试"，若不熔断，环境性故障会触发**无限昂贵重试**（每次 3× 完整 OAuth + 开浏览器 + 烧验证码），并持续泄漏 Xvfb。现有烧号熔断只匹配 `from_state==active && to_state==exhausted`（`api.py:4192`），**auth_invalid 在其盲区**，需补一条独立熔断。

实际默认值：
```
AUTO_CHECK_AUTH_REPAIR_GUARD_ENABLED=true
AUTO_CHECK_AUTH_REPAIR_GUARD_WINDOW_SECONDS=3600
AUTO_CHECK_AUTH_REPAIR_GUARD_MAX_INVALID=4
AUTO_CHECK_AUTH_REPAIR_GUARD_COOLDOWN_SECONDS=300
```

### 方案 4 — 手动 rotate 不默认强制修复（已实施）
`api.py:3797` 现状 `cmd_rotate(..., force_auth_repair=True)`，绕过冷却 / `auth_retry_after`。改为默认 `force_auth_repair=False`（与函数默认 `manager.py:6624` 对齐），仅在显式点击"强制修复异常账号"时才 force。

### 方案 5（单列）— 修 Xvfb 泄漏（触发源治理，不阻塞上述安全修复，第一阶段已实施）
给 Xvfb 一个有界生命周期：单机共享一个显示器 / 用进程组确保随父进程死 / 或周期回收器；并在 api lifespan `finally`（`api.py:65`）里 `.stop()`。原则：**创建的资源必须有一条不依赖优雅退出的、保证执行的清理路径。**

已实施：`display.stop_virtual_display()` + `atexit` + FastAPI lifespan finally。注意：这不能覆盖 `kill -9`，但能覆盖正常 systemd/tmux 停止和多数重启路径。

---

## 7. 现场恢复（处置，不直接轮转）

把符合**全部**条件的账号判定为"误伤候选"，恢复到 `standby`（**不直接 active、不踢拉**），随后只做只读 quota/auth 探测：
```
status == auth_invalid
AND auth_last_error == "exception"
AND auth_last_error_detail 包含 "Page.screenshot"
AND auth_file 存在
```
恢复时一并清理 `auth_last_error* / auth_retry_paused / auth_retry_count`，确保账号真正重新可选。

已提供安全接口：

- `GET /api/auth-repair/recovery-candidates`：只读列出误伤候选。
- `POST /api/auth-repair/recover-misclassified`：默认 dry-run；传 `{"apply": true}` 才把精确匹配候选恢复到 `standby` 并清理 auth-repair retry 字段。

恢复接口不会把账号直接设为 `active`，也不会同步 CPA。恢复后必须再做只读 quota/auth 探测，确认真实可用后再进入正常轮转。

---

## 8. 实施计划：4 个可回滚小提交 + 1 条独立轨

| # | 提交 | 改动边界 | 回滚 |
|---|---|---|---|
| 1 | 截图 best-effort | 仅 `_screenshot`，不动状态机 | 单文件还原 |
| 2 | 错误分级：exception 保持 retry | 仅 auth_repair 分类 + 早退分支；硬失败逻辑不变 | 还原分类集合 |
| 3 | auth_repair 批量熔断 | 仅拦自动修复，不拦手动明确操作 | 关熔断开关 |
| 4 | 现场恢复脚本 / 按钮 | 只恢复符合 §7 条件的误伤账号 | 幂等、可重跑 |
| 5（独立轨）| Xvfb 生命周期治理 | `display.py` + lifespan finally | 单文件还原 |

> 纪律：**不改大流程，只加保护边界。** 每个提交独立、可回滚。

---

## 9. 验证矩阵

**单元测试**
- 截图异常被吞掉，不向上抛出。
- 软失败（screenshot timeout / playwright timeout / 网络 / 5xx）作用于**在 Team 的 active 子号**时，状态**保持 active/standby**，不变 auth_invalid（直击 §3 final_status 陷阱）。
- 硬失败 401/403 仍然 auth_invalid。
- `add_phone` / `human_verification` 仍然暂停或释放席位。
- 熔断：1 小时内多个 `auth_repair:exception` → 暂停自动修复并发出提示。

**运行态**
- 先只读检查（quota/auth 探测），不自动轮转。
- CPA 只同步真实 active；母号不进入 CPA。
- 恢复后确认 active 凭证回到 CPA，401/503 消失。

---

## 10. 附录：已核实的代码位置

| 关注点 | 位置 |
|---|---|
| 截图无 try/except、full_page、默认 30s | `codex_auth.py:293-295` |
| 登录异常兜底为 `error_type="exception"` | `manager.py:1179` |
| `"exception"`/`"login_failed"` 在激进释放集合 | `manager.py:295` 附近 `AUTH_REPAIR_AGGRESSIVE_RELEASE_TYPES` |
| final_status 默认落 auth_invalid 的条件 | `manager.py` `_record_auth_repair_failure` 末尾 `final_status = ... else STATUS_AUTH_INVALID` |
| CPA 仅发布 active 凭证 | `cpa_sync.py` `_active_auth_publish_decision` / 非 active skip |
| 烧号熔断仅匹配 active→exhausted | `api.py:4192-4193` |
| 手动 rotate 强制 force_auth_repair=True | `api.py:3797`（默认值 `manager.py:6624`） |
| Xvfb 在 import 时创建、无回收 | `display.py:10-21` |
| api lifespan finally（可挂 Xvfb 清理） | `api.py:65` |
