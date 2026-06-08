# AutoTeam 账号池轮转设计记录

记录时间: 2026-06-04

## 目标

在一个 ChatGPT Team 工作空间中维护固定子号池。系统只使用已登记的子号,自动记录每个账号的状态、认证文件、配额探测结果和预计刷新时间。当前配置目标是 `target_seats=3`,即母号 + 2 个子号。

## 当前账号池模型

账号状态落盘在 `accounts.json`,关键字段如下:

- `status`: 本地生命周期状态,如 `active`、`standby`、`auth_invalid`、`personal`。
- `seat_type`: 记录席位/认证来源,如 `chatgpt` 或 `codex`。
- `auth_file`: 本地 Codex 认证文件路径。
- `last_quota`: 最近一次配额探测结果,包含 `primary_pct`、`primary_resets_at`、`weekly_pct`、`weekly_resets_at`。
- `quota_exhausted_at`: 判断某号进入耗尽状态的时间。
- `quota_resets_at`: 该号预计可再次被复用的时间。
- `auth_retry_after`: OAuth/登录失败后的下次重试时间。
- `disabled`: 是否从自动化流程排除。

## 排序规则

后端现在会给每个账号派生以下字段:

- `pool_sort_bucket`: 账号池排序桶。
- `next_usable_at`: 下次可用时间;为空表示现在可用。
- `next_usable_reason`: 可用/等待原因。
- `pool_schedule`: 以上字段和配额快照的集合。

排序顺序:

1. 母号。
2. `active`: 当前正在 Team 里的子号,按 5h 重置时间和使用百分比排序。
3. `standby` 且已可复用: `quota_resets_at` 为空或已过期。
4. `personal`: 可作为个人号管理,不参与 Team 席位时排在可复用后面。
5. `standby`/`exhausted` 但仍在等待 `quota_resets_at`。
6. 等待 `auth_retry_after` 的账号。
7. `pending`、`auth_invalid`、`orphan` 等异常账号。
8. `disabled`。

前端 Dashboard 账号表会展示:

- 5h 剩余。
- 周剩余。
- 5h 重置。
- 下次可用。
- 周重置。

因此区分账号时以 `next_usable_at` 和 `next_usable_reason` 为准,不用再靠人工猜。

## 自动巡检与替换流程

当前自动巡检配置:

- `AUTO_CHECK_INTERVAL=300`: 约 5 分钟一轮。
- `AUTO_CHECK_THRESHOLD=11`: 代码判断是 `remaining < threshold`,所以剩余 10% 会触发。
- `AUTO_CHECK_MIN_LOW=1`: 任意 1 个 active 子号低于阈值即可替换。
- `target_seats=3`: 母号 + 2 个子号。
- `ROTATE_ALLOW_NEW_ACCOUNTS=false`: 固定账号池模式,无可复用旧号时不创建随机新号。

单个账号替换链路:

1. 巡检 active 账号的 `check_codex_quota`。
2. 如果 5h 剩余低于阈值,记录 `last_quota` 和预计 `quota_resets_at`。
3. 母号调用 Team API 移出该子号。
4. 等待 Team 成员容量刷新。
5. 从 `standby` 池里选择可复用账号。
6. 母号发 Team invite。
7. 邮箱后端读取邀请邮件并提取 invite link。
8. Playwright 打开 invite link,子号收 OTP,接受邀请进入工作空间。
9. 子号执行 Codex OAuth,选择 Team workspace。
10. 拿到 `plan=team` 的 token 后保存 `auth_file`。
11. 立即探测 quota,只有 quota 可用才标记为 `active`。
12. 失败时清理残留 Team 席位,避免“假 standby 占席”。

## 已修复的问题

### 1. cf_temp_email base URL

`CLOUDMAIL_BASE_URL` 必须填 Worker 根地址,不要追加 `/api`。示例:

```env
CLOUDMAIL_BASE_URL=https://mail-api.yunfei.life
```

### 2. Team 成员接口隐藏邮箱

