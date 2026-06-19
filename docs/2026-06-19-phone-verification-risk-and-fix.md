# 2026-06-19 手机号二次验证风控分析与修复记录

## 背景

`yun12@yunfei.life` 在前端显示为“不可用 / 认证失效 / 席位异常”。检查 `data/api.log` 后确认，它不是普通认证失效，而是在 Codex OAuth 登录过程中触发了 OpenAI/Auth0 的手机号验证页：

- `2026-06-19 17:41:13`：进入 `https://auth.openai.com/phone-verification`
- `2026-06-19 17:42:29`：再次进入 `phone-verification`
- 后续第三次流程卡在 email verification，最终被记录成 `auth_code_missing`

因此真实根因被后续错误覆盖，账号没有被立即作废。

## 风控原因判断

手机号验证是 OpenAI/Auth0 登录侧返回的风险控制页面，不是 CPA 主动触发。CPA 只消费已经生成好的 Codex auth 文件；它不会参与 AutoTeam-F 浏览器 OAuth 登录过程。

公开资料里，Auth0 Adaptive MFA 会在每次登录时计算风险，信号包括：

- 新设备：依赖 user agent 和浏览器 cookie 判断设备是否最近 30 天使用过。
- 异常地理位置：比较上次登录地点和当前登录地点，判断是否存在 impossible travel。
- 不可信 IP：Auth0 会基于流量情报判断 IP 是否与可疑行为相关。

Auth0 Attack Protection 还会使用：

- bot detection：基于 IP reputation 对可疑自动化登录触发额外挑战。
- suspicious IP throttling：同一 IP 在短时间内对大量账号尝试登录会被识别。
- brute-force protection：同一 IP 对某个账号的高频失败尝试会被识别。

结合本项目日志和运行方式，最可能的风险组合是：

- 同一批 `yunfei.life` 子号在短时间内反复加入 Team、踢出 Team、重新 OAuth。
- 自动化浏览器行为模式高度一致。
- 登录出口走固定代理或隧道，多个账号共享相同 IP reputation。
- 账号在不同阶段出现过 `free/team plan drift`、邮箱验证卡住、auth 修复重试等异常。
- OpenAI 侧可能已经把部分账号标成高风险，需要手机验证才能继续。

结论：手机号验证本质是账号级风险状态。我们不绕过、不重试、不继续使用该账号。

## 本次修复

### 1. `RegisterBlocked(is_phone=True)` 不再被吞掉

原来 `_login_codex_with_result()` 只把普通异常包成 `exception`，`RegisterBlocked(is_phone=True)` 在某些 auth repair 路径里会被外层重试流程覆盖，最后落成 `auth_code_missing`。

现在：

- 捕获 `RegisterBlocked`。
- `is_phone=True` 直接映射为 `error_type="add_phone"`。
- `retryable=False`。
- 同一轮不再重试。

### 2. `add_phone` 改成账号级终态

原来 `add_phone` 受 `AUTO_CHECK_RETRY_ADD_PHONE` 软重试配置影响，可能先写 `retry_after`，没有立即释放/禁用。

现在：

- 首次命中 `add_phone` 就暂停认证修复。
- 尝试释放 Team 席位。
- 标记账号禁用：
  - `disabled=true`
  - `disabled_reason=phone_required`
  - `reuse_disabled=true`
  - `retired_reason=phone_required`
  - `auth_last_error=add_phone`

这条规则只作用于子号。母号不参与 CPA/Codex 发布，也不被该路径自动禁用。

### 3. 前端显示修正

前端现在会把 `disabled_reason=phone_required` 显示成：

- 可用性：`已作废`
- 提示：`触发手机号验证`
- 池子原因：`手机号验证`

不再混同为普通“认证失效 / 席位异常”。

### 4. OAuth 已拿到 auth code 后提前跳出 consent 循环

另有一个运行态问题：页面已经捕获到 `auth_code` 后，继续截图或检查 choose-account/workspace 页面可能导致 Playwright/CDP 卡死。现在在捕获 `auth_code` 后立即跳出循环，直接进行 token exchange，减少 OAuth 修复流程卡住的概率。

## 当前 `yun12` 处理结果

已按新策略手动修正：

```text
email: yun12@yunfei.life
status: disabled
raw_status: auth_invalid
disabled_reason: phone_required
reuse_disabled: true
retired_reason: phone_required
auth_last_error: add_phone
auth_retry_paused: true
```

备份文件：

```text
accounts.json.bak-add-phone-yun12-20260619
```

## CPA / CLIProxyAPI 升级评估

当前本地 CPA 二进制：

```text
CLIProxyAPI Version: 7.1.44
Commit: fd309448
BuiltAt: 2026-06-03T17:06:35Z
```

上游已到 `v7.2.20`。上游新增的相关能力主要是：

- auth cooldown 持久化。
- transient upstream error cooldown 配置。
- transport error 分类为 retryable。
- credential error 映射优化。
- management reload / pluginhost / translator 等大量通用更新。

这些更新能改善 CPA 对坏 auth、临时 502/503、transport error 的冷却和重试行为，但不能避免 OpenAI 登录时要求手机号验证，因为手机号验证发生在 AutoTeam-F 浏览器 OAuth 登录阶段。

本地 CLIProxyAPI 工作区当前没有 tracked 修改，只有运行配置和本地脚本未跟踪：

- `config.local.yaml`
- `auths.disabled/`
- `docker-compose.autoteam.yml`
- `start-cpa-local.sh`

因此从 Git 层面看，升级 CPA 大概率是 fast-forward，不会和 tracked 文件冲突。但运行层面需要单独验证：

- 新版本配置兼容性。
- `config.local.yaml` 是否需要新增 `save-cooldown-status` / `transient-error-cooldown-seconds`。
- CPA auth 文件热加载和 AutoTeam-F 上传/删除 auth 文件是否兼容。
- `/v1/models` 和 `/v1/responses` 是否保持当前 Codex CLI 可用。

建议 CPA 升级单独开维护窗口，不和 AutoTeam-F 轮转修复混在一个提交里。

## 验证

已执行：

```text
python -m py_compile src/autoteam/manager.py
pytest tests/unit/test_round12_s3_cherry_pick.py -q
pytest tests/unit/test_api_status.py tests/unit/test_round11_realtime_probe.py -q
npm run build
```

运行态验证：

```text
active_count: 2
active: yun10@yunfei.life, yunyunya@yunfei.life
auth_invalid: 0
disabled: 7
CPA available: 2/2
```

## 参考资料

- Auth0 Adaptive MFA: https://auth0.com/docs/secure/multi-factor-authentication/adaptive-mfa
- Auth0 Attack Protection: https://auth0.com/docs/secure/attack-protection
- OpenAI phone verification limits: https://help.openai.com/en/articles/8983031-chatgpt-supported-countries
