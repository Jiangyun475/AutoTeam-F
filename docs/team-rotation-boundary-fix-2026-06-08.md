# Team 轮转边界修复记录

记录日期: 2026-06-08

## 背景

AutoTeam-F 用母号管理 ChatGPT Team 工作空间,用子号提供 Codex/CPA 可用凭证。当前目标模型是:

- 母号只负责 Team 管理操作,不进入 CPA,不作为 Codex 可用账号。
- Team 总占位为 3: 1 个 owner 母号 + 2 个 standard-user 子号。
- CPA 只同步 2 个可用子号 auth 文件。
- 自动巡检在子号额度低于阈值时,先移出旧子号,再补入新子号或可复用子号。

这套流程的核心风险是 Team 席位只有两个子号位置。如果旧子号没有真正移出,或者 pending invite 没有取消干净,再继续邀请新子号会造成占位混乱。

## 问题现象

现场出现过以下异常:

- 前端显示账号池数量和真实 Team 不一致。
- 点击取消邀请后,前端仍短时间看到 invite 存在。
- 出现未确认旧子号移出,就继续邀请新子号的风险。
- OpenAI Team 成员接口返回的两个子号 email 为空,只能看到 `user_id` 和角色。

## 根因

### 1. Team 成员接口隐藏子号 email

ChatGPT Team API 的 `/backend-api/accounts/{account_id}/users` 对部分子号返回 `email=null`。旧逻辑按 email 找成员,会出现两类错误:

- 找不到目标子号,误判为已经不在 Team。
- 如果使用显示名兜底,可能误踢其他账号。

显示名不能作为依据。多个账号可能都叫 `yun`,因此 name fallback 是不安全的。

### 2. pending invite 也占 Team 席位

Team 的真实容量不能只看成员数。pending invite 同样占 seat。安全口径必须是:

```text
occupancy = members + pending_invites
```

如果只看 `members`,会把 `members=2, invites=1` 错判成还有空位,从而继续邀请。

### 3. 删除/取消操作存在远端延迟

OpenAI API 对删除成员和取消 invite 可能存在短暂延迟。仅凭 DELETE 返回 200/204 不能马上认为席位已经释放。必须重新读取远端状态确认:

- 成员移出后,`members + invites < target` 才能继续补位。
- invite 取消后,远端 invite 列表确认不存在该 email 才算成功。

## 设计边界

### 母号边界

母号只允许做 Team 管理:

- 查询成员和邀请。
- 邀请子号。
- 移出子号。
- 取消 pending invite。

母号不允许进入 CPA auth 目录,不允许作为 Codex 供应账号。

### 子号边界

CPA 只接收处于工作态的 Team 子号 auth 文件。目标数量为 2。

### 匹配边界

移出 Team 成员时只允许两种匹配:

1. 远端明确返回 email,且 email 与目标账号一致。
2. 远端隐藏 email,但本地 team auth 文件的 JWT claim `chatgpt_user_id` 与远端 `user_id` 一致。

禁止使用:

- display name
- name
- 邮箱前缀猜测
- 排序位置猜测

如果无法精确匹配,返回 `hidden_unmatched`,并停止后续邀请。

### 容量边界

所有邀请/创建新账号前必须执行远端容量检查:

```text
members + pending_invites < TEAM_SEATS_MAX
```

如果无法查询 Team API,或者查询结果显示满员,流程必须 fail closed,不能继续邀请。

### 删除确认边界

取消 pending invite 后必须重新读取 invite 列表。如果超时后 invite 仍存在,返回失败,不继续补位。

成员移出后必须等待远端容量释放。如果容量未释放,不继续创建或邀请新子号。

## 代码修改摘要

### `src/autoteam/manager.py`

- 新增 `get_team_occupancy_count()`,统一使用 `members + invites` 作为 Team 占位。
- `_wait_for_remote_capacity_after_removal()` 改为等待 occupancy 下降。
- `_prepare_remote_capacity_for_new_seat()` 改为 fail closed,无法确认容量时拒绝邀请。
- `invite_to_team()` 在调用 `invite_member()` 前增加最终容量守卫。
- `cmd_rotate()` 的空位计算、最终状态判断改为 occupancy 口径。
- `cmd_fill()` 的填充状态显示改为 occupancy 口径。
- `remove_from_team()` 在隐藏 email 时通过 auth JWT `chatgpt_user_id` 精确匹配远端 `user_id`。
- `cmd_cleanup()` 处理 hidden email 时使用 JWT user_id,并把 pending invite 计入占位。

### `src/autoteam/api.py`

