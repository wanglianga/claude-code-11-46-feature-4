import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

const CLASS_LABEL = {
  replayable_plaintext: '可在受控环境重放（原文）',
  replayable_encrypted: '可在受控环境重放（加密副本）',
  plaintext_unavailable: '原文不可用（仅存摘要/受限）',
  evidence_chain_broken: '证据链损坏',
}
const CLASS_BADGE = {
  replayable_plaintext: 'current',
  replayable_encrypted: 'current',
  plaintext_unavailable: 'pending',
  evidence_chain_broken: 'down',
}
const STATE_LABEL = {
  plaintext: '原文可用',
  encrypted_copy: '加密副本',
  encrypted_locked: '密钥撤销·锁定',
  digest_only: '仅摘要',
  destroyed: '已销毁',
  evidence_chain_broken: '证据链损坏',
}

export default function Replay() {
  const [models, setModels] = useState([])
  const [batches, setBatches] = useState([])
  const [packages, setPackages] = useState([])
  const [policies, setPolicies] = useState([])
  const [sweeps, setSweeps] = useState([])
  const [keys, setKeys] = useState([])
  const [corrections, setCorrections] = useState([])
  const [samples, setSamples] = useState([])

  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [verify, setVerify] = useState(null)
  const [replay, setReplay] = useState(null)
  const [deps, setDeps] = useState(null)
  const [manifest, setManifest] = useState(null)
  const [approvals, setApprovals] = useState([])

  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [sweepAsOf, setSweepAsOf] = useState('')
  const [policyAlg, setPolicyAlg] = useState('sha256')
  const [policyDays, setPolicyDays] = useState('7')
  const [corr, setCorr] = useState({ record_id: '', amount: '' })
  const [approvalReason, setApprovalReason] = useState('季度模型复核')

  async function reload() {
    try {
      const [m, b, p, pol, sw, k, c, s] = await Promise.all([
        api.get('/api/models'),
        api.get('/api/model-batches'),
        api.get('/api/replay-packages'),
        api.get('/api/retention/policies'),
        api.get('/api/retention/sweeps'),
        api.get('/api/keys'),
        api.get('/api/source-corrections'),
        api.get('/api/training/samples'),
      ])
      setModels(m)
      setBatches(b)
      setPackages(p)
      setPolicies(pol)
      setSweeps(sw)
      setKeys(k)
      setCorrections(c)
      setSamples(s)
      if (selected) await loadSelected(selected.id)
    } catch (e) {
      setError(e.message)
    }
  }

  async function loadSelected(id) {
    const [d, a] = await Promise.all([
      api.get(`/api/replay-packages/${id}`),
      api.get(`/api/replay-packages/${id}/approvals`),
    ])
    setDetail(d)
    setApprovals(a)
  }

  useEffect(() => {
    reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function pick(p) {
    setError('')
    setMessage('')
    setVerify(null)
    setReplay(null)
    setDeps(null)
    setManifest(null)
    setSelected(p)
    try {
      await loadSelected(p.id)
    } catch (e) {
      setError(e.message)
    }
  }

  async function freezeBatch(batchKey) {
    const sampleIds = samples.slice(0, 3).map((s) => s.id)
    setError('')
    setMessage('')
    try {
      await api.post('/api/model-batches', {
        batch_key: batchKey,
        model_name: 'fraud_blocker',
        sample_ids: sampleIds,
        freeze: true,
      })
      setMessage(`批次 ${batchKey} 已冻结（模型、特征定义与输入摘要一并钉住）`)
      await reload()
    } catch (e) {
      setError(e.message)
    }
  }

  async function makePackage(batchId, pause = false) {
    setError('')
    setMessage('')
    try {
      const p = await api.post('/api/replay-packages', {
        batch_id: batchId,
        pause_before_seal: pause,
      })
      setMessage(pause ? '复现包已生成但暂未封存（用于演示中途策略变更）' : '复现包已生成并封存')
      await reload()
      await pick(p)
    } catch (e) {
      setError(e.message)
    }
  }

  async function sealSelected() {
    try {
      await api.post(`/api/replay-packages/${selected.id}/seal`)
      setMessage('已封存：保留清单生成启动时冻结的策略快照')
      await reload()
    } catch (e) {
      setError(e.message)
    }
  }

  async function doVerify() {
    setError('')
    try {
      setVerify(await api.get(`/api/replay-packages/${selected.id}/verify`))
    } catch (e) {
      setError(e.message)
    }
  }

  async function doReplay() {
    setError('')
    setReplay(null)
    try {
      setReplay(await api.post(`/api/replay-packages/${selected.id}/replay`))
      await loadSelected(selected.id)
    } catch (e) {
      setError(e.message)
    }
  }

  async function doDeps() {
    try {
      setDeps(await api.get(`/api/replay-packages/${selected.id}/dependencies`))
    } catch (e) {
      setError(e.message)
    }
  }

  async function doManifest() {
    try {
      setManifest(await api.get(`/api/replay-packages/${selected.id}/manifest`))
    } catch (e) {
      setError(e.message)
    }
  }

  async function requestApproval(scope) {
    try {
      await api.post(`/api/replay-packages/${selected.id}/approvals`, {
        scope,
        reason: approvalReason,
      })
      setMessage(`已提交 ${scope} 审批申请（需管理员批准，申请人不可自批）`)
      await loadSelected(selected.id)
    } catch (e) {
      setError(e.message)
    }
  }

  async function decide(id, approve) {
    try {
      await api.post(`/api/approvals/${id}/decide`, { approve, note: approve ? '同意' : '驳回' })
      setMessage(approve ? '审批已通过' : '审批已驳回')
      await loadSelected(selected.id)
    } catch (e) {
      setError(e.message)
    }
  }

  async function runSweep() {
    try {
      const s = await api.post('/api/retention/sweeps', {
        as_of: sweepAsOf ? new Date(sweepAsOf).toISOString() : null,
      })
      setMessage(
        `保留清理完成：原文过期 ${s.records_expired} 条，销毁加密副本 ${s.archives_destroyed} 条`,
      )
      await reload()
    } catch (e) {
      setError(e.message)
    }
  }

  async function createPolicy() {
    try {
      const base = policies[0]?.rules
      if (!base) return
      const rules = JSON.parse(JSON.stringify(base))
      rules.transaction_detail.plaintext_days = Number(policyDays)
      rules.transaction_detail.strategy = Number(policyDays) > 0 ? 'encrypted_copy' : 'encrypted_copy'
      rules.account_profile.plaintext_days = Math.min(
        Number(policyDays),
        rules.account_profile.protected_copy_days,
      )
      await api.post('/api/retention/policies', {
        rules,
        digest_algorithm: policyAlg,
        note: `交易原文保留 ${policyDays} 天；摘要算法 ${policyAlg}`,
      })
      setMessage(`已发布新策略版本（摘要算法 ${policyAlg}）`)
      await reload()
    } catch (e) {
      setError(e.message)
    }
  }

  async function rotateKey() {
    try {
      const k = await api.post('/api/keys/rotate', { reason: '常规轮换' })
      setMessage(`已轮换到密钥 v${k.version}；旧版本封存的证据仍可用其版本密钥解密`)
      await reload()
    } catch (e) {
      setError(e.message)
    }
  }

  async function revokeKey(k) {
    try {
      await api.post(`/api/keys/${k.id}/revoke`, { reason: '应急撤销' })
      setMessage(`密钥 v${k.version} 已撤销：其加密副本进入锁定态`)
      await reload()
    } catch (e) {
      setError(e.message)
    }
  }

  async function restoreKey(k) {
    try {
      await api.post(`/api/keys/${k.id}/restore`, { reason: '撤销恢复' })
      setMessage(`密钥 v${k.version} 已恢复，可解密其封存副本`)
      await reload()
    } catch (e) {
      setError(e.message)
    }
  }

  async function applyCorrection(e) {
    e.preventDefault()
    try {
      await api.post('/api/source-corrections', {
        record_id: corr.record_id,
        amount: corr.amount ? Number(corr.amount) : null,
        note: '上游补传修正',
      })
      setMessage('来源补传已应用：现有规范记录摘要改变，封存证据保持不变')
      await reload()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div>
      {error && <div className="error">{error}</div>}
      {message && <div className="ok">{message}</div>}

      <div className="grid-2">
        <section className="card">
          <h2>冻结批次与模型</h2>
          <p className="muted small">
            模型、特征集版本与输入批次冻结后不可变；复现包只能从冻结批次生成。
          </p>
          <div className="action-list">
            <button className="btn primary" onClick={() => freezeBatch(`BATCH-UI-${Date.now()}`)}>
              用当前模型与最近样本冻结一个批次
            </button>
          </div>
          <table>
            <thead>
              <tr>
                <th>批次</th>
                <th>模型</th>
                <th>特征集</th>
                <th>状态</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {batches.map((b) => (
                <tr key={b.id}>
                  <td>{b.batch_key}</td>
                  <td>
                    {b.model.name} v{b.model.version}
                  </td>
                  <td>
                    {b.feature_set.name} v{b.feature_set.version}
                  </td>
                  <td>
                    <span className={`badge ${b.status === 'frozen' ? 'current' : 'draft'}`}>
                      {b.status}
                    </span>
                  </td>
                  <td>
                    <button className="btn ghost" onClick={() => makePackage(b.id)}>
                      生成复现包
                    </button>
                    <button className="btn ghost" onClick={() => makePackage(b.id, true)}>
                      生成（暂停封存）
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">
            已定义模型：{models.map((m) => `${m.name} v${m.version}（${m.digest.slice(0, 10)}…）`).join('，') || '—'}
          </p>
        </section>

        <section className="card">
          <h2>复现包</h2>
          <table>
            <thead>
              <tr>
                <th>清单号</th>
                <th>状态</th>
                <th>策略</th>
                <th>样本/记录</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {packages.map((p) => (
                <tr key={p.id} className={selected?.id === p.id ? 'row-warn' : ''}>
                  <td className="small">{p.package_key}</td>
                  <td>
                    <span className={`badge ${p.status === 'sealed' ? 'current' : 'pending'}`}>
                      {p.status}
                    </span>
                  </td>
                  <td>
                    v{p.policy_version} · {p.digest_algorithm}
                  </td>
                  <td>
                    {p.sample_count ?? '—'} / {p.record_count ?? '—'}
                  </td>
                  <td>
                    <button className="btn ghost" onClick={() => pick(p)}>
                      打开
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>

      {detail && (
        <section className="card">
          <div className="card-head">
            <h2>
              复现清单 {detail.package_key}{' '}
              <span className={`badge ${detail.status === 'sealed' ? 'current' : 'pending'}`}>
                {detail.status}
              </span>
            </h2>
            <div>
              {detail.status === 'building' && (
                <button className="btn warn" onClick={sealSelected}>
                  封存
                </button>
              )}{' '}
              <button className="btn" onClick={doVerify}>
                完整性校验
              </button>{' '}
              <button className="btn primary" onClick={doReplay}>
                受控重放
              </button>{' '}
              <button className="btn ghost" onClick={doDeps}>
                依赖版本
              </button>{' '}
              <button className="btn ghost" onClick={doManifest}>
                查看清单
              </button>
            </div>
          </div>
          <p className="small">
            清单摘要 <code>{detail.manifest_digest}</code> · 策略快照 v{detail.policy_version} ·
            摘要算法 {detail.digest_algorithm} · 环境指纹{' '}
            <code>{detail.environment?.fingerprint?.slice(0, 16)}…</code>
          </p>

          <div className="trace-box">
            <h3>权限审批（申请人不可自批，需管理员）</h3>
            <div className="inline-form">
              <input
                placeholder="申请事由"
                value={approvalReason}
                onChange={(e) => setApprovalReason(e.target.value)}
              />
              <button className="btn" onClick={() => requestApproval('replay')}>
                申请重放权限
              </button>
              <button className="btn" onClick={() => requestApproval('plaintext')}>
                申请明文查看
              </button>
            </div>
            <table>
              <thead>
                <tr>
                  <th>范围</th>
                  <th>申请人</th>
                  <th>状态</th>
                  <th>审批人</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {approvals.map((a) => (
                  <tr key={a.id}>
                    <td>{a.scope === 'replay' ? '受控重放' : '明文查看'}</td>
                    <td>{a.requested_by}</td>
                    <td>
                      <span className={`badge ${a.status === 'approved' ? 'current' : a.status === 'rejected' ? 'down' : 'pending'}`}>
                        {a.status}
                      </span>
                    </td>
                    <td>{a.decided_by || '—'}</td>
                    <td>
                      {a.status === 'requested' && (
                        <>
                          <button className="btn ghost" onClick={() => decide(a.id, true)}>
                            通过
                          </button>
                          <button className="btn ghost" onClick={() => decide(a.id, false)}>
                            驳回
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {verify && (
            <div className="trace-box">
              <h3>
                完整性校验：
                <span className={`badge ${CLASS_BADGE[verify.overall_classification]}`}>
                  {verify.overall_label}
                </span>{' '}
                <span className="muted small">
                  清单校验 {verify.manifest_checksum_ok ? '通过' : '失败'} · 环境指纹{' '}
                  {verify.environment.match ? '一致' : '漂移'}
                </span>
              </h3>
              {verify.digest_migration.migrated && (
                <p className="muted small">
                  摘要算法迁移：证据按封存时算法 {verify.digest_migration.package_algorithm}{' '}
                  验证，当前策略为 {verify.digest_migration.current_policy_algorithm}，
                  历史证据仍可验证（{verify.digest_migration.legacy_verification_ok ? '通过' : '失败'}）。
                </p>
              )}
              <table>
                <thead>
                  <tr>
                    <th>数据类别</th>
                    <th>记录</th>
                    <th>原文</th>
                    <th>加密副本</th>
                    <th>锁定</th>
                    <th>仅摘要</th>
                    <th>已销毁</th>
                    <th>链损坏</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(verify.availability).map(([cat, v]) => (
                    <tr key={cat}>
                      <td>{cat}</td>
                      <td>{v.records}</td>
                      <td>{v.plaintext}</td>
                      <td>{v.encrypted_copy}</td>
                      <td>{v.encrypted_locked}</td>
                      <td>{v.digest_only}</td>
                      <td>{v.destroyed}</td>
                      <td>{v.evidence_chain_broken}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <table>
                <thead>
                  <tr>
                    <th>样本</th>
                    <th>分类</th>
                    <th>封存评分</th>
                    <th>受阻输入</th>
                  </tr>
                </thead>
                <tbody>
                  {verify.samples.map((s) => (
                    <tr key={s.sample_key}>
                      <td>{s.sample_key}</td>
                      <td>
                        <span className={`badge ${CLASS_BADGE[s.classification]}`}>
                          {s.classification_label}
                        </span>
                      </td>
                      <td>
                        {s.score.value}（{s.score.decision}）
                      </td>
                      <td className="small muted">{s.blocked_records.join('，') || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {verify.affected_scores.length > 0 && (
                <p className="error small">
                  受影响评分：
                  {verify.affected_scores
                    .map((a) => `${a.sample_key}=${a.score}（${a.reasons.join('/')}）`)
                    .join('；')}
                </p>
              )}
              {verify.findings.length > 0 && (
                <ul className="small muted">
                  {verify.findings.slice(0, 10).map((f, i) => (
                    <li key={i}>
                      [{f.scope}/{f.kind}] {f.detail}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {replay && (
            <div className="trace-box">
              <h3>
                重放 #{replay.run.run_number}：
                <span className={`badge ${replay.run.status === 'consistent' ? 'current' : 'pending'}`}>
                  {replay.run.status}
                </span>{' '}
                <span className="muted small">
                  结果摘要 {replay.run.result_digest?.slice(0, 16)}… · 环境
                  {replay.run.environment_match ? '一致' : '需受控环境'}
                </span>
              </h3>
              <table>
                <thead>
                  <tr>
                    <th>样本</th>
                    <th>状态</th>
                    <th>特征一致性</th>
                    <th>封存评分</th>
                    <th>重放评分</th>
                  </tr>
                </thead>
                <tbody>
                  {replay.rows.map((r) => (
                    <tr key={r.sample_key}>
                      <td>{r.sample_key}</td>
                      <td>
                        <span className={`badge ${r.status === 'consistent' ? 'current' : 'pending'}`}>
                          {r.status}
                        </span>
                      </td>
                      <td className="small">
                        {r.features
                          ? Object.entries(r.features)
                              .map(([k, v]) => `${k}:${v.match ? '✓' : '✗'}`)
                              .join(' ')
                          : '—'}
                      </td>
                      <td>{r.sealed_score?.value}</td>
                      <td>{r.replayed_score?.value ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {deps && (
            <div className="trace-box">
              <h3>依赖版本（六类状态持有者）</h3>
              <div className="stat-row">
                {Object.entries(deps.holders).map(([name, h]) => (
                  <div className="stat" key={name}>
                    <div className="stat-title">{name}</div>
                    <div className="small">状态：{h.status}</div>
                    {h.policy_version !== undefined && (
                      <div className="small muted">策略 v{h.policy_version}</div>
                    )}
                    {h.engine_version && <div className="small muted">引擎 {h.engine_version}</div>}
                    {h.manifest_digest && (
                      <div className="small muted">封存 {h.manifest_digest.slice(0, 12)}…</div>
                    )}
                    {h.model && (
                      <div className="small muted">
                        模型 {h.model.name} v{h.model.version}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {manifest && (
            <div className="result-box">{JSON.stringify(manifest.manifest, null, 2)}</div>
          )}
        </section>
      )}

      <div className="grid-2">
        <section className="card">
          <h2>数据保留策略与到期清理</h2>
          <p className="muted small">
            不同数据类别采用不同策略：交易保留加密副本以支持日后重放；拒付标签受法定留档约束；
            账户资料仅保留可验证摘要（最小化），过期后无法逐字段复算。
          </p>
          <table>
            <thead>
              <tr>
                <th>版本</th>
                <th>状态</th>
                <th>摘要算法</th>
                <th>类别策略</th>
              </tr>
            </thead>
            <tbody>
              {policies.map((p) => (
                <tr key={p.id}>
                  <td>v{p.version}</td>
                  <td>
                    <span className={`badge ${p.status === 'active' ? 'current' : ''}`}>{p.status}</span>
                  </td>
                  <td>{p.digest_algorithm}</td>
                  <td className="small">
                    {Object.entries(p.rules).map(([cat, r]) => (
                      <div key={cat}>
                        <strong>{cat}</strong>：原文 {r.plaintext_days} 天 · 副本{' '}
                        {r.protected_copy_days} 天 · 法定 {r.legal_hold_days} 天 · {r.strategy}
                        <div className="muted">{r.rationale}</div>
                      </div>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="inline-form">
            <label>
              清理基准时间（as_of）
              <input
                type="datetime-local"
                step="1"
                value={sweepAsOf}
                onChange={(e) => setSweepAsOf(e.target.value)}
              />
            </label>
            <button className="btn warn" onClick={runSweep}>
              执行保留期清理
            </button>
          </div>
          <div className="inline-form">
            <label>
              交易原文保留天数
              <input value={policyDays} onChange={(e) => setPolicyDays(e.target.value)} />
            </label>
            <label>
              摘要算法
              <select value={policyAlg} onChange={(e) => setPolicyAlg(e.target.value)}>
                <option value="sha256">sha256</option>
                <option value="sha3_256">sha3_256</option>
                <option value="sha512">sha512</option>
              </select>
            </label>
            <button className="btn" onClick={createPolicy}>
              发布新策略版本
            </button>
          </div>
          <table>
            <thead>
              <tr>
                <th>清理</th>
                <th>基准</th>
                <th>原文过期</th>
                <th>副本销毁</th>
              </tr>
            </thead>
            <tbody>
              {sweeps.map((s) => (
                <tr key={s.id}>
                  <td>#{s.id}</td>
                  <td className="small">{s.as_of}</td>
                  <td>{s.records_expired}</td>
                  <td>{s.archives_destroyed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="card">
          <h2>证据密钥与来源补传</h2>
          <div className="action-list">
            <button className="btn" onClick={rotateKey}>
              轮换证据密钥（历史包仍用旧版本密钥解密）
            </button>
          </div>
          <table>
            <thead>
              <tr>
                <th>版本</th>
                <th>状态</th>
                <th>算法</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id}>
                  <td>v{k.version}</td>
                  <td>
                    <span className={`badge ${k.status === 'active' ? 'current' : k.status === 'revoked' ? 'down' : ''}`}>
                      {k.status}
                    </span>
                  </td>
                  <td className="small">{k.algorithm}</td>
                  <td>
                    {k.status !== 'revoked' ? (
                      <button className="btn ghost" onClick={() => revokeKey(k)}>
                        撤销
                      </button>
                    ) : (
                      <button className="btn ghost" onClick={() => restoreKey(k)}>
                        恢复
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3>模拟来源补传修改</h3>
          <form onSubmit={applyCorrection} className="inline-form">
            <label>
              交易号
              <input
                required
                value={corr.record_id}
                placeholder="如 TXN-xxxx"
                onChange={(e) => setCorr({ ...corr, record_id: e.target.value })}
              />
            </label>
            <label>
              新金额
              <input
                value={corr.amount}
                onChange={(e) => setCorr({ ...corr, amount: e.target.value })}
              />
            </label>
            <button className="btn warn">应用补传</button>
          </form>
          <p className="muted small">
            补传修改的是当前规范记录；已封存证据的摘要不变，校验时会报告“证据链损坏”并列出受影响评分。
          </p>
          {corrections.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>记录</th>
                  <th>旧摘要</th>
                  <th>新摘要</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {corrections.map((c) => (
                  <tr key={c.id}>
                    <td>{c.record_id}</td>
                    <td className="small">{c.old_digest.slice(0, 10)}…</td>
                    <td className="small">{c.new_digest.slice(0, 10)}…</td>
                    <td className="small">{c.created_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <section className="card">
        <h2>记录可用状态说明</h2>
        <p className="small muted">
          {Object.entries(STATE_LABEL)
            .map(([k, v]) => `${k} = ${v}`)
            .join('；')}
          。分类：{Object.entries(CLASS_LABEL).map(([k, v]) => `${k} = ${v}`).join('；')}。
        </p>
      </section>
    </div>
  )
}
