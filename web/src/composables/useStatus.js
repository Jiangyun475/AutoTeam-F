// 账号状态/可用性/时间格式化工具集中位
// spec 引用:account-state-machine.md v2.0 §2.1 / §3.2 / §5.3
//
// round-12 F3 — 在保留全部 utility 函数 (STATUS_LABELS / computeUsability /
// 时间格式化…) 的前提下,新增 useStatusQuery / useStatusInvalidator 两个
// vue-query 入口,让组件可以渐进迁移而不破坏 21 套现有 import。

import { onUnmounted } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { api } from '../api.js'
import { useRotateStream } from './useRotateStream.js'

// 全局 query key 常量,跨组件共享避免 typo
export const STATUS_QUERY_KEY = ['status']

// useStatusQuery — Dashboard / TaskPanel 使用的复合状态拉取。
//
// 行为:
//   - refetchInterval: 30_000 (与 main.js 默认 staleTime 对齐)
//   - refetchOnWindowFocus: true (vue-query 全局已开,这里仅显式声明可读性)
//   - keepPreviousData: 切页面时保留旧数据,UI 不闪 skeleton
//
// 返回 vue-query 标准 ref ({ data, isFetching, isError, refetch, ... }) +
// 一个 invalidate() 简捷方法,SSE 事件触发后调它即可。
export function useStatusQuery(options = {}) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: () => api.getStatus(),
    refetchInterval: options.refetchInterval ?? 30_000,
    refetchOnWindowFocus: true,
    keepPreviousData: true,
    staleTime: 5_000, // 5s 内重复 mount 不重拉
    ...options,
  })

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: STATUS_QUERY_KEY })
  }

  return { ...query, invalidate, queryClient }
}

// useStatusInvalidator — 把 rotate SSE 事件桥接到 vue-query。
//
// 在任意组件 setup() 里调用一次,SSE 流上每来一条 transition 就 invalidate
// ['status'],vue-query 自动重拉(refetchOnMount + dedup)。
//
// 返回 useRotateStream 的所有 ref,组件可同时拿来渲染"实时进度"面板。
export function useStatusInvalidator(options = {}) {
  const queryClient = useQueryClient()
  const stream = options.stream || useRotateStream(options.streamOptions || {})
  const offTransition = stream.onTransition(() => {
    // invalidateQueries 自身是 promise + dedup,不需要 debounce;
    // 若 30s polling 与 SSE 重叠,vue-query 会合并成单次网络请求。
    queryClient.invalidateQueries({ queryKey: STATUS_QUERY_KEY })
  })
  onUnmounted(() => offTransition && offTransition())
  return stream
}

export const STATUS_LABELS = {
  active: 'Active',
  exhausted: 'Used up',
  standby: 'Standby',
  pending: 'Pending',
  personal: 'Personal',
  disabled: 'Disabled',
  auth_invalid: '认证失效',
  orphan: '孤立',
  degraded_grace: 'Grace',
}

// F2 — 状态视觉:每个状态唯一的渐变 + dot color + label
// 维度:base(背景), text(文字), border(边框), dot(脉冲点), gradient(badge 背景渐变)
export const STATUS_STYLES = {
  active: {
    base: 'bg-emerald-500/[0.08]',
    text: 'text-emerald-300',
    border: 'border-emerald-400/30',
    dot: 'bg-emerald-400',
    pulse: true,
    gradient: 'from-emerald-400/20 via-emerald-500/10 to-teal-500/15',
  },
  personal: {
    base: 'bg-sky-500/[0.08]',
    text: 'text-sky-300',
    border: 'border-sky-400/30',
    dot: 'bg-sky-400',
    pulse: false,
    gradient: 'from-sky-400/20 via-blue-500/10 to-indigo-500/15',
  },
  standby: {
    base: 'bg-amber-500/[0.08]',
    text: 'text-amber-300',
    border: 'border-amber-400/30',
    dot: 'bg-amber-400',
    pulse: false,
    gradient: 'from-amber-400/15 via-yellow-500/10 to-amber-600/15',
  },
  disabled: {
    base: 'bg-stone-500/[0.08]',
    text: 'text-stone-700',
    border: 'border-stone-300',
    dot: 'bg-stone-500',
    pulse: false,
    gradient: 'from-stone-200/70 via-stone-100/50 to-slate-100/60',
  },
  // GRACE — 橙→红渐变,grace_until 越接近 0 越红
  degraded_grace: {
    base: 'bg-orange-500/[0.10]',
    text: 'text-orange-200',
    border: 'border-orange-400/40',
    dot: 'bg-orange-400',
    pulse: true,
    gradient: 'from-amber-500/25 via-orange-500/20 to-rose-500/25',
  },
  auth_invalid: {
    base: 'bg-rose-500/[0.08]',
    text: 'text-rose-300',
    border: 'border-rose-400/30',
    dot: 'bg-rose-400',
    pulse: false,
    gradient: 'from-rose-500/15 via-red-500/10 to-rose-600/15',
  },
  pending: {
    base: 'bg-slate-500/[0.10]',
    text: 'text-slate-300',
    border: 'border-slate-400/25',
    dot: 'bg-slate-400',
    pulse: true,
    gradient: 'from-slate-500/15 via-zinc-500/10 to-slate-600/15',
  },
  exhausted: {
    base: 'bg-red-700/[0.12]',
    text: 'text-red-300',
    border: 'border-red-500/30',
    dot: 'bg-red-500',
    pulse: false,
    gradient: 'from-red-700/20 via-rose-700/15 to-red-800/20',
  },
  orphan: {
    base: 'bg-yellow-600/[0.10]',
    text: 'text-yellow-200',
    border: 'border-yellow-500/30',
    dot: 'bg-yellow-400',
    pulse: false,
    gradient: 'from-yellow-500/15 via-amber-500/10 to-yellow-600/15',
  },
}

