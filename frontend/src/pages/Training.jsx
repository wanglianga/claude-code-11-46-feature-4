import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function Training() {
  const [samples, setSamples] = useState([])
  const [datasets, setDatasets] = useState([])
  const [form, setForm] = useState({ sample_key: '', entity_id: 'ACC001', decision_time: '', transaction_id: '' })
  const [trace, setTrace] = useState(null)
  const [datasetName, setDatasetName] = useState('')
  const [datasetDetail, setDatasetDetail] = useState(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  async function reload() {
    const [s, d] = await Promise.all([api.get('/api/training/samples'), api.get('/api/training/datasets')])
    setSamples(s)
    setDatasets(d)
  }
  useEffect(() => {
    reload().catch((e) => setError(e.message))
  }, [])

  async function createSample(e) {
    e.preventDefault()
    setError('')
    setMessage('')
    try {
      await api.post('/api/training/samples', {
        sample_key: form.sample_key,
        entity_id: form.entity_id,
        decision_time: new Date(form.decision_time).toISOString(),
        transaction_id: form.transaction_id || null,
      })
      setMessage(`样本 ${form.sample_key} 已创建`)
      setForm({ ...form, sample_key: '', transaction_id: '' })
      await reload()
    } catch (err) {
      setError(err.message)
    }
  }

  async function showTrace(sampleId) {
    setError('')
    try {
      const data = await api.get(`/api/training/samples/${sampleId}/trace`)
      setTrace(data)
    } catch (err) {
      setError(err.message)
    }
  }

  async function buildDataset(e) {
    e.preventDefault()
    setError('')
    setMessage('')
    try {
      const resp = await api.post('/api/training/datasets', {
        name: datasetName || `dataset-${Date.now()}`,
        feature_set: 'fraud_scoring',
      })
      setMessage(`数据集已构建：${resp.name}（${resp.row_count} 行，特征集 v${resp.feature_set_version}）`)
      setDatasetName('')
      await reload()
    } catch (err) {
      setError(err.message)
    }
  }

  async function showDataset(id) {
    setError('')
    try {
      setDatasetDetail(await api.get(`/api/training/datasets/${id}`))
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div>
      <div className="grid-2">
        <section className="card">
          <h2>训练样本</h2>
          <form onSubmit={createSample} className="inline-form">
            <input
              required
              placeholder="样本标识，如 S-0001"
              value={form.sample_key}
              onChange={(e) => setForm({ ...form, sample_key: e.target.value })}
            />
            <input
              required
              placeholder="账户"
              value={form.entity_id}
              onChange={(e) => setForm({ ...form, entity_id: e.target.value })}
            />
            <input
              required
              type="datetime-local"
              step="1"
              title="决策时点"
              value={form.decision_time}
              onChange={(e) => setForm({ ...form, decision_time: e.target.value })}
            />
            <input
              placeholder="关联交易ID（可选）"
              value={form.transaction_id}
              onChange={(e) => setForm({ ...form, transaction_id: e.target.value })}
            />
            <button className="btn primary">创建样本</button>
          </form>
          {message && <div className="ok">{message}</div>}
          {error && <div className="error">{error}</div>}
          <table>
            <thead>
              <tr>
                <th>样本</th>
                <th>账户</th>
                <th>决策时点</th>
                <th>标签</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {samples.map((s) => (
                <tr key={s.id}>
                  <td>{s.sample_key}</td>
                  <td>{s.entity_id}</td>
                  <td className="small">{s.decision_time}</td>
                  <td>
                    <span className={`badge ${s.label_status === 'confirmed' ? 'current' : s.label_status === 'pending' ? 'pending' : ''}`}>
                      {s.label_status}
                    </span>
                    {s.label_value ? ` ${s.label_value}` : ''}
                  </td>
                  <td>
                    <button className="btn ghost" onClick={() => showTrace(s.id)}>
                      追溯
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="card">
          <h2>训练数据集</h2>
          <form onSubmit={buildDataset} className="inline-form">
            <input
              placeholder="数据集名称"
              value={datasetName}
              onChange={(e) => setDatasetName(e.target.value)}
            />
            <button className="btn primary" disabled={samples.length === 0}>
              用当前口径构建（全部样本）
            </button>
          </form>
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>特征集</th>
                <th>行数</th>
                <th>构建时间</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {datasets.map((d) => (
                <tr key={d.id}>
                  <td>{d.name}</td>
                  <td>
                    {d.feature_set_name} v{d.feature_set_version}
                  </td>
                  <td>{d.row_count}</td>
                  <td className="small">{d.created_at}</td>
                  <td>
                    <button className="btn ghost" onClick={() => showDataset(d.id)}>
                      查看
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {datasetDetail && (
            <div className="trace-box">
              <h3>
                {datasetDetail.name}（{datasetDetail.feature_set_name} v{datasetDetail.feature_set_version}）
              </h3>
              <table>
                <thead>
                  <tr>
                    <th>样本</th>
                    <th>决策时点</th>
                    <th>标签</th>
                    <th>特征值</th>
                  </tr>
                </thead>
                <tbody>
                  {datasetDetail.rows.map((r) => (
                    <tr key={r.sample_id}>
                      <td>{r.sample_key}</td>
                      <td className="small">{r.decision_time}</td>
                      <td>{r.label_value || '—'}</td>
                      <td className="small">
                        {Object.entries(r.features)
                          .map(([k, v]) => `${k}=${v === null ? 'null' : typeof v === 'number' ? Number(v.toFixed(4)) : v}`)
                          .join('，')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>

      {trace && (
        <section className="card">
          <div className="card-head">
            <h2>
              样本追溯：{trace.sample_key}
              <span className="badge current">
                {trace.feature_set.name} v{trace.feature_set.version}
              </span>
            </h2>
            <button className="btn ghost" onClick={() => setTrace(null)}>
              关闭
            </button>
          </div>
          <p>
            决策时点 <strong>{trace.decision_time}</strong> · 账户 <strong>{trace.entity_id}</strong> · 标签{' '}
            <span className={`badge ${trace.label_status === 'confirmed' ? 'current' : 'pending'}`}>{trace.label_status}</span>
            {trace.label_value ? ` ${trace.label_value}` : ''}
            {trace.label_record && (
              <span className="muted small">
                （{trace.label_record.label_type} 生效于 {trace.label_record.event_time}，确认于 {trace.label_record.confirmed_at}）
              </span>
            )}
          </p>
          {trace.features.map((f) => (
            <div key={f.name} className="trace-box">
              <h3>
                {f.name} <span className="muted">v{f.version}</span> ={' '}
                {f.status === 'ok' ? <strong>{typeof f.value === 'number' ? Number(f.value.toFixed(4)) : String(f.value)}</strong> : <span className="muted">unavailable</span>}
              </h3>
              {f.inputs && f.inputs.length > 0 ? (
                <table>
                  <thead>
                    <tr>
                      <th>输入记录</th>
                      <th>事件时间</th>
                      <th>明细</th>
                    </tr>
                  </thead>
                  <tbody>
                    {f.inputs.map((inp, i) => (
                      <tr key={i}>
                        <td className="small">
                          {inp.kind === 'transaction' && `交易 ${inp.txn_id}`}
                          {inp.kind === 'label' && `标签 ${inp.label_id}`}
                          {inp.kind === 'account_profile' && `账户快照 ${inp.account_id}`}
                        </td>
                        <td className="small">{inp.event_time || inp.snapshot_date}</td>
                        <td className="small muted">
                          {inp.kind === 'transaction' && `金额 ${inp.amount} · ${inp.type} · ${inp.status} · 商户 ${inp.merchant_id}`}
                          {inp.kind === 'label' && `${inp.label_type}=${inp.label_value} · 确认于 ${inp.confirmed_at}`}
                          {inp.kind === 'account_profile' && `KYC ${inp.kyc_level} · ${inp.country} · 开户 ${inp.age_days} 天`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="muted small">窗口内无输入记录</p>
              )}
            </div>
          ))}
        </section>
      )}
    </div>
  )
}
