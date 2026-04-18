import { useState } from 'react'
import { runMonitor } from '../api/client'
import { useQueryClient } from 'react-query'

export default function Navbar({ lastRun }) {
  const qc      = useQueryClient()
  const [running, setRunning] = useState(false)
  const [result,  setResult]  = useState(null)

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
    } finally { setRunning(false) }
  }

  function resultBanner() {
    if (!result) return null
    if (result.status === 'market_closed')
      return <span className="text-xs text-yellow-400 bg-yellow-500/10 border border-yellow-500/20 px-3 py-1 rounded-lg">Market closed — monitor will auto-run when open</span>
    if (result.status === 'error')
      return <span className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 px-3 py-1 rounded-lg">{result.error}</span>
    if (result.status === 'ok') {
      const lost = result.stage2_lost?.length || 0
      const brk  = result.new_breakouts?.length || 0
      const msg  = lost ? `${lost} Stage 2 lost` : brk ? `${brk} breakout(s) detected` : 'All positions healthy'
      const color = lost ? 'red' : brk ? 'emerald' : 'green'
      return <span className={`text-xs text-${color}-400 bg-${color}-500/10 border border-${color}-500/20 px-3 py-1 rounded-lg`}>{msg} — P&L ${result.day_pnl >= 0 ? '+' : ''}${result.day_pnl?.toFixed(2)}</span>
    }
    return null
  }

  return (
    <nav className="border-b border-border bg-card px-6 py-4 flex items-center justify-between">
      <div className="flex items-center gap-3">
        <span className="text-2xl">📈</span>
        <div>
          <h1 className="text-lg font-bold text-slate-100 leading-none">SEPA Trader</h1>
          <p className="text-xs text-slate-400">Minervini Stage 2 Monitor</p>
        </div>
      </div>
      <div className="flex items-center gap-4">
        {resultBanner()}
        <button
          onClick={handleRun}
          disabled={running}
          className="px-4 py-2 bg-accent hover:bg-indigo-500 disabled:opacity-50 text-white text-sm font-semibold rounded-lg transition-colors"
        >
          {running ? 'Running…' : 'Run Monitor'}
        </button>
      </div>
    </nav>
  )
}
