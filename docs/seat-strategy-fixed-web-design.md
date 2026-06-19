# Seat Strategy: 固定网页版子号与双轮换模式设计

记录日期: 2026-06-09

## 目标

在不影响现有 CPA 正常使用的前提下,为 AutoTeam-F 增加可视化可切换的 Team 席位策略:

- `dual_rotate`: 现有模式。母号 + 2 个可轮换 Codex 子号。
- `fixed_web`: 新模式。母号 + 1 个固定网页版子号 + 1 个可轮换 Codex 子号。

最重要边界:

- 母号永远只做 Team 管理,不进入 CPA,不跑 Codex。
- 当前已经跑通的 `dual_rotate` 必须保持默认和可回滚。
- 新策略必须能随时切回当前状态。
- 不允许因为实验模式导致 CPA 无可用 auth。
- 不允许因为策略切换误踢固定网页版子号。

## 当前稳定状态基线

当前已验证的稳定口径:

- Team 总占位目标为 3: owner + 2 children。
- AutoTeam-F 通过 `members + pending_invites` 判断远端占位。
- CPA 只同步当前 active 子号 auth,不包含母号。
- 7901 为固定代理入口:

```text
PLAYWRIGHT_PROXY_URL=http://127.0.0.1:7901
CLIProxyAPI proxy-url=http://127.0.0.1:7901
```

任何实现都不能破坏以上基线。

## 为什么需要新策略

双轮换模式适合 Codex:

- 两个子号都可被踢拉。
- 哪个号额度低就替换。
- CPA 可以保持两个可用子号。

但双轮换不适合 ChatGPT 网页版:

- 网页版聊天历史属于具体账号,不是 Team 共享历史。
- 子号频繁被踢/拉/换后,用户无法稳定使用同一个网页版账号。
- 如果希望网页版历史稳定,必须保留一个长期不被踢的子号。

因此需要 `fixed_web`:

```text
owner: 母号,只管理 Team
web_fixed: 固定网页版子号,长期留在 Team
codex_worker: 可轮换 Codex 子号
```

## 风险分析

### 风险 1: CPA fallback 误消耗固定号

风险:

如果固定号和 worker 同时发布到 CPA,而 CPA 内部是轮询/随机/按可用 auth 选择,固定号会持续处理 Codex 请求。

错误做法:

```text
CPA auths = [codex_worker, web_fixed]
```

问题:

- `web_fixed` 不再只是 fallback,而是长期参与 Codex。
- 固定号额度会持续消耗。
- 很难判断额度到底是谁用掉的。
- 以后排查会变复杂。

解决原则:

不要依赖 CPA 内部调度策略。AutoTeam-F 必须控制 CPA 发布集。

固定模式下 CPA 发布集应为:

```text
READY:
  CPA auths = [codex_worker]

ROTATING:
  CPA auths = [web_fixed]

NEW_WORKER_READY:
  CPA auths = [new_codex_worker]
```

也就是说,固定模式任意时刻 CPA 默认只发布一个 auth。`web_fixed` 只在 worker 切换窗口临时发布。

### 风险 2: 切换 CPA 发布集导致短暂不可用

风险:

从 `[worker]` 切到 `[web_fixed]` 时,如果 CPA 不能热加载或 reload 失败,用户请求可能短暂失败。

边界:

- 不允许先踢 worker 再尝试切 CPA。
- 必须先确认 CPA 已经切到 fallback,再开始踢旧 worker。

安全顺序:

```text
1. 检测 worker 需要替换
2. 发布 web_fixed 到 CPA
3. 验证 CPA 至少有 1 个可用 auth
4. 成功后才踢旧 worker
5. 拉新 worker
6. 新 worker OAuth + quota 验证成功
7. 发布 new_worker 到 CPA
8. 确认 CPA 可用
9. 移除 web_fixed 的 CPA 发布状态
```

如果第 2/3 步失败:

```text
停止轮转,保留旧 worker,返回告警
```

### 风险 3: 固定号被误踢

风险:

旧代码把两个 active 子号视为同类资源。进入固定模式后,如果仍按 active 统一筛选,`web_fixed` 可能被:

