<template>
  <!-- 初始配置页 -->
  <SetupPage v-if="needSetup" @configured="onSetupDone" />

  <!-- 登录页 -->
  <div v-else-if="!authenticated" class="min-h-screen flex items-center justify-center px-4">
    <div class="glass rounded-lg p-8 w-full max-w-sm">
      <div>
        <div class="text-[10px] uppercase tracking-[0.3em] text-ink-400 mb-1">Account Operations</div>
        <h1 class="text-2xl font-extrabold text-ink-950 mb-1 tracking-tight">AutoTeam</h1>
        <p class="text-sm text-ink-500 mb-6">输入管理 API Key 进入控制台</p>
        <div v-if="authError"
          class="mb-4 px-3 py-2.5 rounded-lg text-sm bg-rose-50 text-rose-700 border border-rose-200">
          {{ authError }}
        </div>
        <input
          v-model.trim="inputKey"
          type="password"
          placeholder="API Key"
          @keyup.enter="doLogin"
          class="w-full px-3.5 py-2.5 bg-surface border border-hairline rounded-lg text-sm text-ink-950
                 font-mono placeholder:text-ink-400 focus-ring mb-4 transition" />
        <AtButton variant="primary" class="w-full" :loading="authLoading" :disabled="!inputKey" @click="doLogin">
          {{ authLoading ? '验证中…' : '进入控制台' }}
        </AtButton>
      </div>
    </div>
  </div>

  <!-- 主面板 -->
  <div v-else class="flex min-h-screen">
    <!-- 侧边栏 -->
    <Sidebar :active="currentPage" :loading="loading" :auth-required="authRequired"
      @navigate="currentPage = $event" @refresh="refresh" @logout="doLogout" />

    <!-- 主内容区 -->
    <div class="flex-1 p-4 md:p-6 overflow-y-auto pb-20 md:pb-6 max-w-screen-2xl mx-auto w-full">
      <!-- 任务执行中提示 -->
      <div v-if="busyTask"
        class="flex items-center gap-2.5 text-sm text-amber-800 mb-4 px-3 py-2 rounded-lg border border-amber-200 bg-amber-50 w-fit animate-rise">
        <span class="animate-spin inline-block w-3.5 h-3.5 border-2 border-amber-700 border-t-transparent rounded-full"></span>
        <span class="font-medium">
          {{ busyTask.command === 'admin-login'
            ? '管理员登录中...'
            : busyTask.command === 'main-codex-sync'
              ? '主号 Codex 同步中...'
              : `${busyTask.command} 执行中...` }}
        </span>
      </div>

      <!-- 页面内容 — round-12 F1 加 Vue Transition page 过渡 -->
      <Transition name="page" mode="out-in">
        <Dashboard v-if="currentPage === 'dashboard'" key="dashboard"
          :status="status" :loading="loading" :running-task="busyTask" :admin-status="adminStatus"
          :register-failures="registerFailures" :register-failures-loading="registerFailuresLoading"
          :register-failures-unavailable="registerFailuresUnavailable"
          :master-health="masterHealth" @refresh="onActionRefresh" @reload-master-health="reloadMasterHealth" />

        <TeamMembers v-else-if="currentPage === 'team'" key="team" />

        <PoolPage v-else-if="currentPage === 'pool'" key="pool"
          :running-task="busyTask" :admin-status="adminStatus" :master-health="masterHealth" :status="status"
          :rotate-stream="rotateStream"
          @task-started="onTaskStarted" @refresh="onActionRefresh" @reload-master-health="reloadMasterHealth" />

        <SyncPage v-else-if="currentPage === 'sync'" key="sync"
          :running-task="busyTask" :admin-status="adminStatus"
          :rotate-stream="rotateStream"
          @task-started="onTaskStarted" @refresh="onActionRefresh" />

        <OAuthPage v-else-if="currentPage === 'oauth'" key="oauth"
          :manual-account-status="manualAccountStatus" @refresh="onActionRefresh" @progress="onAdminProgress" />

        <MailInboxPage v-else-if="currentPage === 'mail'" key="mail"
          :status="status" />

        <TaskHistoryPage v-else-if="currentPage === 'tasks'" key="tasks"
          :tasks="tasks" />

        <LogViewer v-else-if="currentPage === 'logs'" key="logs" />

        <Settings v-else-if="currentPage === 'settings'" key="settings"
          :admin-status="adminStatus" :codex-status="codexStatus"
          :master-health="masterHealth" :status="status"
          @refresh="onActionRefresh" @admin-progress="onAdminProgress" @reload-master-health="reloadMasterHealth" />
      </Transition>
    </div>

    <ToastHost />
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import { api, setApiKey, clearApiKey } from './api.js'
import { useAppState } from './composables/useAppState.js'
import SetupPage from './components/SetupPage.vue'
import Sidebar from './components/Sidebar.vue'
import Dashboard from './components/Dashboard.vue'
import TeamMembers from './components/TeamMembers.vue'
import PoolPage from './components/PoolPage.vue'
import SyncPage from './components/SyncPage.vue'
import TaskHistoryPage from './components/TaskHistoryPage.vue'
import LogViewer from './components/LogViewer.vue'
import OAuthPage from './components/OAuthPage.vue'
import MailInboxPage from './components/MailInboxPage.vue'
import Settings from './components/Settings.vue'
import ToastHost from './components/ToastHost.vue'
import AtButton from './components/AtButton.vue'

