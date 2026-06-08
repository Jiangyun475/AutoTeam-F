<template>
  <div v-if="status" class="space-y-6">
    <!-- F3 Master health banner -->
    <MasterHealthBanner
      :master-health="masterHealth"
      :min-grace-until="minGraceUntil"
      :loading="masterHealthBusy"
      @refresh="onRefreshMasterHealth" />

    <!-- F6 Pool health card -->
    <PoolHealthCard
      :accounts="status.accounts || []"
      :master-health="masterHealth"
      :min-grace-until="minGraceUntil" />

    <!-- 状态分布卡片(色彩区分明显) -->
    <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3">
      <div v-for="card in cards" :key="card.label"
        class="glass-soft rounded-lg p-3.5 lift-hover relative overflow-hidden">
        <div class="text-[10px] uppercase tracking-widest text-ink-500 font-semibold flex items-center gap-1.5">
          <span class="w-1 h-1 rounded-full" :style="{ background: card.dot }"></span>
          {{ card.label }}
        </div>
        <div class="text-3xl font-extrabold mt-1.5 tabular leading-none" :class="card.color">{{ card.value }}</div>
      </div>
    </div>

    <!-- 账号表格 -->
    <div class="glass rounded-lg overflow-hidden">
      <div class="px-5 py-4 border-b border-hairline flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h2 class="text-base font-bold text-ink-950 tracking-tight">账号列表</h2>
          <p class="text-[11px] text-ink-500 mt-0.5">
            {{ totalAccounts }} 个账号 · 每页 {{ ACCOUNT_PAGE_SIZE }} 条 · 排序: 使用中 → 可复用 → 等刷新 → 异常 → 禁用
          </p>
        </div>
        <div class="flex items-center gap-2">
          <AtButton v-if="selectedDisableEmails.length"
            variant="secondary" size="sm" :loading="bulkToggling" :disabled="actionDisabled"
            @click="bulkDisableSelected">
            禁用自动化 ({{ selectedDisableEmails.length }})
          </AtButton>
          <AtButton v-if="selectedEnableEmails.length"
            variant="primary" size="sm" :loading="bulkToggling" :disabled="actionDisabled"
            @click="bulkEnableSelected">
            启用自动化 ({{ selectedEnableEmails.length }})
          </AtButton>
          <AtButton v-if="selectedEmails.length"
            variant="danger" size="sm" :loading="batchDeleting" :disabled="actionDisabled"
            confirm @click="batchDelete">
            <template #icon>
              <Trash2 class="w-3.5 h-3.5" :stroke-width="2" />
            </template>
            {{ batchDeleting ? `批量删除 ${batchProgress}` : `批量删除 (${selectedEmails.length})` }}
          </AtButton>
          <AtButton v-if="selectedEmails.length" variant="ghost" size="sm" @click="clearSelection">
            取消选择
          </AtButton>
          <AtButton variant="secondary" size="sm" :loading="syncing" @click="syncAccounts">
            <template #icon>
              <RefreshCw class="w-3.5 h-3.5" :stroke-width="2" />
            </template>
            {{ syncing ? '同步中…' : '同步账号' }}
          </AtButton>
        </div>
      </div>

      <div v-if="message" class="mx-5 mt-4 px-4 py-2.5 rounded-lg text-sm border animate-rise" :class="messageClass">
        {{ message }}
      </div>
      <div v-if="!adminReady"
        class="mx-5 mt-4 px-4 py-2.5 rounded-lg text-sm border bg-amber-50 text-amber-800 border-amber-200">
        请先在「设置」页完成管理员登录后,才能操作账号。
      </div>
      <div v-if="hiddenAccountCount"
        class="mx-5 mt-4 px-4 py-2.5 rounded-lg text-sm border bg-sky-50 text-sky-800 border-sky-200">
        当前仅渲染第 {{ accountPageStart + 1 }}-{{ accountPageEnd }} 个账号，另有 {{ hiddenAccountCount }} 个账号在其它页，以控制页面资源占用。
      </div>
      <div v-if="totalAccountPages > 1"
        class="mx-5 mt-4 px-3 py-2 rounded-lg border border-hairline bg-ink-50 flex items-center justify-between gap-3 flex-wrap">
        <div class="text-xs text-ink-600">
          显示 {{ accountPageStart + 1 }}-{{ accountPageEnd }} / {{ totalAccounts }}
        </div>
        <div class="flex items-center gap-2">
          <AtButton variant="ghost" size="sm" :disabled="accountPage <= 1" @click="goAccountPage(accountPage - 1)">
            <template #icon>
              <ChevronLeft class="w-3.5 h-3.5" :stroke-width="2" />
            </template>
            上一页
          </AtButton>
          <span class="text-xs text-ink-600 tabular">第 {{ accountPage }} / {{ totalAccountPages }} 页</span>
          <AtButton variant="ghost" size="sm" :disabled="accountPage >= totalAccountPages" @click="goAccountPage(accountPage + 1)">
            <template #icon>
              <ChevronRight class="w-3.5 h-3.5" :stroke-width="2" />
            </template>
            下一页
          </AtButton>
        </div>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-ink-500 text-left border-b border-hairline text-[10px] uppercase tracking-widest">
              <th class="px-3 py-3 font-semibold w-8">
                <input
                  type="checkbox"
                  :checked="allSelectableChecked"
                  :indeterminate.prop="someSelectableChecked"
                  @change="toggleSelectAll"
                  :disabled="!selectableEmails.length"
                  class="accent-indigo-500 cursor-pointer w-3.5 h-3.5"
                  title="全选/取消全选本页账号(主号除外)" />
              </th>
              <th class="px-3 py-3 font-semibold">#</th>
              <th class="px-4 py-3 font-semibold">邮箱</th>
              <th class="px-4 py-3 font-semibold">状态</th>
              <th class="px-4 py-3 font-semibold">实际可用</th>
              <th class="px-4 py-3 font-semibold text-right">5h 剩余</th>
              <th class="px-4 py-3 font-semibold text-right">周 剩余</th>
              <th class="px-4 py-3 font-semibold">5h 重置</th>
              <th class="px-4 py-3 font-semibold">下次可用</th>
              <th class="px-4 py-3 font-semibold">周 重置</th>
              <th class="px-4 py-3 font-semibold text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(acc, i) in visibleAccounts" :key="acc.email"
              class="row-hoverable border-b border-hairline hover:bg-ink-50 group"
              :class="[isSelected(acc.email) ? 'bg-indigo-50' : '', acc.disabled ? 'opacity-75' : '']">
              <td class="px-3 py-3.5">
                <input
                  v-if="!acc.is_main_account"
                  type="checkbox"
                  :checked="isSelected(acc.email)"
                  @change="toggleSelect(acc.email)"
                  class="accent-indigo-500 cursor-pointer w-3.5 h-3.5" />
              </td>
              <td class="px-3 py-3.5 text-ink-400 font-mono text-xs">{{ String(accountPageStart + i + 1).padStart(2, '0') }}</td>
              <td class="px-4 py-3.5">
                <div class="flex items-center gap-2">
                  <span v-if="acc.is_main_account"
                    class="text-[9px] uppercase tracking-widest font-bold px-1.5 py-0.5 rounded
                           bg-indigo-50 text-indigo-700 border border-indigo-200">
                    Master
                  </span>
                  <span class="font-mono text-[12px] text-ink-900">{{ acc.email }}</span>
                </div>
              </td>
              <td class="px-4 py-3.5">
                <StatusBadge :status="acc.status" :grace-until="acc.grace_until" />
              </td>
              <td class="px-4 py-3.5">
                <UsabilityCell :account="acc" />
              </td>
              <td class="px-4 py-3.5 text-right font-mono tabular text-[12px]" :class="pctColor(quota(acc, 'primary'))">
                {{ quotaPct(acc, 'primary') }}
              </td>
              <td class="px-4 py-3.5 text-right font-mono tabular text-[12px]" :class="pctColor(quota(acc, 'weekly'))">
                {{ quotaPct(acc, 'weekly') }}
              </td>
              <td class="px-4 py-3.5 text-ink-500 font-mono text-[11px]">{{ quotaReset(acc, 'primary') }}</td>
              <td class="px-4 py-3.5">
                <div class="font-mono text-[11px] text-ink-700">{{ nextUsableText(acc) }}</div>
                <div class="text-[10px] text-ink-400 mt-0.5">{{ nextUsableReason(acc) }}</div>
              </td>
              <td class="px-4 py-3.5 text-ink-500 font-mono text-[11px]">{{ quotaReset(acc, 'weekly') }}</td>
              <td class="px-4 py-3.5 text-right">
                <div class="flex items-center justify-end gap-1.5 flex-wrap">
                  <span v-if="acc.status === 'personal' && !acc.auth_file"
                    class="inline-block px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wide
                           bg-amber-50 text-amber-800 border border-amber-200"
                    title="未拿到 Codex auth_file,请点击补登录">
                    缺认证
                  </span>
                  <AtButton v-if="canLogin(acc)"
                    variant="primary" size="sm"
                    :loading="actionEmail === acc.email && actionType === 'login'"
                    :disabled="actionDisabled || (actionEmail === acc.email && actionType !== 'login')"
                    @click="loginAccount(acc.email)">
                    {{ loginLabel(acc) }}
                  </AtButton>
                  <!-- Round 11:子号"立即探活"按钮(spec realtime-probe.md v1.0 §4) -->
                  <AtButton v-if="canProbe(acc)"
                    variant="secondary" size="sm"
                    :loading="actionEmail === acc.email && actionType === 'probe'"
                    :disabled="actionDisabled || (actionEmail === acc.email && actionType !== 'probe')"
                    @click="probeAccount(acc.email)"
                    title="立即调 cheap_codex_smoke + check_codex_quota,绕过 30min 节流">
                    立即探活
                  </AtButton>
                  <AtButton v-if="!acc.is_main_account && !acc.disabled && acc.raw_status === 'active'"
                    variant="secondary" size="sm"
                    :loading="actionEmail === acc.email && actionType === 'kick'"
                    :disabled="actionDisabled || (actionEmail === acc.email && actionType !== 'kick')"
                    @click="kickAccount(acc.email)">
                    移出
                  </AtButton>
                  <AtButton v-if="acc.raw_status === 'active' || acc.raw_status === 'personal' || acc.is_main_account"
                    variant="ghost" size="sm"
                    :disabled="actionEmail === acc.email"
                    @click="exportCodexAuth(acc.email)">
                    <template #icon>
                      <Download class="w-3.5 h-3.5" :stroke-width="2" />
                    </template>
                    导出
                  </AtButton>
                  <AtButton v-if="!acc.is_main_account"
                    :variant="acc.disabled ? 'primary' : 'secondary'" size="sm"
                    :loading="actionEmail === acc.email && actionType === (acc.disabled ? 'enable' : 'disable')"
                    :disabled="actionDisabled || (actionEmail === acc.email && !['enable', 'disable'].includes(actionType))"
                    @click="toggleAccountDisabled(acc)">
                    {{ acc.disabled ? '启用' : '禁用' }}
                  </AtButton>
                  <AtButton v-if="!acc.is_main_account"
                    variant="danger" size="sm" confirm
                    :loading="actionEmail === acc.email && actionType === 'delete'"
                    :disabled="actionDisabled || (actionEmail === acc.email && actionType !== 'delete')"
                    @click="removeAccount(acc.email)">
                    删除
                  </AtButton>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 注册失败明细 -->
      <div class="border-t border-hairline p-5 bg-ink-50">
        <div class="flex items-center justify-between mb-3">
          <div>
            <h2 class="text-sm font-bold text-ink-950 tracking-tight">注册失败明细</h2>
            <div class="text-[11px] text-ink-500 mt-0.5">未能入池的注册尝试 (add-phone / duplicate / OAuth 失败 等)</div>
          </div>
          <AtButton variant="ghost" size="sm" :loading="failuresLoading" @click="loadFailures">
            刷新
          </AtButton>
        </div>
        <div v-if="failuresCounts && Object.keys(failuresCounts).length" class="flex flex-wrap gap-1.5 mb-3 text-[11px]">
          <span v-for="(cnt, cat) in failuresCounts" :key="cat"
            class="px-2 py-0.5 rounded-md border bg-surface border-hairline text-ink-700">
            {{ cat }}: <span class="text-rose-700 font-mono ml-0.5 tabular">{{ cnt }}</span>
          </span>
        </div>
        <div class="overflow-x-auto rounded-lg border border-hairline bg-surface">
          <div v-if="failuresUnavailable"
            class="px-3 py-3 text-xs text-amber-800 bg-amber-50 border-b border-amber-200">
            {{ failuresUnavailable }}
          </div>
          <table class="w-full text-sm">
            <thead class="text-[10px] uppercase tracking-widest text-ink-500 border-b border-hairline bg-ink-50">
              <tr>
                <th class="text-left px-3 py-2 font-semibold">时间</th>
                <th class="text-left px-3 py-2 font-semibold">邮箱</th>
                <th class="text-left px-3 py-2 font-semibold">类别</th>
                <th class="text-left px-3 py-2 font-semibold">原因</th>
                <th class="text-left px-3 py-2 font-semibold">附加</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-hairline text-xs">
              <tr v-if="!failuresItems.length">
                <td class="px-3 py-4 text-ink-400 italic" colspan="5">暂无失败记录</td>
              </tr>
              <tr v-for="(f, idx) in failuresItems" :key="idx" class="hover:bg-ink-50">
                <td class="px-3 py-2 text-ink-500 font-mono">{{ fmtTs(f.timestamp) }}</td>
                <td class="px-3 py-2 text-ink-700 font-mono">{{ f.email || '-' }}</td>
                <td class="px-3 py-2">
                  <span class="px-1.5 py-0.5 rounded-md border text-[10px] font-semibold uppercase tracking-wide"
                    :class="failureCategoryClass(f.category)">{{ f.category }}</span>
                </td>
                <td class="px-3 py-2 text-ink-600">{{ f.reason }}</td>
                <td class="px-3 py-2 text-ink-400 font-mono text-[10px]">{{ fmtFailureExtra(f) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Codex 认证导出弹窗 -->
      <div v-if="exportData"
        class="fixed inset-0 bg-ink-600/20 z-50 flex items-center justify-center p-4 animate-rise"
        @click.self="exportData = null">
        <div class="glass rounded-lg w-full max-w-2xl max-h-[80vh] flex flex-col">
          <div class="px-5 py-4 border-b border-hairline flex items-center justify-between">
            <h3 class="text-ink-950 font-bold tracking-tight">Codex CLI 认证文件</h3>
            <button @click="exportData = null" class="text-ink-500 hover:text-ink-950 transition focus-ring rounded-lg p-1">
              <X class="w-4 h-4" :stroke-width="2" />
            </button>
          </div>
          <div class="p-5 space-y-3 overflow-y-auto flex-1">
            <div class="px-3 py-2.5 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800 space-y-2">
              <div class="font-semibold">使用步骤:</div>
              <ol class="list-decimal list-inside space-y-1 text-xs text-amber-700">
                <li>退出当前 Codex CLI 会话</li>
                <li>删除旧文件:<code class="bg-amber-50 border border-amber-200 px-1 rounded">rm ~/.codex/auth.json</code></li>
                <li>将下方内容保存到 <code class="bg-amber-50 border border-amber-200 px-1 rounded">~/.codex/auth.json</code>(Windows: <code class="bg-amber-50 border border-amber-200 px-1 rounded">%APPDATA%\codex\auth.json</code>)</li>
                <li>重新启动 Codex CLI</li>
              </ol>
              <div class="text-xs text-amber-700">导出后 Codex CLI 直连 OpenAI,不走 CPA 代理,响应更快。</div>
            </div>
            <div class="relative">
              <pre class="bg-ink-50 border border-hairline rounded-lg p-4 text-xs font-mono text-ink-800 overflow-x-auto whitespace-pre">{{ exportJson }}</pre>
              <AtButton :variant="copied ? 'primary' : 'secondary'" size="sm"
                class="absolute top-2 right-2" @click="copyExport">
                {{ copied ? '已复制' : '复制' }}
              </AtButton>
            </div>
          </div>
          <div class="px-5 py-4 border-t border-hairline flex justify-end gap-2">
            <AtButton variant="primary" @click="downloadExport">
              <template #icon>
                <Download class="w-3.5 h-3.5" :stroke-width="2" />
              </template>
              下载 auth.json
            </AtButton>
            <AtButton variant="secondary" @click="exportData = null">关闭</AtButton>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Loading skeleton -->
  <div v-else-if="loading" class="space-y-4">
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
      <div v-for="i in 4" :key="i" class="glass-soft rounded-lg p-4 h-20 shimmer-bg"></div>
    </div>
    <div class="glass-soft rounded-lg h-64 shimmer-bg"></div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '../api.js'
import StatusBadge from './StatusBadge.vue'
import UsabilityCell from './UsabilityCell.vue'
import MasterHealthBanner from './MasterHealthBanner.vue'
import PoolHealthCard from './PoolHealthCard.vue'
import AtButton from './AtButton.vue'
import { ChevronLeft, ChevronRight, Download, RefreshCw, Trash2, X } from 'lucide-vue-next'
import {
  quotaRemainingPct as _qr,
  quotaPctText,
  formatQuotaReset,
  quotaPctColor,
} from '../composables/useStatus.js'
import { useToast } from '../composables/useToast.js'

const toast = useToast()

const props = defineProps({
  status: Object,
  loading: Boolean,
  runningTask: Object,
  adminStatus: { type: Object, default: null },
  masterHealth: { type: Object, default: null },
  registerFailures: { type: Object, default: null },
  registerFailuresLoading: { type: Boolean, default: false },
  registerFailuresUnavailable: { type: String, default: '' },
})
const emit = defineEmits(['refresh', 'reload-master-health'])

const actionEmail = ref('')
const actionType = ref('')
const syncing = ref(false)
const message = ref('')
const exportData = ref(null)
const copied = ref(false)
const messageClass = ref('')
const masterHealthBusy = ref(false)
const accountPage = ref(1)
const ACCOUNT_PAGE_SIZE = 50

// 批量删除选中态:按邮箱(小写)保存,便于跨刷新复用
const selectedSet = ref(new Set())
const batchDeleting = ref(false)
const batchProgress = ref('')
const bulkToggling = ref(false)

const failuresItems = computed(() => props.registerFailures?.items || [])
const failuresCounts = computed(() => props.registerFailures?.counts || {})
const failuresLoading = computed(() => !!props.registerFailuresLoading)
const failuresUnavailable = computed(() => props.registerFailuresUnavailable || '')

const accounts = computed(() => props.status?.accounts || [])
const totalAccounts = computed(() => accounts.value.length)
const totalAccountPages = computed(() => Math.max(1, Math.ceil(totalAccounts.value / ACCOUNT_PAGE_SIZE)))
const accountPageStart = computed(() => (accountPage.value - 1) * ACCOUNT_PAGE_SIZE)
const accountPageEnd = computed(() => Math.min(accountPageStart.value + ACCOUNT_PAGE_SIZE, totalAccounts.value))
const visibleAccounts = computed(() => accounts.value.slice(accountPageStart.value, accountPageEnd.value))
const hiddenAccountCount = computed(() => Math.max(0, accounts.value.length - visibleAccounts.value.length))

function goAccountPage(page) {
  const nextPage = Math.min(Math.max(1, page), totalAccountPages.value)
  if (nextPage === accountPage.value) return
  accountPage.value = nextPage
  clearSelection()
}

watch(totalAccountPages, pages => {
  if (accountPage.value > pages) {
    accountPage.value = pages
    clearSelection()
  }
})

function loadFailures() {
  emit('refresh')
}

function fmtTs(ts) {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  const pad = n => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function failureCategoryClass(cat) {
  const map = {
    phone_blocked: 'bg-rose-50 text-rose-700 border-rose-200',
    duplicate_exhausted: 'bg-orange-50 text-orange-700 border-orange-200',
    register_failed: 'bg-amber-50 text-amber-700 border-amber-200',
    oauth_failed: 'bg-sky-50 text-sky-700 border-sky-200',
    kick_failed: 'bg-yellow-50 text-yellow-700 border-yellow-200',
    exception: 'bg-red-50 text-red-700 border-red-200',
    master_subscription_degraded: 'bg-orange-50 text-orange-700 border-orange-200',
  }
  return map[cat] || 'bg-ink-50 text-ink-600 border-hairline'
}

function fmtFailureExtra(f) {
  const keys = ['step', 'register_attempts', 'duplicate_swaps', 'stage']
  const parts = []
  for (const k of keys) {
    if (f[k] !== undefined && f[k] !== null && f[k] !== '') parts.push(`${k}=${f[k]}`)
  }
  return parts.join(' ') || '-'
}

const adminReady = computed(() => !!props.adminStatus?.configured)
const actionDisabled = computed(() => !!props.runningTask || !adminReady.value || bulkToggling.value)

const selectableEmails = computed(() =>
  visibleAccounts.value.filter(a => !a.is_main_account).map(a => a.email)
)
const selectedEmails = computed(() =>
  selectableEmails.value.filter(e => selectedSet.value.has(e.toLowerCase()))
)
const selectedDisableEmails = computed(() =>
  visibleAccounts.value
    .filter(a => !a.is_main_account && !a.disabled && selectedSet.value.has((a.email || '').toLowerCase()))
    .map(a => a.email)
)
const selectedEnableEmails = computed(() =>
  visibleAccounts.value
    .filter(a => !a.is_main_account && a.disabled && selectedSet.value.has((a.email || '').toLowerCase()))
    .map(a => a.email)
)
const allSelectableChecked = computed(() =>
  selectableEmails.value.length > 0 && selectedEmails.value.length === selectableEmails.value.length
)
const someSelectableChecked = computed(() =>
  selectedEmails.value.length > 0 && selectedEmails.value.length < selectableEmails.value.length
)

function isSelected(email) {
  return selectedSet.value.has((email || '').toLowerCase())
}
function toggleSelect(email) {
  const key = (email || '').toLowerCase()
  const next = new Set(selectedSet.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  selectedSet.value = next
}
function toggleSelectAll() {
  if (allSelectableChecked.value) selectedSet.value = new Set()
  else selectedSet.value = new Set(selectableEmails.value.map(e => e.toLowerCase()))
}
function clearSelection() { selectedSet.value = new Set() }

// Round 9 — 派生 minGraceUntil:从所有 GRACE 子号取最早到期者,banner/PoolHealthCard 共用
const minGraceUntil = computed(() => {
  let min = null
  for (const acc of accounts.value) {
    if (acc.status === 'degraded_grace' && typeof acc.grace_until === 'number') {
      if (min === null || acc.grace_until < min) min = acc.grace_until
    }
  }
  return min
})

const cards = computed(() => {
  if (!props.status) return []
  const s = props.status.summary || {}
  const dg = s.degraded_grace ?? accounts.value.filter(a => a.status === 'degraded_grace').length
  return [
    { label: 'Active', value: s.active || 0, color: 'text-emerald-700', dot: 'rgb(16, 185, 129)' },
    { label: 'Standby', value: s.standby || 0, color: 'text-amber-700', dot: 'rgb(217, 119, 6)' },
    { label: 'Grace', value: dg, color: 'text-orange-700', dot: 'rgb(234, 88, 12)' },
    { label: 'Personal', value: s.personal || 0, color: 'text-sky-700', dot: 'rgb(2, 132, 199)' },
    { label: 'Disabled', value: s.disabled || 0, color: 'text-stone-700', dot: 'rgb(120, 113, 108)' },
    { label: 'Total', value: s.total || 0, color: 'text-ink-950', dot: 'rgb(79, 70, 229)' },
  ]
})

function quota(acc, type) {
  const qi = props.status?.quota_cache?.[acc.email] || acc.last_quota
  if (!qi) return null
  return _qr(qi, type)
}
function quotaPct(acc, type) {
  const qi = props.status?.quota_cache?.[acc.email] || acc.last_quota
  return quotaPctText(qi, type)
}
function quotaReset(acc, type) {
  const qi = props.status?.quota_cache?.[acc.email] || acc.last_quota
  return formatQuotaReset(qi, type)
}
function pctColor(remain) { return quotaPctColor(remain) }

function formatPoolTime(ts) {
  if (!ts) return '现在'
  const d = new Date(ts * 1000)
  const pad = n => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function nextUsableText(acc) {
  if (acc.disabled) return '-'
  return formatPoolTime(acc.next_usable_at)
}

function nextUsableReason(acc) {
  const map = {
    main_account: '母号',
    active_5h_reset: '当前使用中',
    ready_now: '已可复用',
    quota_resets_at: '等额度刷新',
    auth_retry_after: '等登录重试',
    personal: '个人号',
    disabled: '已禁用',
    pending: '处理中',
    auth_invalid: '认证异常',
    orphan: '席位异常',
  }
  return map[acc.next_usable_reason] || acc.next_usable_reason || '-'
}

const exportJson = computed(() => {
  if (!exportData.value) return ''
  return JSON.stringify(exportData.value.codex_auth, null, 2)
})

async function exportCodexAuth(email) {
  try {
    exportData.value = await api.getCodexAuth(email)
    copied.value = false
  } catch (e) {
    toast.error('导出失败', e.message)
  }
}

async function copyExport() {
  try {
    await navigator.clipboard.writeText(exportJson.value)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = exportJson.value
    ta.style.position = 'fixed'; ta.style.opacity = '0'
    document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta)
  }
  copied.value = true
  setTimeout(() => { copied.value = false }, 2400)
}

function downloadExport() {
  const blob = new Blob([exportJson.value], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = 'auth.json'; a.click()
  URL.revokeObjectURL(url)
}

async function syncAccounts() {
  syncing.value = true
  try {
    const result = await api.postSyncAccounts()
    toast.success('同步完成', result.message || '')
    emit('refresh')
  } catch (e) {
    toast.error('同步失败', e.message)
  } finally {
    syncing.value = false
  }
}

function canLogin(acc) {
  if (acc.is_main_account) return false
  if (acc.disabled) return false
  if (acc.status === 'active') return false
  if (acc.status === 'personal' && acc.auth_file) return false
  if (acc.status === 'degraded_grace') return false // grace 期内仍可用,无需补登录
  return true
}
// Round 11 realtime-probe.md v1.0 §4 — 仅对持有 auth_file 的子号显示"立即探活"
// (主号有自己的"立即重测"按钮在 MasterHealthBanner;orphan/pending 无 token 探不动)
function canProbe(acc) {
  if (acc.is_main_account) return false
  if (acc.disabled) return false
  if (!acc.auth_file) return false
  if (acc.status === 'pending' || acc.status === 'orphan') return false
  return true
}
function loginLabel(acc) {
  if (acc.status === 'personal' || acc.status === 'auth_invalid' || acc.status === 'orphan') return '补登录'
  return '登录'
}

async function loginAccount(email) {
  if (actionDisabled.value) return
  actionEmail.value = email; actionType.value = 'login'
  try {
    const result = await api.loginAccount(email)
    toast.success('登录任务已提交', `${email} · ${result.task_id}`)
    emit('refresh')
  } catch (e) {
    toast.error('提交登录任务失败', e.message)
  } finally {
    actionEmail.value = ''; actionType.value = ''
  }
}

// Round 11 realtime-probe.md v1.0 — 子号实时探活
// 调 POST /api/accounts/{email}/probe (force_codex_smoke=true)
// 副作用:后端落 last_quota_check_at,绕过 30min 节流;不修改 status (RT-I1)
async function probeAccount(email) {
  if (actionDisabled.value) return
  actionEmail.value = email; actionType.value = 'probe'
  try {
    const result = await api.probeAccount(email, true)
    // 后端 spec RT-I2:probe 永不抛 5xx,smoke_result ∈ {alive, auth_invalid, uncertain}
    const smoke = result?.smoke_result || result?.last_smoke_result || '-'
    const lastCheck = result?.last_quota_check_at
    const checkedAt = lastCheck
      ? new Date(lastCheck * 1000).toLocaleTimeString()
      : '刚刚'
    if (smoke === 'alive') {
      toast.success('探活成功', `${email} · ${checkedAt} · alive`)
    } else if (smoke === 'auth_invalid') {
      toast.warn('探活完成 · token 失效', `${email} · ${checkedAt} · auth_invalid`)
    } else {
      toast.info('探活完成', `${email} · ${checkedAt} · ${smoke}`)
    }
    emit('refresh')
  } catch (e) {
    toast.error('探活失败', e.message)
  } finally {
    actionEmail.value = ''; actionType.value = ''
  }
}

async function kickAccount(email) {
  if (actionDisabled.value) return
  const ok = window.confirm(`确认将 ${email} 移出 Team?\n账号会变为 standby 状态,额度恢复后可重新复用。`)
  if (!ok) return
  actionEmail.value = email; actionType.value = 'kick'
  try {
    const result = await api.kickAccount(email)
    toast.success('已移出 Team', result.message || email)
    emit('refresh')
  } catch (e) {
    toast.error('移出失败', e.message)
  } finally {
    actionEmail.value = ''; actionType.value = ''
  }
}

async function toggleAccountDisabled(acc) {
  if (actionDisabled.value) return
  const disabling = !acc.disabled
  const ok = window.confirm(
    disabling
      ? `确认禁用账号 ${acc.email}?\n禁用后会保留本地记录,但自动巡检、轮转和 CPA 同步会跳过该账号。`
      : `确认启用账号 ${acc.email}?\n启用后该账号会重新参与自动巡检、轮转和 CPA 同步。`
  )
  if (!ok) return
  actionEmail.value = acc.email
  actionType.value = disabling ? 'disable' : 'enable'
  try {
    const result = disabling
      ? await api.disableAccount(acc.email)
      : await api.enableAccount(acc.email)
    toast.success(disabling ? '账号已禁用' : '账号已启用', result.message || acc.email)
    emit('refresh')
  } catch (e) {
    toast.error(disabling ? '禁用失败' : '启用失败', e.message)
  } finally {
    actionEmail.value = ''; actionType.value = ''
  }
}

async function removeAccount(email) {
  if (actionDisabled.value) return
  // 二次确认已在 AtButton(confirm) 内做了第一次,这里再用 native confirm 提示破坏性操作细节
  const ok = window.confirm(`确认删除账号 ${email}?\n这会清理本地记录、CPA、Team/Invite 和 CloudMail。`)
  if (!ok) return
  actionEmail.value = email; actionType.value = 'delete'
  try {
    const result = await api.deleteAccount(email)
    toast.success('账号已删除', result.message || email)
    emit('refresh')
  } catch (e) {
    toast.error('删除失败', e.message)
  } finally {
    actionEmail.value = ''; actionType.value = ''
  }
}

async function bulkDisableSelected() {
  if (actionDisabled.value || bulkToggling.value) return
  const emails = selectedDisableEmails.value
  if (!emails.length) return
  const preview = emails.slice(0, 8).join('\n')
  const more = emails.length > 8 ? `\n…还有 ${emails.length - 8} 个` : ''
  const ok = window.confirm(
    `确认批量禁用以下 ${emails.length} 个账号?\n禁用后会保留本地记录,但自动巡检、轮转和 CPA 同步会跳过它们。\n\n${preview}${more}`
  )
  if (!ok) return
  bulkToggling.value = true
  try {
    const result = await api.bulkDisableAccounts(emails)
    toast.success('批量禁用完成', result.message || `已禁用 ${emails.length} 个账号`)
    clearSelection()
    emit('refresh')
  } catch (e) {
    toast.error('批量禁用失败', e.message)
  } finally {
    bulkToggling.value = false
  }
}

async function bulkEnableSelected() {
  if (actionDisabled.value || bulkToggling.value) return
  const emails = selectedEnableEmails.value
  if (!emails.length) return
  const preview = emails.slice(0, 8).join('\n')
  const more = emails.length > 8 ? `\n…还有 ${emails.length - 8} 个` : ''
  const ok = window.confirm(
    `确认批量启用以下 ${emails.length} 个账号?\n启用后它们会重新参与自动巡检、轮转和 CPA 同步。\n\n${preview}${more}`
  )
  if (!ok) return
  bulkToggling.value = true
  try {
    const result = await api.bulkEnableAccounts(emails)
    toast.success('批量启用完成', result.message || `已启用 ${emails.length} 个账号`)
    clearSelection()
    emit('refresh')
  } catch (e) {
    toast.error('批量启用失败', e.message)
  } finally {
    bulkToggling.value = false
  }
}

async function batchDelete() {
  if (actionDisabled.value || batchDeleting.value) return
  const emails = selectedEmails.value
  if (!emails.length) return
  const preview = emails.slice(0, 8).join('\n')
  const more = emails.length > 8 ? `\n…还有 ${emails.length - 8} 个` : ''
  const ok = window.confirm(
    `确认批量删除以下 ${emails.length} 个账号?这会清理本地记录、CPA、Team/Invite 和 CloudMail。\n\n${preview}${more}`
  )
  if (!ok) return
  batchDeleting.value = true; batchProgress.value = `0/${emails.length}`
  try {
    const r = await api.deleteAccountsBatch(emails, true)
    const s = r?.summary || {}
    const failed = (r?.results || []).filter(x => !x.ok)
    if (failed.length === 0) {
      toast.success('批量删除完成', `成功 ${s.ok}/${s.total}`)
    } else {
      const head = failed.slice(0, 3).map(x => `${x.email}: ${x.error}`).join('; ')
      toast.warn('批量删除部分失败', `成功 ${s.ok}/${s.total}; ${head}${failed.length > 3 ? ' …' : ''}`)
    }
    clearSelection()
    emit('refresh')
  } catch (e) {
    toast.error('批量删除失败', e.message)
  } finally {
    batchDeleting.value = false; batchProgress.value = ''
  }
}

async function onRefreshMasterHealth() {
  masterHealthBusy.value = true
  try {
    emit('reload-master-health', true)
    toast.info('已请求重测', '走 force_refresh,绕过 5min cache')
  } finally {
    setTimeout(() => { masterHealthBusy.value = false }, 1200)
  }
}
</script>