- 手动取消 invite 时使用统一的 `delete_team_invite()`。
- DELETE 成功后重新读取 Team invites,确认 invite 消失。
- 如果 invite 仍存在,返回 409,避免 UI 显示假成功。

### `src/autoteam/codex_auth.py`

- `check_codex_quota()` 支持使用配置的 ChatGPT HTTP 代理,保证巡检和登录出口一致。
- OAuth consent 点击逻辑更稳,避免按钮文案或页面延迟导致登录卡住。

### `tests/unit/test_manager_rotate.py`

新增覆盖:

- 隐藏 email 时禁止 name fallback。
- 隐藏 email 时可通过 JWT user_id 精确移出。
- pending invite 计入容量。
- pending invite 仍占满容量时不调用 invite。

### `tests/unit/test_manager_cleanup.py`

覆盖 cleanup 在 hidden email + pending invite 场景下的处理。

### `.gitignore`

补充忽略运行态文件:

- `accounts/`
- `state_log.jsonl`
- `workspaces.json`

这些文件可能包含真实账号状态或运行记录,不能入库。

## 验证

执行过的验证:

```bash
AutoTeam-F/.venv/bin/python -m py_compile \
  AutoTeam-F/src/autoteam/manager.py \
  AutoTeam-F/src/autoteam/api.py \
  AutoTeam-F/src/autoteam/codex_auth.py
```

```bash
AutoTeam-F/.venv/bin/python -m pytest \
  AutoTeam-F/tests/unit/test_manager_rotate.py::test_remove_from_team_does_not_match_hidden_email_by_display_name \
  AutoTeam-F/tests/unit/test_manager_rotate.py::test_remove_from_team_matches_hidden_email_by_auth_user_id \
  AutoTeam-F/tests/unit/test_manager_rotate.py::test_prepare_remote_capacity_counts_pending_invites \
  AutoTeam-F/tests/unit/test_manager_rotate.py::test_invite_to_team_refuses_when_pending_invite_still_occupies_capacity \
  AutoTeam-F/tests/unit/test_manager_rotate.py::test_cmd_rotate_removes_replaceable_blocker_before_creating_replacement \
  AutoTeam-F/tests/unit/test_manager_cleanup.py \
  -q
```

结果:

```text
6 passed
```

运行态验证:

- AutoTeam-F 启动成功。
- CloudMail 配置验证成功。
- CPA 配置验证成功,检测到 2 个 auth entries。
- Team 查询结果为 `members=3`, `invites=0`。
- CPA auth 目录只有 2 个子号 auth 文件。

## 后续维护原则

以后改轮转逻辑时必须遵守:

- 不把母号同步到 CPA。
- 不用 name/display name 匹配 Team 子号。
- 不只看 members,必须看 members + pending invites。
- 邀请前必须确认远端有空位。
- 删除/取消后必须重新读取远端状态确认。
- 无法确认时停止流程,不要继续拉号。

## 2026-06-09 追加修复: pending invite 占位导致 active 不足

### 新现象

现场再次出现账号池和 Team 不一致:

- 本地一度显示 9 个账号全是 `standby`。
- 同步修复后只恢复出 1 个 `active`。
- 远端 Team 实际为 `owner + 1 个子号成员 + 1 个 pending invite`。
- pending invite 对应本地账号仍是 `standby`,并带有历史失败:

```text
invite accept unconfirmed (still pending after accept)
```

这说明该账号曾经尝试接受邀请,但远端仍保持 pending invite 状态。它占住了第二个子号 seat,但本地不能把它当作可用 active。

### 新根因

之前补了“pending invite 计入容量”,但还缺少两个边界:

1. `sync_account_states()` 旧版本只按 email 判断 Team 成员。OpenAI 隐藏子号 email 后,本地无法把隐藏成员恢复为 active。
2. `reinvite_account()` 在邀请已经发出但“没有收到邀请链接/接受邀请失败”时,只把账号保持 `standby`,没有取消自己刚产生的 pending invite。
3. `cmd_rotate()` 看到 `members + invites >= target` 时容易认为 Team 已满并退出,即使本地 `active_with_auth < 2`。这会形成死锁:

```text
Team seat 被 pending invite 占满
本地可用 active 不足
fill/rotate 因为 occupancy 满而不补位
pending invite 又不会自动消失
```

### 追加代码修改

`src/autoteam/manager.py`:

- `_reconcile_team_members()` 增加 hidden email → auth JWT `chatgpt_user_id` 精确匹配。
- `sync_account_states()` 增加 hidden email → auth JWT `chatgpt_user_id` 精确匹配,并返回结构化结果。
- `_can_cancel_pending_invite()` 允许取消“本地管理但非 active”的 pending invite,即使它有 auth_file。active auth 才保护。
- 新增 `_cancel_pending_invite_for_email()`,用于按 email 取消单个 pending invite 并确认远端消失。
- `reinvite_account()` 在“邀请邮件缺失/接受邀请失败”时主动取消对应 pending invite。
- `cmd_rotate()` 增加守卫:如果 Team 占位已满但 `usable_active < target_active`,先清理 stale pending invite,再重新计算空位。

`src/autoteam/api.py`:

- `/api/sync/accounts` 不再把同步失败伪装成成功。`sync_account_states()` 返回 `ok=false` 时接口返回 502 和结构化原因。
- `/api/admin/diagnose` 对 Playwright 页面跳转导致的单个探针异常做降级,避免诊断接口整体 500。
- `/api/team/members` 对 hidden email 成员使用本地 auth JWT `chatgpt_user_id` 反查 email,前端可显示真实本地账号。
- `/api/team/members` 失败时返回结构化错误,避免前端误以为空列表。

测试新增:

- `test_sync_account_states_matches_hidden_member_by_auth_user_id`
- `test_cancel_stale_pending_invite_allows_standby_account_with_auth_file`
- `test_cmd_rotate_clears_pending_invite_when_pool_underfilled`
- `test_reinvite_account_cancels_pending_invite_when_invite_link_missing`

### 本次运行态验证

固定代理策略:

```text
PLAYWRIGHT_PROXY_URL=http://127.0.0.1:7901
CLIProxyAPI proxy-url=http://127.0.0.1:7901
```

最终状态:

- Team: `members=3`, `invites=0`。
- Team 成员: `owner + yun7 + yun5`。
- 本地账号池: `active=2`, `standby=7`。
- CPA auth 目录:只保留 `yun7` 和 `yun5` 两个子号文件。
- 自动巡检连续多轮显示 `available=2/2`,额度正常。

本次过程中 `yun9` 被检测为 5h 额度耗尽,转回 `standby`;`yun5` 成功接受邀请、完成 Codex OAuth、保存 team auth,并同步到 CPA。

## 2026-06-11 追加修复: standby 冷却误判与烧号熔断

### 新现象

现场出现两个相互关联的问题:

- 前端显示 standby 账号 `5h剩余 99% · 周剩余 63%`,但仍提示很久之后才参与复用。
- 用户短时间开启多个 Codex 任务,模型为高推理档位,并使用 priority/fast 服务层后,多个子号在几十分钟内连续进入 5h 额度耗尽状态。

这两个问题的影响不同:

- 冷却误判会让已经恢复的 standby 账号排在后面,造成“明明有号却不补”的错觉。
- 连续高强度任务会让自动巡检不断补入下一个 standby,形成“串行烧池”:一个 active 耗尽后立即换下一个,最后整个池子的 5h 窗口都被消耗。

### 根因

#### 1. 旧 `quota_resets_at` 比新 `last_quota` 优先级过高

账号被标记耗尽时会写入:

- `quota_exhausted_at`
- `quota_resets_at`

后续 standby 探测又会写入更具体的 `last_quota`:

- `primary_pct`
- `primary_resets_at`
- `weekly_pct`
- `weekly_resets_at`

旧逻辑在排序和前端展示时仍优先相信 `quota_resets_at`。如果 `quota_resets_at` 是未来时间,即使 `last_quota` 已经显示 5h 额度恢复,账号仍会被显示为“等待复用”。

正确原则:

```text
last_quota 是更具体、更接近实时的额度事实。
如果 last_quota 显示 5h 或周额度仍可用,旧 quota_resets_at 不能继续阻塞复用。
如果 last_quota 显示 5h/周额度真正耗尽,才继续等待对应 reset 时间。
```

#### 2. high reasoning + priority/fast 会快速消耗 5h 额度

现场日志显示请求具备以下特征:

- `model: gpt-5.5`
- `reasoning.effort: xhigh`
- `service_tier: priority`
- 单次请求体很大,包含大量上下文。
- 同时存在多个 Codex 任务和失败后的重试。

`fast/priority` 不是“省额度模式”,只是更快服务层。高推理档位和大上下文会显著放大单次请求消耗。多个任务并发时,CPA 的 `fill-first` 策略会倾向先用完一个 auth,再切到下一个 auth,因此很容易出现:

```text
yunA active -> 5h 耗尽
自动巡检补 yunB
yunB active -> 5h 耗尽
自动巡检补 yunC
...
```

