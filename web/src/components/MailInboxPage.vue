<template>
  <div class="space-y-5">
    <div class="glass rounded-lg p-4">
      <div class="flex flex-col md:flex-row md:items-end gap-3">
        <div class="flex-1">
          <label class="block text-xs text-ink-500 mb-1">邮箱地址</label>
          <input
            v-model.trim="address"
            type="email"
            placeholder="yun6@yunfei.life"
            class="w-full px-3 py-2 bg-surface border border-hairline rounded-lg text-sm text-ink-950 font-mono focus-ring"
            @keyup.enter="loadInbox"
          />
        </div>
        <AtButton variant="primary" :loading="loading" :disabled="!address" @click="loadInbox">
          <template #icon><RefreshCw class="w-4 h-4" :stroke-width="2" /></template>
          刷新邮件
        </AtButton>
      </div>

      <div v-if="suggestions.length" class="mt-3 flex flex-wrap gap-2">
        <button
          v-for="email in suggestions"
          :key="email"
          class="px-2.5 py-1 rounded border border-hairline bg-surface-hover text-xs font-mono text-ink-700 hover:bg-ink-100 focus-ring"
          @click="selectAddress(email)"
        >
          {{ email }}
        </button>
      </div>
    </div>

    <div v-if="error" class="px-4 py-3 rounded-lg border bg-rose-50 text-rose-700 border-rose-200 text-sm">
      {{ error }}
    </div>

    <div class="glass rounded-lg overflow-hidden">
      <div class="px-4 py-3 border-b border-hairline flex items-center justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold text-ink-950">最近邮件</h2>
          <div class="text-xs text-ink-500 font-mono">{{ result?.address || address || '-' }}</div>
        </div>
        <div class="text-xs text-ink-500">{{ result ? `${result.count} 封` : '未查询' }}</div>
      </div>

      <div v-if="loading" class="p-4 space-y-3">
        <div v-for="i in 3" :key="i" class="h-16 rounded bg-ink-50 shimmer-bg"></div>
      </div>

      <div v-else-if="!messages.length" class="p-8 text-center text-sm text-ink-500">
        没有邮件
      </div>

      <div v-else class="divide-y divide-hairline">
        <div v-for="mail in messages" :key="mail.id || `${mail.created_at}-${mail.subject}`" class="p-4">
          <div class="flex flex-col md:flex-row md:items-start md:justify-between gap-3">
            <div class="min-w-0">
              <div class="text-sm font-medium text-ink-950 truncate">{{ mail.subject || '(无主题)' }}</div>
              <div class="mt-1 text-xs text-ink-500 font-mono break-all">{{ mail.from || '-' }}</div>
              <div class="mt-1 text-xs text-ink-400">{{ mail.created_at || '-' }}</div>
            </div>
            <div class="flex flex-wrap gap-2 md:justify-end">
              <button
                v-for="code in mail.codes"
                :key="code"
                class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded border border-emerald-200 bg-emerald-50 text-emerald-800 font-mono text-sm hover:bg-emerald-100 focus-ring"
                @click="copyCode(code)"
              >
                <Copy class="w-3.5 h-3.5" :stroke-width="2" />
                {{ code }}
              </button>
            </div>
          </div>
          <div
            v-if="mail.body || mail.preview"
            class="mt-3 text-xs text-ink-700 bg-ink-50 border border-hairline rounded p-3 whitespace-pre-wrap break-words max-h-96 overflow-auto leading-relaxed"
          >
            {{ mail.body || mail.preview }}
            <div v-if="mail.body_truncated" class="mt-2 pt-2 border-t border-hairline text-ink-400">
              正文过长，已显示前 20000 个字符
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { Copy, RefreshCw } from 'lucide-vue-next'
import { api } from '../api.js'
import AtButton from './AtButton.vue'

const props = defineProps({
  status: { type: Object, default: null },
})

const address = ref('')
const result = ref(null)
const loading = ref(false)
const error = ref('')

const suggestions = computed(() => {
  const seen = new Set()
  const out = []
  for (const acc of props.status?.accounts || []) {
    const email = String(acc.email || '').trim().toLowerCase()
    if (!email || seen.has(email)) continue
    seen.add(email)
    out.push(email)
  }
  return out.slice(0, 12)
})

const messages = computed(() => result.value?.items || [])

watch(
  suggestions,
  (next) => {
    if (!address.value && next.length) address.value = next[0]
  },
  { immediate: true },
)

function selectAddress(email) {
  address.value = email
  loadInbox()
}

async function loadInbox() {
  if (!address.value || loading.value) return
  loading.value = true
  error.value = ''
  try {
    result.value = await api.getMailInbox(address.value, 10)
  } catch (e) {
    error.value = e.message || '查询失败'
  } finally {
    loading.value = false
  }
}

async function copyCode(code) {
  try {
    await navigator.clipboard.writeText(code)
  } catch {
    const el = document.createElement('textarea')
    el.value = code
    document.body.appendChild(el)
    el.select()
    document.execCommand('copy')
    document.body.removeChild(el)
  }
}
</script>