OpenAI Team 成员接口对部分子号返回 `email=null`,只显示 `name`。这会导致同步误判 active 子号已经不在 Team。

处理方式:

- 如果远端存在匿名非 owner 子席位,不能用 `name` / display name 兜底匹配。
- 移出成员时只允许两种精确匹配:
  - 远端明确返回 email,且 email 与目标账号一致。
  - 远端隐藏 email,但本地 team auth 文件中的 JWT claim `chatgpt_user_id` 与远端 `user_id` 一致。
- 如果没有精确 `user_id` 证据,返回 `hidden_unmatched`,本轮停止移出/补位,避免误踢其他子号。

原因:

- 多个子号的显示名可能都叫 `yun`,使用 name fallback 会把错误账号踢出。
- OpenAI Team 成员接口隐藏 email 后,`user_id` 是当前唯一可靠的自动匹配依据。

### 3. 旧号复用漏掉邀请步骤

原 `reinvite_account` 只做 Codex OAuth,没有负责“母号发邀请 + 子号接受邀请”。这会导致自动流程只能在用户手动拉号后成功。

已补齐:

- `invite_to_team`
- `_wait_for_invite_link`
- `_accept_existing_account_team_invite`
- Team OAuth
- quota 验证

### 4. Codex consent 页卡住

Codex OAuth 中最后的 `Sign in to Codex with ChatGPT` 页面有 workspace 选择和 Continue 按钮。旧逻辑只认少数按钮文本,页面跳转时会提前 break。

已补齐:

- 账号选择页选中后总是进入下一轮等待页面稳定。
- 新增 `_click_oauth_consent_continue`,支持 `Continue`、`Allow`、`Authorize`、中文同义按钮和 DOM 兜底。

### 5. OTP 提取不够准确

原逻辑最后会全局取第一个 6 位数字,容易被时间、工单号、链接参数、HTML 色值干扰。

已改为:

- 优先解析 `ai_extract` 元数据。
- 优先匹配 OpenAI/ChatGPT/code/验证码上下文。
- 支持前置上下文和后置上下文,如 `837462 is your OpenAI verification code`。
- 只有邮件可见文本中存在唯一孤立 6 位数字时才兜底返回。
- 多个无上下文 6 位数字时返回 `None`,避免乱提交旧码/错码。

## 账号池快照口径

公开文档不记录真实邮箱、密码、token 或当前生产账号池。排查现场时可以用以下口径描述账号:

- `active child A`: 当前在 Team 中,team auth 已保存。
- `active child B`: 当前在 Team 中,team auth 已保存。
- `standby recovered`: 曾被移出,预计额度已恢复,可作为补位候选。
- `standby exhausted`: 曾用完额度,等待 `quota_resets_at`。
- `auth_invalid`: OAuth/token 状态异常,需要重登或人工处理。
- `orphan`: 远端/本地状态不一致,需要对账。

真实快照应只保存在本机运行态文件或私有运维记录中,不要提交到公开仓库。

## 运维入口

API 服务:

```bash
tmux new -d -s autoteam8787 "cd /home/data/jiangyun/2_Auto_team/AutoTeam-F && PYTHONPATH=src .venv/bin/python -m autoteam api --host 0.0.0.0 --port 8787"
```

浏览器访问:

```text
http://服务器IP:8787
```

不要在浏览器里使用 `0.0.0.0`。`0.0.0.0` 只用于服务监听。

API key 存在 `.env` 中,本文档不写入明文密钥。

## 后续建议

1. 为每个子号记录远端 `user_id`,减少隐藏邮箱时按显示名匹配的歧义。
2. 给 `reinvite_account` 增加专门的集成测试,覆盖 invite -> accept -> OAuth 的全链路。
3. 在 UI 增加“仅显示可复用 / 等刷新 / 异常”筛选。
4. 对 `yun7` 这类 `non_team_plan` 账号提供一键重试和失败原因详情。
5. CPA/同步目标 503 不应影响本地池轮转,但需要单独修复远端同步健康检查。
