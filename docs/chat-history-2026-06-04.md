# 2026-06-04 会话与排障纪要

说明: 本文件根据当前会话上下文、工具执行日志和项目状态整理,用于后续审计和接续工作。它不是逐字聊天转录,但覆盖本轮关键问题、决策、修复和验证结果。

## 初始问题

用户要求阅读 `/home/data/jiangyun/2_Auto_team/a.md`,理解两个项目,检查问题并给出解决方案。后续目标逐渐明确为:

- 配置临时邮箱后端。
- 让系统能自动接收 OpenAI/ChatGPT 邮箱验证码。
- 管理固定账号池。
- 在 Team 只有 2 个子号席位的情况下维护 active 子号。
- 当某个子号额度不足时,移出该号并拉入另一个可用号。
- 避免母号参与使用。

## 邮箱后端配置

用户在 UI 中看到:

- 后端类型: `cf_temp_email`
- `CLOUDMAIL_BASE_URL` 要填写 Worker 根地址。
- 测试连接曾出现 `TIMEOUT: GET https://mail-api.yunfei.life/admin/address 超时(>5s)`。
- 也询问过 API key 是否变化、当前是什么。

处理结论:

- `MAIL_PROVIDER=cf_temp_email`
- `CLOUDMAIL_BASE_URL=https://mail-api.yunfei.life`
- `CLOUDMAIL_DOMAIN=@yunfei.life`
- `CLOUDMAIL_BASE_URL` 不追加 `/api`。
- API key 是 `.env` 中的固定值,不应频繁变化。

后续已增加:

- 邮箱后端设置说明。
- 邮件收件箱 UI/API,用于用户自己查询验证码。

## 管理员登录与 no_valid_organizations

用户遇到:

```text
糟糕，出错了！
你没有任何有效组织。
错误代码：no_valid_organizations
```

排查方向:

- 管理员登录态和 workspace 选择状态不稳定。
- 需要确保母号 session 能读取 Team workspace。
- 用户随后反馈“登录成功了”。

## 席位数量

用户明确:

```text
自动巡检目标 Codex 子号席位数只用两个子号,母号不用。所以应该是 2。
```

项目配置解释:

- 代码的 `target_seats=3` 表示总席位数: 母号 + 2 个子号。
- 前端“目标席位”显示为 3 是符合当前代码语义的。
- active 池目标实际由 `_pool_active_target(3)` 得到 2。

## 0.0.0.0 与访问地址

用户询问:

```text
0.0.0.0:8787 这样访问可以吗？还是必须得此服务器ip？
```

结论:

- `0.0.0.0` 只用于服务监听。
- 浏览器应访问 `http://服务器IP:8787` 或服务器本机的 `http://127.0.0.1:8787`。

## 账号池导入与验证码

用户已有账号:

- `yun3`
- `yun5`
- `yun6`
- `yun7`
- `yun8`
- `yun9`
- 后续新增 `yun10`
- 后续新增 `yun11`

用户曾反馈登录 `yun6` 需要验证码:

```text
刚刚向 yun6@yunfei.life 发送的验证码
```

为解决“以后查邮件麻烦”:

- 增加了邮箱收件箱页面。
- 增加了 `/api/mail/inbox`。

## 固定池模式

用户不希望系统创建奇怪的新随机账号,而是只使用固定池。

已配置:

```env
ROTATE_ALLOW_NEW_ACCOUNTS=false
```

效果:

- 无可复用 standby 时停止补位。
- 不再自动创建随机新账号。

## 自动替换想法

用户描述的目标:

1. 当前 Team 有两个子号位置。
2. 一个子号 5h 额度用完时,立即使用另一个子号。
3. 同时把用完的子号踢出 Team。
4. 从池子拉一个新/可用子号进 Team。
5. 新子号接受邀请并登录 Team workspace。
6. 当另一个 active 子号也用完时再切换。

系统对应实现:

- active 账号低于阈值时触发 `cmd_replace` / `_replace_single`。
- 被替换账号写入 `quota_exhausted_at` 和 `quota_resets_at`。
- standby 账号按是否恢复和时间排序。
- 新拉入账号完成 Team OAuth 后才标 active。

当前阈值:

```env
AUTO_CHECK_THRESHOLD=11
```

代码判断是 `remaining < threshold`,所以剩余 10% 会触发。

## yun8 -> yun5 实测流程

用户要求:

```text
现在可以把 yun8 踢出，拉 yun5 进来测试整个流程
```

