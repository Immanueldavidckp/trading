import { useState, useEffect, useRef, useCallback } from 'react'
import './App.css'

const API = 'http://127.0.0.1:8000'
const WS = 'ws://127.0.0.1:8000/ws/prices'
const SYMBOL = 'RELIANCE-EQ'
const MAX_ROWS = 500

function fmtTime(iso) {
  // iso like 2026-05-29T10:04:01.911 -> 10:04:01.911
  if (!iso) return '-'
  const t = iso.split('T')[1] || iso
  return t
}

function App() {
  const [rows, setRows] = useState([])
  const [connected, setConnected] = useState(false)
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)
  const wsRef = useRef(null)
  const seenIds = useRef(new Set())

  const addRows = useCallback((incoming) => {
    setRows((prev) => {
      const fresh = incoming.filter((r) => !seenIds.current.has(r.id))
      fresh.forEach((r) => seenIds.current.add(r.id))
      if (fresh.length === 0) return prev
      // newest first
      const merged = [...fresh.reverse(), ...prev]
      return merged.slice(0, MAX_ROWS)
    })
  }, [])

  // WebSocket connection (auto-reconnect)
  useEffect(() => {
    let stop = false
    let retry

    const connect = () => {
      if (stop) return
      const ws = new WebSocket(`${WS}?tsym=${SYMBOL}&backlog=40`)
      wsRef.current = ws

      ws.onopen = () => setConnected(true)
      ws.onclose = () => {
        setConnected(false)
        if (!stop) retry = setTimeout(connect, 1500)
      }
      ws.onerror = () => ws.close()
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data)
          if (msg.rows && msg.rows.length) addRows(msg.rows)
        } catch (e) { /* ignore */ }
      }
    }

    connect()
    return () => {
      stop = true
      clearTimeout(retry)
      if (wsRef.current) wsRef.current.close()
    }
  }, [addRows])

  // Poll status periodically
  useEffect(() => {
    const tick = async () => {
      try {
        const r = await fetch(`${API}/api/yahoo/status`)
        setStatus(await r.json())
      } catch (e) { /* ignore */ }
    }
    tick()
    const id = setInterval(tick, 3000)
    return () => clearInterval(id)
  }, [])

  const startPoller = async () => {
    setBusy(true)
    try {
      await fetch(`${API}/api/yahoo/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tsym: SYMBOL, interval_sec: 1 }),
      })
    } finally { setBusy(false) }
  }

  const stopPoller = async () => {
    setBusy(true)
    try { await fetch(`${API}/api/yahoo/stop`, { method: 'POST' }) }
    finally { setBusy(false) }
  }

  const backfill = async (rng) => {
    setBusy(true)
    try { await fetch(`${API}/api/yahoo/backfill?tsym=${SYMBOL}&rng=${rng}`, { method: 'POST' }) }
    finally { setBusy(false) }
  }

  const latest = rows[0]
  const up = latest && latest.change >= 0

  return (
    <div className="wrap">
      <header className="topbar">
        <div>
          <h1>{SYMBOL}</h1>
          <span className="src">source: Yahoo Finance (free) · ~10-12s updates · no bid/ask depth</span>
        </div>
        <div className={`conn ${connected ? 'on' : 'off'}`}>
          {connected ? '● LIVE' : '○ disconnected'}
        </div>
      </header>

      {latest && (
        <div className={`ltp ${up ? 'green' : 'red'}`}>
          <span className="price">₹{Number(latest.lp).toFixed(2)}</span>
          <span className="chg">
            {up ? '▲' : '▼'} {latest.change} ({latest.change_pct}%)
          </span>
          <span className="ts">@ {fmtTime(latest.received_at)}</span>
        </div>
      )}

      <div className="controls">
        <button onClick={startPoller} disabled={busy}>Start</button>
        <button onClick={stopPoller} disabled={busy}>Stop</button>
        <button onClick={() => backfill('1d')} disabled={busy}>Backfill 1d</button>
        <button onClick={() => backfill('5d')} disabled={busy}>Backfill 5d</button>
        {status && (
          <span className="stat">
            polls: {status.poll_count ?? 0} · changes: {status.changes_recorded ?? 0} ·
            {status.running ? ' running' : ' stopped'}
          </span>
        )}
      </div>

      <div className="tablewrap">
        <table>
          <thead>
            <tr>
              <th>Time (capture)</th>
              <th>Bar time</th>
              <th>Price ₹</th>
              <th>Change</th>
              <th>%</th>
              <th>Volume</th>
              <th>Src</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className={r.change >= 0 ? 'g' : 'r'}>
                <td className="mono">{fmtTime(r.received_at)}</td>
                <td className="mono dim">{r.bar_time}</td>
                <td className="mono">{Number(r.lp).toFixed(2)}</td>
                <td className="mono">{r.change}</td>
                <td className="mono">{r.change_pct}%</td>
                <td className="mono dim">{r.volume?.toLocaleString?.() ?? r.volume}</td>
                <td className="dim">{r.source?.replace('yahoo', 'Y')}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan="7" className="empty">Waiting for price changes…</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <footer className="foot">
        Showing latest {rows.length} price changes · stored in <code>local_data/ticks.db</code> (table <code>price_changes</code>)
      </footer>
    </div>
  )
}

export default App
