import React, { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'

const EMPTY_FORM = {
  name: '',
  version: '',
  description: '',
  owner: '',
  source: 'transactions',
  aggregation: 'COUNT',
  field: '',
  window_seconds: 3600,
  filter_json: '',
  value_type: 'number',
}

function windowLabel(seconds) {
  if (!seconds) return '—'
  if (seconds % 86400 === 0) return `${seconds / 86400}天`
  if (seconds % 3600 === 0) return `${seconds / 3600}小时`
  return `${seconds}秒`
}

export default function Catalog() {
  const [features, setFeatures] = useState([])
  const [sets, setSets] = useState([])
  const [form, setForm] = useState(EMPTY_FORM)
  const [selected, setSelected] = useState({}) // "name@version" -> true
  const [publishNote, setPublishNote] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  async function reload() {
    const [f, s] = await Promise.all([api.get('/api/features'), api.get('/api/feature-sets')])
    setFeatures(f)
    setSets(s)
  }
  useEffect(() => {
    reload().catch((e) => setError(e.message))
  }, [])

  const key = (f) => `${f.name}@${f.version}`
  const chosen = useMemo(() => features.filter((f) => selected[key(f)]), [features, selected])

  async function createFeature(e) {
    e.preventDefault()
    setError('')
    setMessage('')
    try {
      const body = {
        name: form.name.trim(),
        description: form.description,
        owner: form.owner || undefined,
        source: form.source,
        aggregation: form.aggregation,
        value_type: form.value_type,
      }
      if (form.version) body.version = Number(form.version)
      if (form.source === 'account_profile') {
        body.field = form.field
      } else {
        body.window_seconds = Number(form.window_seconds)
        if (form.aggregation !== 'COUNT') body.field = form.field || 'amount'
        if (form.filter_json.trim()) body.filter_json = JSON.parse(form.filter_json)
      }
      await api.post('/api/features', body)
      setMessage(`特征 ${body.name} 创建成功（draft）`)
      setForm(EMPTY_FORM)
      await reload()
    } catch (err) {
      setError(err.message)
    }
  }

  async function publish(e) {
    e.preventDefault()
    setError('')
    setMessage('')
    try {
      const members = chosen.map((f) => ({ name: f.name, version: f.version }))
      const resp = await api.post('/api/feature-sets/fraud_scoring/publish', {
        members,
        note: publishNote,
      })
      setMessage(`已发布 fraud_scoring v${resp.version}（${members.length} 个特征），在线指针已原子切换`)
      setSelected({})
      setPublishNote('')
      await reload()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="grid-2">
      <section className="card">
        <h2>特征目录</h2>
        <p className="muted small">登记定义、输入字段、时间窗口与负责人；发布后计算作业开始维护流式窗口状态。</p>
        <table>
          <thead>
            <tr>
              <th></th>
              <th>名称</th>
              <th>版本</th>
              <th>来源</th>
              <th>聚合</th>
              <th>窗口</th>
              <th>负责人</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {features.map((f) => (
              <tr key={f.id}>
                <td>
                  <input
                    type="checkbox"
                    checked={!!selected[key(f)]}
                    onChange={(e) =>
                      setSelected({ ...selected, [key(f)]: e.target.checked })
                    }
                  />
                </td>
                <td>
                  <div>{f.name}</div>
                  <div className="muted small">{f.description}</div>
                </td>
                <td>v{f.version}</td>
                <td>{f.source}</td>
                <td>
                  {f.aggregation}
                  {f.field ? `(${f.field})` : ''}
                </td>
                <td>{windowLabel(f.window_seconds)}</td>
                <td>{f.owner}</td>
                <td>
                  <span className={`badge ${f.status}`}>{f.status}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <h3>发布新版本（fraud_scoring）</h3>
        <form onSubmit={publish} className="inline-form">
          <span className="muted">已选 {chosen.length} 个特征</span>
          <input
            placeholder="发布说明"
            value={publishNote}
            onChange={(e) => setPublishNote(e.target.value)}
          />
          <button className="btn primary" disabled={chosen.length === 0}>
            发布并切换在线指针
          </button>
        </form>
        {message && <div className="ok">{message}</div>}
        {error && <div className="error">{error}</div>}
      </section>

      <section>
        <div className="card">
          <h2>新建特征 / 新版本</h2>
          <form onSubmit={createFeature} className="form-grid">
            <label>
              名称
              <input
                required
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如 txn_count_1h"
              />
            </label>
            <label>
              版本（留空自动递增）
              <input
                value={form.version}
                onChange={(e) => setForm({ ...form, version: e.target.value })}
                placeholder="1"
              />
            </label>
            <label>
              负责人
              <input
                value={form.owner}
                onChange={(e) => setForm({ ...form, owner: e.target.value })}
                placeholder="risk-ml"
              />
            </label>
            <label>
              数据来源
              <select
                value={form.source}
                onChange={(e) => setForm({ ...form, source: e.target.value })}
              >
                <option value="transactions">transactions（交易流）</option>
                <option value="labels">labels（拒付/核查）</option>
                <option value="account_profile">account_profile（账户日快照）</option>
              </select>
            </label>
            <label>
              聚合方式
              <select
                value={form.aggregation}
                onChange={(e) => setForm({ ...form, aggregation: e.target.value })}
              >
                {['COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'LAST'].map((a) => (
                  <option key={a}>{a}</option>
                ))}
              </select>
            </label>
            <label>
              聚合字段
              <input
                value={form.field}
                onChange={(e) => setForm({ ...form, field: e.target.value })}
                placeholder={form.source === 'account_profile' ? 'kyc_level / age_days / country' : 'amount'}
              />
            </label>
            {form.source !== 'account_profile' && (
              <>
                <label>
                  窗口（秒）
                  <input
                    type="number"
                    value={form.window_seconds}
                    onChange={(e) => setForm({ ...form, window_seconds: e.target.value })}
                  />
                </label>
                <label>
                  过滤条件（JSON，可选）
                  <input
                    value={form.filter_json}
                    onChange={(e) => setForm({ ...form, filter_json: e.target.value })}
                    placeholder='{"type":"purchase"}'
                  />
                </label>
              </>
            )}
            <label className="span-2">
              描述
              <input
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
              />
            </label>
            <button className="btn primary span-2">创建（draft）</button>
          </form>
        </div>

        <div className="card">
          <h2>特征集版本</h2>
          {sets.map((s) => (
            <div key={s.id} className="set-version">
              <div>
                <strong>
                  {s.name} v{s.version}
                </strong>
                {s.is_current && <span className="badge current">当前在线</span>}
                <span className="muted small"> · {s.created_by} · {s.note}</span>
              </div>
              <div className="muted small">
                {s.members.map((m) => `${m.name}@v${m.version}`).join('，')}
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