- quota 低时踢出。
- cleanup 时删除。
- replace one 时替换。
- stale blocker 逻辑移除。

解决原则:

账号必须有角色和硬保护字段。

建议字段:

```json
{
  "seat_role": "web_fixed",
  "rotation_locked": true,
  "protect_team_seat": true,
  "codex_fallback": true,
  "codex_primary": false
}
```

所有自动踢/拉/清理路径必须检查:

```text
rotation_locked=true -> 禁止自动移出 Team
seat_role=web_fixed -> 禁止作为普通 worker 替换
```

### 风险 4: 固定模式下 Team seat 更容易卡住

固定模式只有一个可轮换 worker seat。任何 pending invite 都会直接卡死 worker 补位。

必须沿用现有边界:

```text
occupancy = members + pending_invites
```

并且在固定模式下更严格:

- pending invite 如果属于 worker 补位失败,必须自动取消。
- pending invite 如果属于 `web_fixed`,默认不能自动取消,需要人工确认。

### 风险 5: web_fixed 自己额度耗尽

固定号作为 fallback 时也会消耗 Codex 额度。

处理原则:

- 不自动踢 `web_fixed`。
- 不因为 `web_fixed` 额度低就替换它。
- 只告警:

```text
web_fixed fallback quota low
```

如果 worker 正在切换且 web_fixed 也不可用:

```text
返回 CPA 无可用 fallback,暂停踢 worker
```

### 风险 6: 模式切换造成状态混乱

从 `dual_rotate` 切到 `fixed_web` 时:

- 必须明确选择哪个子号是 `web_fixed`。
- 不能自动猜测。
- 不能用 name fallback。

从 `fixed_web` 切回 `dual_rotate` 时:

- 清除 `rotation_locked`。
- `web_fixed` 改回 `codex_worker`。
- CPA 重新同步两个 active worker。

模式切换必须有预检和回滚:

```text
切换前保存 old_strategy_snapshot
切换失败则恢复旧策略字段和旧 CPA 发布集
```

### 风险 7: 现有状态无法回溯

这是最高优先级风险。

解决原则:

- 新功能默认关闭。
- 默认策略保持 `dual_rotate`。
- 引入 feature flag:

```env
SEAT_STRATEGY=dual_rotate
```

只有用户在前端明确切换后才进入 `fixed_web`。

回滚按钮必须能做:

```text
SEAT_STRATEGY=dual_rotate
清除 web_fixed 角色锁
CPA 同步当前两个 active 子号
恢复现有双轮换行为
```

## 策略定义

### dual_rotate

含义:

```text
owner + codex_worker + codex_worker
```

验收口径:

```text
team_occupancy = 3
active_workers_with_auth = 2
cpa_published_auths = 2
pending_invites = 0
```

行为:

- 两个 worker 都可自动替换。
- CPA 发布两个 worker。
- 不保留固定网页版账号。

### fixed_web

含义:

```text
owner + web_fixed + codex_worker
```

验收口径:

```text
team_occupancy = 3
web_fixed_present = 1
worker_active_with_auth = 1
cpa_published_auths = 1 in READY
pending_invites = 0
```

行为:

- `web_fixed` 不自动踢。
- `codex_worker` 可自动替换。
- CPA READY 状态只发布 worker。
- worker 切换时 CPA 临时发布 web_fixed。

## CPA 发布状态机

固定模式下新增 CPA 发布状态:

```text
READY_WORKER
  published = [worker]

FALLBACK_ACTIVE
  published = [web_fixed]

ROTATING_WORKER
  published = [web_fixed]

PROMOTE_NEW_WORKER
  published = [new_worker]

ROLLBACK
  published = previous_good_set
```

关键要求:

- 不允许 `[worker, web_fixed]` 长期并存。
- 不允许踢 worker 前没有可用 CPA 发布集。
- CPA 发布后必须验证可用。

## 前端设计

设置页新增:

```text
席位策略
[ 双轮换子号模式 ] [ 固定网页版子号模式 ]
```

固定网页版子号模式下显示:

```text
固定网页版子号: <select active child>
允许作为 Codex fallback: [on/off]
当前 Codex worker: yunX
CPA 当前发布: worker / fixed fallback
```

