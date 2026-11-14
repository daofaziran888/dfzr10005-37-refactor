/** 统一事件来源展示：四个业务台账共用同一套事件列与渲染口径。 */

export const EVENT_COLUMNS = ['事件来源', '事件编号', '落库时间'] as const

/**
 * 把后端事件字段补成可直接渲染的表格列；历史记录没有事件时展示“—”，不补造。
 */
export function withEventColumns(row: Record<string, unknown>): Record<string, unknown> {
  return {
    ...row,
    事件来源: (row.event_source as string) ?? '—',
    事件编号: (row.event_id as string) ?? '—',
    落库时间: (row.event_time as string) ?? '—',
  }
}

export function decorateRows(rows: Record<string, unknown>[]): Record<string, unknown>[] {
  return rows.map(withEventColumns)
}