这不是 Team 周额度一定全部用光,而是多个账号的 5h 窗口被连续打满。

### 追加代码修改

#### standby 恢复判断

`src/autoteam/accounts.py`:

- 新增 `_quota_snapshot_recovered()`。
- `get_standby_accounts()` 先根据 `last_quota` 判断是否恢复。
- 只有缺少有效 `last_quota` 时,才回退使用旧 `quota_resets_at`。
- standby 排序继续优先周剩余额度和 5h 剩余额度,但低于巡检阈值的账号会靠后。

`src/autoteam/api.py`:

- 前端状态计算新增 `primary_exhausted`、`weekly_exhausted`、`primary_low`。
- 只有真正走旧字段回退时才显示 `复用冷却 ...`。
- 当 `last_quota` 显示额度已恢复时,不再展示“很久之后才参与复用”。

`web/src/components/Dashboard.vue`:

- 增加 `5h额度偏低`、`5h额度耗尽` 展示文案。
- 缩短冷却文案,避免表格列被长文本截断。

#### 自动巡检烧号熔断

`src/autoteam/config.py` 新增配置:

```text
AUTO_CHECK_BURN_GUARD_ENABLED=true
AUTO_CHECK_BURN_GUARD_WINDOW_SECONDS=3600
AUTO_CHECK_BURN_GUARD_MAX_EXHAUSTED=4
AUTO_CHECK_BURN_GUARD_COOLDOWN_SECONDS=300
```

默认含义:

- 1 小时内如果有 4 个不同子号从 `active` 进入 `exhausted`,认为当前任务强度异常。
- 触发后 5 分钟内暂停后台自动补位、自动替换、预防性 auto-rotate。
- 触发日志会提示用户检查任务并发、reasoning effort 和 service_tier/fast 设置。
- 手动 `rotate/fill/cleanup` 不受限制。
- Team 超员 / stale invite 清理不受限制,因为清理不会消耗新号额度。

`src/autoteam/api.py` 新增:

- `_collect_recent_quota_exhaustions()`
- `_auto_check_burn_guard_status()`

熔断判断优先读取 `state_log.jsonl` 中真实的:

```text
from_state=active
to_state=exhausted
```

如果 state log 不可读,再降级使用 `accounts.json` 的 `quota_exhausted_at`。

熔断影响范围:

- 阻止 `auto-fill`。
- 阻止 `auto-replace` 继续拉入新 standby。
- 阻止 provider-auth 低水位触发的预防性 `auto-rotate`。
- 不改 CPA 配置。
- 不重启 CPA。
- 不影响人工点击轮转。

### 当前运行态验证

重启范围:

- 只重启 `autoteam8787`。
- 未重启 `cpa8317`。

验证结果:

```text
AutoTeam-F API: 正常启动
CloudMail: 验证通过
CPA: 验证通过,当前 2 个认证文件
API /api/status?fast=true: 200
本地账号池: active=2, standby=7, total=9
当前 active 子号: yun10, yun3
CPA provider auth: 2/2
```

验证命令:

```bash
AutoTeam-F/.venv/bin/python -m pytest \
  AutoTeam-F/tests/unit/test_api_status.py \
  AutoTeam-F/tests/unit/test_accounts.py \
  AutoTeam-F/tests/unit/test_manager_rotate.py \
  AutoTeam-F/tests/unit/test_round12_s6_concurrent.py \
  -q
```

结果:

```text
77 passed
```

其他验证:

```bash
AutoTeam-F/.venv/bin/python -m py_compile \
  AutoTeam-F/src/autoteam/api.py \
  AutoTeam-F/src/autoteam/config.py

npm run build
git -C AutoTeam-F diff --check
```

### 维护建议

如果需要长时间跑多个 Codex 高强度任务:

- 不建议同时跑多个 `xhigh + priority/fast`。
- 需要保池时,把任务降到普通推理档位或减少并发。
- 如果熔断触发,先检查任务强度,不要马上继续堆多个高强度任务。
- 如果确实要强行继续消耗池子,可以手动执行 rotate/fill;这属于人工决策,不会被熔断拦截。

如果确认自己的工作负载需要更保守的保护,再调整:

```text
AUTO_CHECK_BURN_GUARD_MAX_EXHAUSTED=3
AUTO_CHECK_BURN_GUARD_COOLDOWN_SECONDS=1800
```

不建议关闭:

```text
AUTO_CHECK_BURN_GUARD_ENABLED=false
```

除非已经明确知道当前任务不会触发连续 5h 额度耗尽。