export function statusLabel(s) {
  return STATUS_LABELS[s] || s || '-'
}
export function statusStyle(s) {
  return STATUS_STYLES[s] || STATUS_STYLES.pending
}

// F1 — "实际可用性" 派生四档状态
// 输入:account 对象(含 status, last_quota, grace_until 等)
// 输出:{ kind: 'usable'|'grace'|'standby'|'unusable', label, hint, tone }
export function computeUsability(acc) {
  if (!acc) return { kind: 'unknown', label: '—', hint: '', tone: 'neutral' }
  const s = acc.status
  const liveQuotaStatus = acc.live_quota_status
  if (liveQuotaStatus === 'auth_error') {
    return { kind: 'unusable', label: '认证异常', hint: '实时 quota 探测返回 401/403', tone: 'rose' }
  }
  if (liveQuotaStatus === 'network_error') {
    return { kind: 'unknown', label: '网络未知', hint: '实时 quota 探测失败，保留本地状态', tone: 'neutral' }
  }
  if (s === 'disabled') {
    if (acc.disabled_reason === 'phone_required') {
      return { kind: 'unusable', label: '已作废', hint: '触发手机号验证', tone: 'rose' }
    }
    return { kind: 'standby', label: '禁用', hint: '已从自动化流程排除', tone: 'neutral' }
  }
  const q = acc.last_quota || {}
  const noQuota = q.primary_total === 0 || q.no_quota === true
  const hasError = q.error || q.status === 'auth_error'

  if (s === 'auth_invalid' || s === 'orphan') {
    return { kind: 'unusable', label: '不可用', hint: '认证失效 / 席位异常', tone: 'rose' }
  }
  if (hasError) {
    return { kind: 'unusable', label: '不可用', hint: '配额检测错误', tone: 'rose' }
  }
  if (s === 'degraded_grace') {
    const remain = formatGraceRemain(acc.grace_until)
    return {
      kind: 'grace',
      label: 'Grace',
      hint: remain ? `剩 ${remain}` : '母号已 cancel,grace 期内仍可用',
      tone: 'orange',
      remainMs: graceRemainMs(acc.grace_until),
    }
  }
  if (s === 'standby' || s === 'pending') {
    return { kind: 'standby', label: '待机', hint: s === 'pending' ? '注册中' : '等待 quota 恢复', tone: 'amber' }
  }
  if (s === 'exhausted') {
    return { kind: 'standby', label: '待机', hint: '已耗尽,等 5h 重置', tone: 'amber' }
  }
  if (s === 'personal') {
    if (noQuota) return { kind: 'unusable', label: '不可用', hint: '无配额', tone: 'rose' }
    return { kind: 'usable', label: '可用', hint: 'Personal free', tone: 'emerald' }
  }
  if (s === 'active') {
    if (noQuota) return { kind: 'unusable', label: '不可用', hint: '无配额', tone: 'rose' }
    const primaryRemain = 100 - (q.primary_pct || 0)
    if (primaryRemain <= 0) return { kind: 'standby', label: '待机', hint: '5h 配额耗尽', tone: 'amber' }
    return { kind: 'usable', label: '可用', hint: `5h 剩余 ${primaryRemain}%`, tone: 'emerald' }
  }
  return { kind: 'unknown', label: '—', hint: '', tone: 'neutral' }
}