执行结果:

1. 远端 Team 初始成员为母号 + `nine` + `eight`。
2. 成功移出 `yun8`。由于 OpenAI 隐藏邮箱,按显示名 `eight` 匹配。
3. 给 `yun5@yunfei.life` 发送 Team invite。
4. 从 cf_temp_email 取到邀请链接。
5. 打开邀请链接,自动收 OTP 并加入 Team。
6. 首次 Codex OAuth 卡在 consent 页面。
7. 修复 consent 点击和账号选择后再次执行。
8. 成功拿到 `plan=team`。
9. 保存:

```text
auths/codex-yun5@yunfei.life-team-2b3a2716.json
```

10. quota 探测返回 `ok`,5h 使用约 1%。

最终远端 Team:

- 母号
- `nine` 对应 `yun9`
- `yun` 对应 `yun5`

本地 active:

- `yun5@yunfei.life`
- `yun9@yunfei.life`

## 修复点汇总

### 1. OAuth consent 页面

问题:

- 页面已经在 `Sign in to Codex with ChatGPT`,底部有黑色 `Continue`。
- 旧选择器只认 `继续`、`Continue`、`Allow`,并且在页面刚跳转时会提前 break。

修复:

- 新增 `_click_oauth_consent_continue`。
- 账号选择页点击后强制进入下一轮等待页面稳定。
- 同一逻辑应用到 stage2 fallback。

### 2. 旧号复用缺少 invite/accept

问题:

- `reinvite_account` 原本只做 Codex OAuth。
- 如果账号还没被母号重新拉入 Team,OAuth 会拿到 free plan 或失败。

修复:

- 在 `reinvite_account` 中补:
  - `invite_to_team`
  - `_wait_for_invite_link`
  - `_accept_existing_account_team_invite`
  - 然后再做 Team OAuth 和 quota 验证。

### 3. 空密码 OTP 登录误判失败

问题:

- 旧号没有密码时,接受邀请流程通过 OTP 成功,但返回空字符串。
- 调用方用 `if not accepted_password` 误判失败。

修复:

- 改为只把 `None` 当失败。
- 空字符串代表“无密码/OTP 成功”。

### 4. 隐藏邮箱成员匹配

问题:

- OpenAI Team 成员接口对 yun8/yun9/yun5 返回 `email=null`。
- 只按 email 查找会认为成员不存在,或无法踢出。

修复:

- 对当前邮箱域名下的账号增加显示名匹配。
- `yun8` 可匹配 `eight`。
- `yun9` 可匹配 `nine`。
- `yun5` 可匹配 `yun`。

风险:

- `yun3`、`yun5` 可能都匹配 `yun`,需要后续记录 `user_id` 来彻底解决。

### 5. 账号池排序和刷新时间

问题:

- UI 之前主要按 `accounts.json` 原始顺序展示。
- 虽然记录了 `quota_resets_at` 和 `last_quota.primary_resets_at`,但没有明确展示“下次可用”。

修复:

- 后端派生 `next_usable_at`、`next_usable_reason`、`pool_sort_bucket`。
- 前端新增“下次可用”列。
- 排序为: 使用中 -> 可复用 -> 等刷新 -> 异常 -> 禁用。

### 6. 验证码提取

问题:

- 原提取规则可能从邮件中拿到第一个 6 位数字,不一定是验证码。

修复:

- 优先上下文匹配。
- 多个无上下文 6 位数字时返回 `None`。
- 新增单测覆盖多个数字、后置上下文、HTML 色值等情况。

## 当前服务状态

API 服务通过 tmux 运行:

```bash
tmux ls
# autoteam8787
```

启动命令:

```bash
cd /home/data/jiangyun/2_Auto_team/AutoTeam-F
PYTHONPATH=src .venv/bin/python -m autoteam api --host 0.0.0.0 --port 8787
```

当前浏览器入口:

```text
http://服务器IP:8787
```

## 当前注意事项

- `yun6` 当前没有 5h 耗尽锁,但有历史 `login_failed`/`auth_retry_after` 字段,后续可重试。
- `yun10`、`yun11` 已加入本地池,均为 standby/free auth,没有耗尽锁。
- `yun7` 有 `non_team_plan` 历史失败,需要再次拉入 Team 后才能刷新为 team。
- CPA 同步目标有 503,当前不影响本地池,但会影响远端同步。
- 远端成员隐藏邮箱时仍需谨慎,避免显示名歧义。