const needSetup = ref(false)
const authenticated = ref(false)
const authRequired = ref(false)
const authLoading = ref(false)
const authError = ref('')
const inputKey = ref('')
const currentPage = ref('dashboard')

const appState = useAppState(authenticated)
const {
  status,
  adminStatus,
  codexStatus,
  manualAccountStatus,
  registerFailures,
  registerFailuresLoading,
  registerFailuresUnavailable,
  tasks,
  loading,
  busyTask,
  rotateStream,
} = appState
// Round 9 — master-health 提到 App 级,4 个页面共享同一份(避免每页各刷各的)
const masterHealth = ref(null)
const masterHealthLoading = ref(false)

async function checkAuth() {
  try {
    const result = await api.checkAuth()
    authenticated.value = result.authenticated
    authRequired.value = result.auth_required
    return result.authenticated
  } catch (e) {
    if (e.status === 401) {
      authenticated.value = false
      authRequired.value = true
      return false
    }
    authenticated.value = true
    authRequired.value = false
    return true
  }
}

async function doLogin() {
  authError.value = ''
  authLoading.value = true
  try {
    setApiKey(inputKey.value)
    const ok = await checkAuth()
    if (!ok) {
      clearApiKey()
      authError.value = 'API Key 无效'
    } else {
      inputKey.value = ''
      refresh()
    }
  } catch (e) {
    clearApiKey()
    authError.value = e.message
  } finally {
    authLoading.value = false
  }
}

function doLogout() {
  clearApiKey()
  authenticated.value = false
}

async function refresh() {
  await appState.refresh()
}

async function reloadMasterHealth(forceRefresh = false) {
  if (!adminStatus.value?.configured) return
  masterHealthLoading.value = true
  try {
    masterHealth.value = await api.getMasterHealth(!!forceRefresh)
  } catch (e) {
    masterHealth.value = null
  } finally {
    masterHealthLoading.value = false
  }
}

function onTaskStarted() {
  appState.notifyActionStarted()
}

function onAdminProgress() {
  appState.notifyActionStarted()
}

function onActionRefresh() {
  appState.notifyActionStarted()
}

async function checkSetup() {
  try {
    const result = await api.getSetupStatus()
    return result.configured
  } catch {
    return true // 接口不存在说明是旧版本，跳过
  }
}

function onSetupDone() {
  needSetup.value = false
  checkAuth().then(ok => {
    if (ok) {
      refresh()
    }
  })
}

// admin 配置完成后自动拉一次 master-health
watch(
  () => adminStatus.value?.configured,
  (configured) => {
    if (configured && !masterHealth.value) reloadMasterHealth(false)
  }
)

onMounted(async () => {
  const setupOk = await checkSetup()
  if (!setupOk) {
    needSetup.value = true
    return
  }
  const ok = await checkAuth()
  if (ok) {
    refresh()
  }
})
</script>
