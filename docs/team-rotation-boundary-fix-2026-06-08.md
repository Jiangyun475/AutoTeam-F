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
