import React, { useState } from 'react'
import { api } from '../api.js'

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('analyst')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const data = await api.post('/api/auth/login', { username, password })
      const { setToken } = await import('../api.js')
      setToken(data.token)
      onLogin(data.user)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <form className="card login-card" onSubmit={submit}>
        <h1>反欺诈特征平台</h1>
        <p className="muted">特征计算与在线服务 · 训练/在线同口径</p>
        <label>
          用户名
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
        </label>
        <label>
          密码
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && <div className="error">{error}</div>}
        <button className="btn primary" disabled={busy}>
          {busy ? '登录中…' : '登录'}
        </button>
        <p className="muted small">测试账号：analyst / analyst123，admin / admin123</p>
      </form>
    </div>
  )
}
