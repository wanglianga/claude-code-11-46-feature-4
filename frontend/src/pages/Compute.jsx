import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function Compute() {
  const [status, setStatus] = useState(null)
  const [revisions, setRevisions] = useState([])
  const [watermarks, setWatermarks] = useState([])
  const [error, setError] = useState('')

  async function reload() {
    try {
      const [s, r, w] = await Promise.all([
        api.get('/api/compute/status'),
        api.get('/api/compute/revisions?limit=50'),
        api.get('/api/compute/watermarks'),
      ])
      setStatus(s)
      setRevisions(r)
      setWatermarks(w)
    } catch (e) {
      setError(e.message)
    }
  }
  useEffect(() => {
    reload()
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!status) return <div className="muted">加载中…</div>

  return (
    <div>
      <section className="card">
        <div className="card-head">
          <h2>计算作业状态</h2>
          <button className="btn ghost" onClick={reload}>
            刷新
          </button>
        </div>
        <div className="stat-row">
          {Object.entries(status.events).map(([source, counts]) => (
            <div className="stat" key={source}>
              <div className="stat-title">{source}</div>
              <div>已应用 {counts.applied || 0}</div>
              <div>迟到应用 {counts.late_applied || 0}</div>
              <div>重复回调 {counts.duplicate || 0}（未计入窗口）</div>
            </div>
          ))}
          {status.sources.map((s) => (
            <div className="stat" key={s.source}>
              <div className="stat-title">源：{s.source}</div>
              <div>
                状态 <span className={`badge ${s.status === 'ok' ? 'current' : 'down'}`}>{s.status}</span>
              </div>
              <div className="muted small">最近成功：{s.last_success_at || '—'}</div>
              {s.message && <div className="muted small">{s.message}</div>}
            </div>
          ))}
        </div>

        <table>
          <thead>
            <tr>
              <th>特征</th>
              <th>来源</th>
              <th>实体数</th>
              <th>已应用事件</th>
              <th>迟到事件</th>
              <th>检查点</th>
              <th>修订</th>
              <th>最大水位</th>
            </tr>
          </thead>
          <tbody>
            {status.features.map((f) => (
              <tr key={f.feature_id}>
                <td>
                  {f.name} <span className="muted">v{f.version}</span>
                </td>
                <td>{f.source}</td>
                <td>{f.entities}</td>
                <td>{f.events_applied}</td>
                <td>{f.late_events}</td>
                <td>{f.checkpoints}</td>
                <td>{f.revisions}</td>
                <td className="small">{f.max_watermark || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <div className="grid-2">
        <section className="card">
          <h2>迟到事件修订日志</h2>
          <p className="muted small">迟到事件只重算受影响账户与窗口的检查点，每次变更在此留痕。</p>
          {revisions.length === 0 && <p className="muted">暂无修订</p>}
          {revisions.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>特征</th>
                  <th>账户</th>
                  <th>检查点 as_of</th>
                  <th>旧值 → 新值</th>
                  <th>触发事件</th>
                </tr>
              </thead>
              <tbody>
                {revisions.map((r) => (
                  <tr key={r.id}>
                    <td>
                      {r.feature} <span className="muted">v{r.feature_version}</span>
                    </td>
                    <td>{r.entity_id}</td>
                    <td className="small">{r.checkpoint_as_of}</td>
                    <td>
                      {r.old_value?.value ?? '—'} → <strong>{r.new_value?.value}</strong>
                    </td>
                    <td className="small">{r.cause_event_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card">
          <h2>水位（按特征 × 账户）</h2>
          <table>
            <thead>
              <tr>
                <th>特征</th>
                <th>账户</th>
                <th>水位（最大事件时间）</th>
                <th>已应用</th>
                <th>迟到</th>
              </tr>
            </thead>
            <tbody>
              {watermarks.slice(0, 100).map((w, i) => (
                <tr key={i}>
                  <td>
                    {w.feature} <span className="muted">v{w.feature_version}</span>
                  </td>
                  <td>{w.entity_id}</td>
                  <td className="small">{w.watermark}</td>
                  <td>{w.events_applied}</td>
                  <td>{w.late_events}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  )
}
