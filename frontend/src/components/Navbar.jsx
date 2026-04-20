import { useState } from 'react'
import { runMonitor } from '../api/client'
import { useQuery, useQueryClient } from 'react-query'
import { fetchAccount } from '../api/client'

export default function Navbar({ onModeChange }) {
  const qc                = useQueryClient()
  const [running, setRunning] = useState(false)
  const [result,  setResult]  = useState(null)

  // Pull mode from the already-cached account query — zero extra requests
  const { data: account } = useQuery('account', () => fetchAccount(), {
    refetchInterval: 30000,
  })
  const mode     = account?.mode ?? 'paper'
  const isPaper  = mode === 'paper'

  async function handleRun() {
    setRunning(true)
    setResult(null)
    try {
      const res = await runMonitor()
      qc.invalidateQueries()
      setResult(res)
      setTimeout(() => setResult(null), 8000)
    } catch (e) {
      setResult({ status: 'error', error: e.message })
    } finally {
      setRunning(false)
    }
  }

  async function handleModeSwitch() {
    if (isPaper) {
      const confirmed = window.confirm(
        '⚠️ Switch to LIVE trading?\n\nReal money will be used. Make sure your live Alpaca credentials are configured.'
      )
      if (!confirmed) return
    }
    onModeChange && onModeChange(isPaper ? 'live' : 'paper')
  }

  function resultBanner() {
    if (!result) return null
    if (result.status === 'market_closed')
      return (
        <span className="text-xs text-yellow-400 bg-yellow-500/10 border border-yellow-500/20 px-3 py-1 rounded-lg">
          Market closed — monitor will auto-run when open
        </span>
      )
    if (result.status === 'error')
      return (
        <span className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 px-3 py-1 rounded-lg">
          {result.error}
        </span>
      )
    if (result.status === 'ok') {
      const lost = result.stage2_lost?.length  || 0
      const brk  = result.new_breakouts?.length || 0
      const msg  = lost ? `${lost} Stage 2 lost` : brk ? `${brk} breakout(s) detected` : 'All positions healthy'
      const color = lost ? 'red' : brk ? 'emerald' : 'green'
      return (
        <span className={`text-xs text-${color}-400 bg-${color}-500/10 border border-${color}-500/20 px-3 py-1 rounded-lg`}>
          {msg} — P&L {result.day_pnl >= 0 ? '+' : ''}${result.day_pnl?.toFixed(2)}
        </span>
      )
    }
    return null
  }

  return (
    <nav className="border-b border-border bg-card px-6 py-4 flex items-center justify-between">
      {/* Left — logo + mode badge */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-3">
          <span className="text-2xl">📈</span>
          <div>
            <h1 className="text-lg font-bold text-slate-100 leading-none">SEPA Trader</h1>
            <p className="text-xs text-slate-400">Minervini Stage 2 Monitor</p>
          </div>
        </div>

        {/* Persistent mode indicator — always visible */}
        <button
          onClick={handleModeSwitch}
          title={`Active: ${mode.toUpperCase()} — click to switch`}
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-bold transition-colors ${
            isPaper
              ? 'bg-blue-500/15 text-blue-400 border-blue-500/40 hover:bg-blue-500/25'
              : 'bg-orange-500/15 text-orange-400 border-orange-500/40 hover:bg-orange-500/25 animate-pulse'
          }`}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${isPaper ? 'bg-blue-400' : 'bg-orange-400'}`} />
          {isPaper ? 'PAPER' : '⚡ LIVE'}
        </button>
      </div>

      {/* Right — result banner + run button */}
      <div className="flex items-center gap-4">
        {resultBanner()}
        <button
          onClick={handleRun}
          disabled={running}
          className={`px-4 py-2 disabled:opacity-50 text-white text-sm font-semibold rounded-lg transition-colors ${
            isPaper
              ? 'bg-accent hover:bg-indigo-500'
              : 'bg-orange-600 hover:bg-orange-500'
          }`}
        >
          {running ? 'Running…' : 'Run Monitor'}
        </button>
      </div>
    </nav>
  )
}