// ── 时间工具 ──
export function nowSec() {
  return Date.now() / 1000
}

export function graceRemainMs(graceUntil) {
  if (!graceUntil || typeof graceUntil !== 'number') return null
  return Math.max(0, graceUntil * 1000 - Date.now())
}

export function formatGraceRemain(graceUntil) {
  const ms = graceRemainMs(graceUntil)
  if (ms === null) return ''
  if (ms <= 0) return '已到期'
  const totalMin = Math.floor(ms / 60000)
  const days = Math.floor(totalMin / (60 * 24))
  const hours = Math.floor((totalMin % (60 * 24)) / 60)
  const minutes = totalMin % 60
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

export function formatGraceDate(graceUntil) {
  if (!graceUntil) return ''
  const d = new Date(graceUntil * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// GRACE 倒计时颜色:< 24h 红 / < 7d 橙 / 其它 amber
export function graceUrgencyClass(graceUntil) {
  const ms = graceRemainMs(graceUntil)
  if (ms === null) return 'text-amber-300'
  const days = ms / (1000 * 60 * 60 * 24)
  if (days < 1) return 'text-rose-300'
  if (days < 7) return 'text-orange-300'
  return 'text-amber-300'
}

// ── master health reason → severity 映射 (Round 11) ──
// spec:master-subscription-health.md v1.2 §14
//
// 输入:
//   reason         — master_health endpoint 返回的 reason 字段
//   masterHealth   — 完整 masterHealth 对象 (可选,提供 healthy / evidence.grace_until)
//   minGraceUntil  — 任意子号最早的 grace_until epoch (Round 9 派生,旧路径兜底)
// 输出:
//   'hidden'   — 不渲染 banner (active / subscription_grace healthy 时通常 hidden 或自定义)
//   'warning'  — 黄色提示 (subscription_grace 母号 healthy=True 但 grace 期内,见 M-I14)
//   'info'     — 灰色 (network_error / stale)
//   'critical' — 红色 (subscription_cancelled / workspace_missing / role_not_owner / auth_invalid)
//
// Round 11 关键改动:
//   - 新增 reason='subscription_grace' → 'warning' (healthy=True 但仍提示倒计时)
//   - 旧路径 (subscription_cancelled + minGraceUntil 仍未过期) 继续兼容显示 warning
export function masterHealthSeverity(masterHealth, minGraceUntil = null) {
  const m = masterHealth
  if (!m) return 'hidden'
  // Round 11:subscription_grace 是 healthy=True 的新状态,但 banner 仍显 warning + 倒计时
  if (m.reason === 'subscription_grace') return 'warning'
  // active / 其它 healthy 状态隐藏 banner
  if (m.healthy === true || m.reason === 'active') return 'hidden'
  const r = m.reason
  if (r === 'subscription_cancelled') {
    // 旧兼容路径:cancelled + 子号有 grace_until 仍显 warning
    const evGrace = m.evidence?.grace_until
    if (evGrace && evGrace * 1000 > Date.now()) return 'warning'
    if (minGraceUntil && minGraceUntil * 1000 > Date.now()) return 'warning'
    return 'critical'
  }
  if (r === 'workspace_missing' || r === 'role_not_owner' || r === 'auth_invalid') {
    return 'critical'
  }
  if (r === 'network_error') return 'info'
  return 'critical'
}

// 配额数值与颜色 (Dashboard 复用)
export function quotaRemainingPct(qi, type = 'primary') {
  if (!qi) return null
  const pct = type === 'primary' ? qi.primary_pct : qi.weekly_pct
  if (typeof pct !== 'number' || Number.isNaN(pct)) return null
  return Math.max(0, Math.min(100, 100 - pct))
}

export function quotaPctText(qi, type = 'primary') {
  if (!qi) return '-'
  if (type === 'primary' && (qi.primary_total === 0 || qi.no_quota === true)) return '无配额'
  const v = quotaRemainingPct(qi, type)
  return v === null ? '-' : `${v}%`
}

export function quotaPctColor(remain) {
  if (remain === null || remain === undefined) return 'text-gray-500'
  if (remain > 30) return 'text-emerald-300'
  if (remain > 0) return 'text-amber-300'
  return 'text-rose-300'
}

export function formatQuotaReset(qi, type = 'primary') {
  if (!qi) return '-'
  const ts = type === 'primary' ? qi.primary_resets_at : qi.weekly_resets_at
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
