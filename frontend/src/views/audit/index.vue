<template>
  <section class="page" data-module="audit">
    <header class="page-head">
      <div>
        <h2>变更审计追踪管理</h2>
        <p class="page-desc">维护数仓变更的操作类型、前后对比与复核状态，为变更审计追踪提供可追溯的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记变更记录</button>
        <button class="btn" type="button" @click="exportRows">导出变更审计追踪清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>来源记录编号</span>
        <input v-model="recordFilter" placeholder="LIFE-0001 / CATA-0001 / RETN-0001 / RECY-0001" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ displayValue(row, column) }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无变更审计追踪数据，可先登记变更记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条变更审计追踪记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/audit'
const columns = ["记录编号", "变更对象", "变更类型", "变更前值", "变更后值", "操作人", "操作时间", "复核状态", "事件来源", "事件编号"]
const actions = ["标记复核", "归档记录"]
const statuses = ["已记录", "已复核", "已归档"]
const stats = [{"label": "变更记录总数", "value": 0}, {"label": "已复核记录", "value": 0}, {"label": "已归档记录", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const recordFilter = ref('')

function resetFilters() {
  filters.value = {}
  recordFilter.value = ''
  void reload()
}

/** 事件来源/事件编号列：历史审计记录无事件字段时按“—”展示，不补造历史。 */
function displayValue(row: Row, column: string): string | number | null {
  if (column === '事件来源') return (row.event_source as string) ?? '—'
  if (column === '事件编号') return (row.event_id as string) ?? '—'
  return row[column] ?? '—'
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '变更记录登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '变更审计追踪动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '变更审计追踪操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams(filters.value as Record<string, string>)
  // 支持按来源业务记录编号（LIFE-/CATA-/RETN-/RECY-）或审计编号追查同一事件。
  if (recordFilter.value.trim()) {
    params.set('record_no', recordFilter.value.trim())
  }
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error('变更记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '变更审计追踪列表读取失败'
  }
}

onMounted(reload)
</script>
