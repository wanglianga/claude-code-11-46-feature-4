import React, { useEffect, useState } from 'react'
import { api, getToken, setToken } from './api.js'
import Login from './pages/Login.jsx'
import Catalog from './pages/Catalog.jsx'
import Compute from './pages/Compute.jsx'
import Online from './pages/Online.jsx'
import Training from './pages/Training.jsx'
import Simulate from './pages/Simulate.jsx'
import Replay from './pages/Replay.jsx'

const TABS = [
  { key: 'catalog', label: '特征目录' },
  { key: 'compute', label: '计算状态' },
  { key: 'online', label: '在线查询' },
  { key: 'training', label: '训练样本' },
  { key: 'replay', label: '复现与留档' },
  { key: 'simulate', label: '数据模拟' },
]

export default function App() {
  const [user, setUser] = useState(null)
  const [checking, setChecking] = useState(!!getToken())
  const [tab, setTab] = useState('catalog')

  useEffect(() => {
    const onUnauthorized = () => setUser(null)
    window.addEventListener('afp:unauthorized', onUnauthorized)
    return () => window.removeEventListener('afp:unauthorized', onUnauthorized)
  }, [])

  useEffect(() => {
    if (!getToken()) return
    api
      .get('/api/auth/me')
      .then((u) => setUser(u))
      .catch(() => {})
      .finally(() => setChecking(false))
  }, [])

  if (checking) return <div className="center muted">加载中…</div>
  if (!user) return <Login onLogin={(u) => setUser(u)} />

  return (
    <div className="layout">
      <header className="topbar">
        <div className="brand">
          反欺诈特征平台
          <span className="brand-sub">特征计算与在线服务 · 离线/在线同口径</span>
        </div>
        <nav>
          {TABS.map((t) => (
            <button
              key={t.key}
              className={tab === t.key ? 'tab active' : 'tab'}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="userbox">
          <span className="muted">{user.username}（{user.role}）</span>
          <button
            className="btn ghost"
            onClick={() => {
              setToken(null)
              setUser(null)
            }}
          >
            退出
          </button>
        </div>
      </header>
      <main>
        {tab === 'catalog' && <Catalog />}
        {tab === 'compute' && <Compute />}
        {tab === 'online' && <Online />}
        {tab === 'training' && <Training />}
        {tab === 'replay' && <Replay />}
        {tab === 'simulate' && <Simulate />}
      </main>
    </div>
  )
}
