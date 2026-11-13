import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/ods'
const columns = [
  "流水编号",
  "来源系统",
  "源表名称",
  "落地表名",
  "接入周期",
  "分区粒度",
  "保留天数",
  "最近接入时间",
  "动作序号",
  "分区水位",
  "拒绝原因"
]
const actions = [
  "启动接入",
  "暂停接入",
  "归档流水"
]
const statuses = [
  "草稿",
  "运行中",
  "已暂停",
  "已完成"
]
const stats = [
  {
    "label": "接入流水总数",
    "value": 0
  },
  {
    "label": "运行中流水",
    "value": 0
  },
  {
    "label": "已暂停流水",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

export default function OdsView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('接入流水列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '贴源层接入列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    // 幂等键 = 流水编号 + 动作序号；序号取页面上的最新值 +1，失败可原样重试。
    const actionSeq = Number(row['动作序号'] ?? 0) + 1
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action, action_seq: actionSeq } }),
      })
      if (!response.ok) {
        throw new Error('贴源层接入动作未生效，请稍后重试')
      }
      const payload = await response.json()
      // 服务端按显式迁移表/幂等键拒绝：不本地改状态，reload 后页面停在原状态并展示拒绝原因。
      if (payload.ok === false) {
        setErrorMessage(payload.reject_reason ?? payload.message ?? '贴源层接入动作被拒绝')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '贴源层接入操作失败')
    }
  }

  return (
    <section className="page" data-module="ods">
      <header className="page-head">
        <div>
          <h2>贴源层接入管理</h2>
          <p className="page-desc">维护贴源层接入流水的配置、执行状态与落地记录，为原始数据接入提供可追踪的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('接入流水登记入口尚未接入审批流')}>
            登记接入流水
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出贴源层接入清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无贴源层接入数据，可先登记接入流水</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条贴源层接入记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
