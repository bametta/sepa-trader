import { useState, useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from 'react-query'
import { fetchWeeklyPlan, fetchScreenerStatus, runScreener, syncTradingView, updatePlanStatus } from '../api/client'

const SIGNAL_STYLE = {
  BREAKOUT:       'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30',
  PULLBACK_EMA20: 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30',
  PULLBACK_EMA50: 'bg-blue-500/20 text-blue-300 border border-blue-500/30',
  STAGE2_WATCH:   'bg-yellow-500/20 text-yellow-300 border border-yellow-500/30',
  NO_SETUP:       'bg-red-500/20 text-red-400 border border-red-500/30',
}

const STATUS_STYLE = {
  PENDING:  'bg-slate-700 text-slate-300',
  EXECUTED: 'bg-emerald-500/20 text-emerald-300',
  PARTIAL:  'bg-yellow-500/20 text-yellow-300',
  SKIPPED:  'bg-slate-600 text-slate-400 line-through',
}

export default function WeeklyPlan() {
  const qc = useQueryClient()
  const [running, setRunning] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [msg, setMsg]         = useState(null)
  const [msgType, setMsgType] = useState('info') // info | error
  const prevStatusRef         = useRef(null)

  // Plan data — refetch frequently when screener is running
  const { data: plan = [], isLoading, isError } = useQuery('weeklyPlan', fetchWeeklyPlan, {
    refetchInterval: 30000,
  })

  // Status — poll every 5s while running, every 60s otherwise
  const { data: status } = useQuery('screenerStatus', fetchScreenerStatus, {
    refetchInterval: (data) => data?.status === 'running' ? 5000 : 60000,
  })

  // React to screener status transitions
  useEffect(() => {
    const prev = prevStatusRef.current
    const curr = status?.status
    prevStatusRef.current = curr

    if (prev === 'running' && curr === 'done') {
      setRunning(false)
      qc.invalidateQueries('weeklyPlan')
      const summary = status?.last_run_summary || `Screener complete — ${status?.count ?? 0} stocks selected.`
      setMsg(summary)
      setMsgType('info')
    } else if (prev === 'running' && curr === 'error') {
      setRunning(false)
      setMsg(`Screener error: ${status?.error || 'Unknown error — check docker logs.'}`)
      setMsgType('error')
    }
  }, [status?.status])

  // Keep running=true in sync with DB status on mount (e.g. page reload mid-run)
  useEffect(() => {
    if (status?.status === 'running' && !running) setRunning(true)
  }, [status?.status])

  async function handleRunScreener() {
    setMsg(null)
    setRunning(true)
    try {
      await runScreener()
      setMsg('Scanning ~120 stocks… this takes 1–3 minutes.')
      setMsgType('info')
    } catch (err) {
      setRunning(false)
      setMsg(err?.response?.data?.detail || 'Failed to start screener.')
      setMsgType('error')
    }
  }

  async function handleSyncTV() {
    setSyncing(true)
    setMsg(null)
    try {
      const res = await syncTradingView()
      setMsg(res.message || 'Syncing to TradingView…')
      setMsgType('info')
      setTimeout(() => setMsg(null), 8000)
    } catch (err) {
      setMsg(err?.response?.data?.detail || 'TV sync failed — add credentials in Settings.')
      setMsgType('error')
    } finally {
      setSyncing(false)
    }
  }

  async function handleStatus(symbol, newStatus) {
    await updatePlanStatus(symbol, newStatus)
    qc.invalidateQueries('weeklyPlan')
  }

  const weekStart = plan[0]?.week_start
    ? new Date(plan[0].week_start).toLocaleDateString('en-US', {
        month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC',
      })
    : null

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text-slate-100">Weekly Trading Plan</h3>
          {weekStart && <p className="text-xs text-slate-500 mt-0.5">Week of {weekStart}</p>}
        </div>
        <div className="flex gap-2">
          <button
            onClick={handleSyncTV}
            disabled={syncing || plan.length === 0}
            className="px-3 py-1.5 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 disabled:opacity-40 transition-colors"
            title="Push to TradingView weekly_picks"
          >
            {syncing ? 'Syncing…' : 'Sync TV'}
          </button>
          <button
            onClick={handleRunScreener}
            disabled={running}
            className="px-4 py-1.5 rounded-lg text-sm font-medium bg-accent hover:bg-indigo-500 text-white disabled:opacity-50 transition-colors flex items-center gap-2"
          >
            {running && (
              <span className="inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full animate-spin" />
            )}
            {running ? 'Scanning…' : 'Run Screener'}
          </button>
        </div>
      </div>

      {/* Message banner */}
      {msg && (
        <div className={`border rounded-xl px-4 py-2.5 text-sm ${
          msgType === 'error'
            ? 'bg-red-500/10 border-red-500/30 text-red-300'
            : 'bg-indigo-500/10 border-indigo-500/30 text-indigo-300'
        }`}>
          {msg}
        </div>
      )}

      {/* Last-run summary bar (shown when not showing an active message) */}
      {!msg && !running && status?.last_run_summary && (
        <div className="bg-slate-800/50 border border-border rounded-xl px-4 py-2 text-xs text-slate-400">
          {status.last_run_summary}
        </div>
      )}

      {/* Content */}
      {isLoading ? (
        <div className="space-y-3">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="bg-card border border-border rounded-xl h-20 animate-pulse" />
          ))}
        </div>
      ) : isError ? (
        <div className="bg-card border border-red-500/30 rounded-xl p-10 text-center text-red-400 text-sm">
          Failed to load weekly plan — check backend logs.
        </div>
      ) : plan.length === 0 ? (
        <div className="bg-card border border-border rounded-xl p-12 text-center space-y-2 text-slate-500">
          {running ? (
            <>
              <p className="font-medium text-slate-300">Screener running…</p>
              <p className="text-xs">Analyzing stocks — results will appear automatically when done.</p>
            </>
          ) : (
            <>
              <p className="font-medium">No weekly plan yet.</p>
              <p className="text-xs">
                {status?.last_run_summary
                  ? status.last_run_summary
                  : 'Click "Run Screener" to scan stocks. Runs automatically every Sunday at 8 PM ET.'}
              </p>
              {status?.error && (
                <p className="text-xs text-red-400 mt-2">Last error: {status.error}</p>
              )}
            </>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          {plan.map(row => (
            <PlanCard key={row.symbol} row={row} onStatusChange={handleStatus} />
          ))}
        </div>
      )}
    </div>
  )
}