账号池列表新增列:

```text
seat_role
rotation_locked
CPA 发布状态
用途
```

账号操作新增:

```text
设为固定网页版号
设为 Codex worker
解除固定
切回双轮换
```

危险操作必须二次确认:

- 解除固定。
- 切回双轮换。
- 让 fixed 进入 CPA fallback。

## 后端实现计划

### Phase 0: 只读状态与文档

目标:

- 不改运行行为。
- 只增加策略文档和只读状态输出。

验收:

- 当前 CPA 仍可用。
- 当前双轮换仍可用。

### Phase 1: 数据模型与默认兼容

新增账号字段:

```json
{
  "seat_role": "codex_worker",
  "rotation_locked": false,
  "protect_team_seat": false,
  "codex_primary": true,
  "codex_fallback": false
}
```

默认迁移:

- 没字段的旧账号按现有行为处理。
- `SEAT_STRATEGY` 默认 `dual_rotate`。
- 不改变 CPA 同步结果。

验收:

- 未开启 fixed_web 时,现有测试和运行状态不变。

### Phase 2: 轮转保护

改造:

- 所有自动移出路径跳过 `rotation_locked=true`。
- `cmd_rotate` 在 `fixed_web` 下只替换 `codex_worker`。
- `cmd_fill` 在 `fixed_web` 下只补 worker seat。
- cleanup 不自动删除 `web_fixed`。

验收:

- fixed_web 模式下无法自动踢 fixed。
- dual_rotate 模式行为不变。

### Phase 3: CPA 发布集控制

新增发布函数:

```text
publish_cpa_auth_set(mode, desired_emails)
verify_cpa_provider_available(expected_count)
rollback_cpa_auth_set(snapshot)
```

固定模式:

- READY 发布 `[worker]`。
- ROTATING 发布 `[web_fixed]`。
- 新 worker 就绪后发布 `[new_worker]`。

验收:

- fixed 不会和 worker 长期同时在 CPA。
- 发布失败不踢 worker。
- 发布成功后 CPA 可用。

### Phase 4: 前端策略切换

新增 UI:

- 策略切换按钮。
- 固定号选择。
- 当前发布状态显示。
- 回滚到双轮换按钮。

验收:

- 前端可以切到 fixed_web。
- 前端可以切回 dual_rotate。
- 切换失败时保留旧策略。

### Phase 5: 端到端验证

固定模式验证:

```text
1. 选择 yunX 为 web_fixed
2. CPA READY 只发布 worker
3. 模拟 worker exhausted
4. CPA 切到 web_fixed
5. 旧 worker 被踢
6. 新 worker 加入并 OAuth 成功
7. CPA 切到 new worker
8. web_fixed 仍在 Team,未被踢
```

双轮换回归:

```text
1. 切回 dual_rotate
2. CPA 发布两个 active worker
3. 任意一个额度低触发替换
4. Team 最终 owner + 2 workers
```

## 回滚策略

任何阶段如果出问题,必须能回滚到当前稳定状态。

最小回滚:

```text
SEAT_STRATEGY=dual_rotate
清空 WEB_FIXED_EMAIL
所有非母号 active 子号 seat_role=codex_worker
rotation_locked=false
CPA sync_to_configured_targets()
```

回滚前不删除本地 auth 文件。

回滚后验收:

```text
Team members=3
invites=0
active_with_auth=2
CPA available=2/2
母号不在 CPA
```

## 不做的事情

第一版不做:

- 不做 ChatGPT 网页 iframe 嵌入。
- 不做跨账号官方聊天历史合并。
- 不让 CPA 同时长期发布 worker + fixed。
- 不自动替换 web_fixed。
- 不把母号作为任何 fallback。

## 结论

该方案可行,但实现关键不是“多一个按钮”,而是引入明确的席位策略和 CPA 发布状态机。

最重要原则:

```text
dual_rotate 是默认稳定路径。
fixed_web 是显式启用路径。
CPA 发布集由 AutoTeam-F 控制。
web_fixed 永不自动踢。
任何失败都能回滚到 dual_rotate。
```
