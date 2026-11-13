import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dwd'
const columns = [
  "流水编号",
  "输入表名",
  "输出表名",
  "空值填充",
  "格式转换",
  "脏数据比例",
  "执行批次",
  "清洗状态",
  "动作序号",
  "分区水位",
  "上游接入状态",
  "清洗准入",
  "拒绝原因"
]
const actions = [
  "启动清洗",
  "暂停清洗",
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
    "label": "清洗流水总数",
    "value": 0
  },
  {
    "label": "运行中清洗",
    "value": 0
  },
  {
    "label": "已暂停清洗",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

export default function DwdView() {
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
        throw new Error('清洗流水列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '明细层清洗列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    // 幂等键 = 流水编号 + 动作序号；启动清洗还要过上游 ODS 的清洗准入。
    const actionSeq = Number(row['动作序号'] ?? 0) + 1
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action, action_seq: actionSeq } }),
      })
      if (!response.ok) {
        throw new Error('明细层清洗动作未生效，请稍后重试')
      }
      const payload = await response.json()
      // 被迁移表/准入/幂等键拒绝时不本地改状态，reload 后两页都停在原状态并展示原因。
      if (payload.ok === false) {
        setErrorMessage(payload.reject_reason ?? payload.message ?? '明细层清洗动作被拒绝')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '明细层清洗操作失败')
    }
  }

  return (
    <section className="page" data-module="dwd">
      <header className="page-head">
        <div>
          <h2>明细层清洗管理</h2>
          <p className="page-desc">维护明细层清洗流水的规则定义、执行状态与批次记录，为数据标准化提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('清洗流水登记入口尚未接入审批流')}>
            登记清洗流水
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出明细层清洗清单
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无明细层清洗数据，可先登记清洗流水</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条明细层清洗记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