function PlanCard({ row, onStatusChange }) {
  const [expanded, setExpanded] = useState(false)

  const signalCls = SIGNAL_STYLE[row.signal] || SIGNAL_STYLE.STAGE2_WATCH
  const statusCls = STATUS_STYLE[row.status] || STATUS_STYLE.PENDING
  const rr = row.target1 && row.entry_price && row.stop_price
    ? ((row.target1 - row.entry_price) / (row.entry_price - row.stop_price)).toFixed(1)
    : '—'

  return (
    <div className={`bg-card border border-border rounded-xl overflow-hidden ${row.status === 'SKIPPED' ? 'opacity-50' : ''}`}>
      <div
        className="flex items-center gap-3 px-4 py-3 cursor-pointer hover:bg-white/5"
        onClick={() => setExpanded(e => !e)}
      >
        <span className="w-6 h-6 rounded-full bg-slate-700 text-slate-300 text-xs flex items-center justify-center font-bold flex-shrink-0">
          {row.rank}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-slate-100">{row.symbol}</span>
            <span className={`text-xs px-1.5 py-0.5 rounded-md ${signalCls}`}>{row.signal}</span>
          </div>
          <div className="flex items-center gap-3 mt-0.5 text-xs text-slate-400">
            <span>Score <strong className="text-slate-200">{row.score}/8</strong></span>
            <span>Entry <strong className="text-slate-200">${Number(row.entry_price).toFixed(2)}</strong></span>
            <span>Stop <strong className="text-red-400">${Number(row.stop_price).toFixed(2)}</strong></span>
            <span>R:R <strong className="text-emerald-400">{rr}x</strong></span>
          </div>
        </div>
        <div className="text-right flex-shrink-0 space-y-1">
          <div className="text-sm font-medium text-slate-200">{row.position_size} sh</div>
          <span className={`text-xs px-2 py-0.5 rounded-full ${statusCls}`}>{row.status}</span>
        </div>
      </div>

      {expanded && (
        <div className="border-t border-border px-4 py-3 space-y-3">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
            <Stat label="Entry"        value={`$${Number(row.entry_price).toFixed(2)}`} />
            <Stat label="Stop"         value={`$${Number(row.stop_price).toFixed(2)}`}  color="text-red-400" />
            <Stat label="Target 1 (2R)"value={`$${Number(row.target1).toFixed(2)}`}    color="text-emerald-400" />
            <Stat label="Target 2 (3R)"value={`$${Number(row.target2).toFixed(2)}`}    color="text-emerald-300" />
            <Stat label="Shares"       value={row.position_size} />
            <Stat label="Risk $"       value={`$${Number(row.risk_amount).toFixed(0)}`} />
            <Stat label="Mode"         value={row.mode?.toUpperCase()} />
            <Stat label="R:R"          value={`${rr}x`} />
          </div>
          {row.rationale && (
            <p className="text-xs text-slate-400 leading-relaxed">{row.rationale}</p>
          )}
          <div className="flex gap-2 pt-1">
            {['PENDING', 'EXECUTED', 'PARTIAL', 'SKIPPED'].map(s => (
              <button
                key={s}
                onClick={() => onStatusChange(row.symbol, s)}
                className={`text-xs px-2 py-1 rounded-md transition-colors ${
                  row.status === s
                    ? 'bg-accent text-white'
                    : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function Stat({ label, value, color = 'text-slate-200' }) {
  return (
    <div>
      <div className="text-slate-500 mb-0.5">{label}</div>
      <div className={`font-medium ${color}`}>{value}</div>
    </div>
  )
}
