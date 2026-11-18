import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function Online() {
  const [entityId, setEntityId] = useState('ACC001')
  const [asOf, setAsOf] = useState('')
  const [setVersion, setSetVersion] = useState('')
  const [sets, setSets] = useState([])
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.get('/api/feature-sets').then(setSets).catch(() => {})
  }, [])

  async function query(e) {
    e && e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const params = new URLSearchParams({ entity_id: entityId })
      if (asOf) params.set('as_of', new Date(asOf).toISOString())
      if (setVersion) params.set('set_version', setVersion)
      const data = await api.get(`/api/online/features?${params}`)
      setResult(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <section className="card">
        <h2>在线特征查询</h2>
        <p className="muted small">
          每次请求钉住一个完整的特征集版本读取；as_of 缺省为当前时间，窗口按事件时间 (as_of−W, as_of] 计算。
        </p>
        <form onSubmit={query} className="inline-form">
          <label>
            账户
            <input value={entityId} onChange={(e) => setEntityId(e.target.value)} />
          </label>
          <label>
            as_of（可选）
            <input
              type="datetime-local"
              step="1"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
            />
          </label>
          <label>
            特征集版本（可选，默认当前指针）
            <select value={setVersion} onChange={(e) => setSetVersion(e.target.value)}>
              <option value="">当前发布版本</option>
              {sets
                .filter((s) => s.name === 'fraud_scoring')
                .map((s) => (
                  <option key={s.id} value={s.version}>
                    v{s.version}
                    {s.is_current ? '（当前）' : ''}
                  </option>
                ))}
            </select>
          </label>
          <button className="btn primary" disabled={busy}>
            {busy ? '查询中…' : '查询'}
          </button>
        </form>
        {error && <div className="error">{error}</div>}
      </section>

      {result && (
        <section className="card">
          <div className="card-head">
            <h2>
              结果：{result.entity_id}
              <span className="badge current">
                {result.feature_set.name} v{result.feature_set.version}
              </span>
            </h2>
            <span className="muted small">as_of {result.as_of}</span>
          </div>
          <table>
            <thead>
              <tr>
                <th>特征</th>
                <th>版本</th>
                <th>值</th>
                <th>状态</th>
                <th>来源说明</th>
              </tr>
            </thead>
            <tbody>
              {result.features.map((f) => (
                <tr key={f.name} className={f.status !== 'ok' ? 'row-warn' : ''}>
                  <td>{f.name}</td>
                  <td>v{f.version}</td>
                  <td>
                    {f.status === 'ok' ? (
                      <strong>{typeof f.value === 'number' ? Number(f.value.toFixed?.(4) ?? f.value) : String(f.value)}</strong>
                    ) : (
                      <span className="muted">null（不伪装为 0）</span>
                    )}
                  </td>
                  <td>
                    <span className={`badge ${f.status === 'ok' ? 'current' : 'down'}`}>{f.status}</span>
                  </td>
                  <td className="small muted">
                    {f.status !== 'ok'
                      ? `${f.source?.source} ${f.source?.status}，最近成功 ${f.source?.last_success_at || '—'}：${f.reason || ''}`
                      : ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted small">
            来源状态：
            {result.sources.map((s) => (
              <span key={s.source} className="src-chip">
                {s.source}: {s.status}（最近成功 {s.last_success_at || '—'}）
              </span>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
