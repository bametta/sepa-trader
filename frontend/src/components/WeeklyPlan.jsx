import { useState } from 'react'
import { useQuery, useQueryClient } from 'react-query'
import { fetchWeeklyPlan, runScreener, syncTradingView, updatePlanStatus } from '../api/client'

const SIGNAL_STYLE = {
  BREAKOUT:      'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30',
  PULLBACK_EMA20:'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30',
  PULLBACK_EMA50:'bg-blue-500/20 text-blue-300 border border-blue-500/30',
  STAGE2_WATCH:  'bg-yellow-500/20 text-yellow-300 border border-yellow-500/30',
  NO_SETUP:      'bg-red-500/20 text-red-400 border border-red-500/30',
}

const STATUS_STYLE = {
  PENDING:  'bg-slate-700 text-slate-300',
  EXECUTED: 'bg-emerald-500/20 text-emerald-300',
  PARTIAL:  'bg-yellow-500/20 text-yellow-300',
  SKIPPED:  'bg-slate-600 text-slate-400 line-through',
}

export default function WeeklyPlan() {
  const qc = useQueryClient()
  const [running, setRunning]   = useState(false)
  const [syncing, setSyncing]   = useState(false)
  const [runMsg, setRunMsg]     = useState(null)

  const { data: plan = [], isLoading, isError } = useQuery('weeklyPlan', fetchWeeklyPlan, {
    refetchInterval: 30000,
  })

  async function handleRunScreener() {
    setRunning(true)
    setRunMsg(null)
    try {
      const res = await runScreener()
      setRunMsg(res.message || 'Screener started — results ready in ~2 min.')
      setTimeout(() => {
        qc.invalidateQueries('weeklyPlan')
        setRunMsg(null)
      }, 90000)
    } catch {
      setRunMsg('Failed to start screener.')
    } finally {
      setRunning(false)
    }
  }

  async function handleSyncTV() {
    setSyncing(true)
    setRunMsg(null)
    try {
      const res = await syncTradingView()
      setRunMsg(res.message || `Syncing ${res.symbols?.length || ''} symbols to TradingView weekly_picks…`)
      setTimeout(() => setRunMsg(null), 8000)
    } catch (err) {
      const msg = err?.response?.data?.detail || 'TradingView sync failed — check TV credentials in Settings.'
      setRunMsg(msg)
    } finally {
      setSyncing(false)
    }
  }

  async function handleStatus(symbol, status) {
    await updatePlanStatus(symbol, status)
    qc.invalidateQueries('weeklyPlan')
  }

  const weekStart = plan[0]?.week_start
    ? new Date(plan[0].week_start).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
    : null

  return (
    <div className="space-y-4">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text-slate-100">Weekly Trading Plan</h3>
          {weekStart && (
            <p className="text-xs text-slate-500 mt-0.5">Week of {weekStart}</p>
          )}
        </div>
        <div className="flex gap-2">
          <button
            onClick={handleSyncTV}
            disabled={syncing || plan.length === 0}
            className="px-3 py-1.5 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 disabled:opacity-40 transition-colors"
            title="Push current plan to TradingView weekly_picks watchlist"
          >
            {syncing ? 'Syncing…' : 'Sync TV'}
          </button>
          <button
            onClick={handleRunScreener}
            disabled={running}
            className="px-4 py-1.5 rounded-lg text-sm font-medium bg-accent hover:bg-indigo-500 text-white disabled:opacity-50 transition-colors"
          >
            {running ? 'Running…' : 'Run Screener'}
          </button>
        </div>
      </div>

      {runMsg && (
        <div className="bg-indigo-500/10 border border-indigo-500/30 rounded-xl px-4 py-2.5 text-sm text-indigo-300">
          {runMsg}
        </div>
      )}

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
        <div className="bg-card border border-border rounded-xl p-12 text-center text-slate-500">
          <p className="mb-2">No weekly plan yet.</p>
          <p className="text-xs">Click "Run Screener" to generate this week's top 10 candidates, or wait for the automatic Sunday 8 PM run.</p>
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
    <div className={`bg-card border border-border rounded-xl overflow-hidden transition-all ${row.status === 'SKIPPED' ? 'opacity-50' : ''}`}>
      {/* Main row */}
      <div
        className="flex items-center gap-3 px-4 py-3 cursor-pointer hover:bg-white/5"
        onClick={() => setExpanded(e => !e)}
      >
        {/* Rank badge */}
        <span className="w-6 h-6 rounded-full bg-slate-700 text-slate-300 text-xs flex items-center justify-center font-bold flex-shrink-0">
          {row.rank}
        </span>

        {/* Symbol + signal */}
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

        {/* Right side: shares + status */}
        <div className="text-right flex-shrink-0 space-y-1">
          <div className="text-sm font-medium text-slate-200">{row.position_size} sh</div>
          <span className={`text-xs px-2 py-0.5 rounded-full ${statusCls}`}>{row.status}</span>
        </div>
      </div>

      {/* Expanded details */}
      {expanded && (
        <div className="border-t border-border px-4 py-3 space-y-3">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
            <Stat label="Entry"   value={`$${Number(row.entry_price).toFixed(2)}`} />
            <Stat label="Stop"    value={`$${Number(row.stop_price).toFixed(2)}`} color="text-red-400" />
            <Stat label="Target 1 (2R)" value={`$${Number(row.target1).toFixed(2)}`} color="text-emerald-400" />
            <Stat label="Target 2 (3R)" value={`$${Number(row.target2).toFixed(2)}`} color="text-emerald-300" />
            <Stat label="Shares"  value={row.position_size} />
            <Stat label="Risk $"  value={`$${Number(row.risk_amount).toFixed(0)}`} />
            <Stat label="Mode"    value={row.mode?.toUpperCase()} />
            <Stat label="R:R"     value={`${rr}x`} />
          </div>

          {row.rationale && (
            <p className="text-xs text-slate-400 leading-relaxed">{row.rationale}</p>
          )}

          {/* Status actions */}
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
