/**
 * Dual Momentum (GEM) Strategy Tab
 *
 * Sections:
 *   1. Market Environment card
 *   2. Momentum bars  (SPY / EFA / AGG / BIL)
 *   3. AI Decision card
 *   4. Current position card
 *   5. Action row: Run Signal + Execute + auto-execute toggle
 *   6. Signal history table
 *   7. Strategy settings accordion
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from 'react-query'
import {
  fetchMarketEnvironment,
  fetchDMSignal,
  evaluateDualMomentum,
  executeDualMomentum,
  fetchDMPosition,
  fetchDMHistory,
  fetchDMConfig,
  updateDMConfig,
} from '../api/client'

// ── tiny helpers ──────────────────────────────────────────────────────────────

function pct(v) {
  if (v == null || v === '') return '—'
  const n = parseFloat(v)
  return `${n >= 0 ? '+' : ''}${(n * 100).toFixed(2)}%`
}

function fmt(v, decimals = 2) {
  if (v == null) return '—'
  return parseFloat(v).toFixed(decimals)
}

function currency(v) {
  if (v == null) return '—'
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(v)
}

// ── colour maps ───────────────────────────────────────────────────────────────

const ENV_COLORS = {
  BULL:           'text-emerald-400 bg-emerald-400/10 border-emerald-400/30',
  BULL_VOLATILE:  'text-yellow-400  bg-yellow-400/10  border-yellow-400/30',
  CORRECTION:     'text-orange-400  bg-orange-400/10  border-orange-400/30',
  BEAR:           'text-red-400     bg-red-400/10     border-red-400/30',
  TRANSITIONAL:   'text-sky-400     bg-sky-400/10     border-sky-400/30',
  UNKNOWN:        'text-slate-400   bg-slate-400/10   border-slate-400/30',
}

const DECISION_COLORS = {
  EXECUTE: 'text-emerald-400 bg-emerald-400/10 border-emerald-400/30',
  HOLD:    'text-yellow-400  bg-yellow-400/10  border-yellow-400/30',
  WAIT:    'text-slate-400   bg-slate-700/40   border-slate-600/30',
}

const RISK_COLORS = {
  LOW:    'text-emerald-400 bg-emerald-400/10',
  MEDIUM: 'text-yellow-400  bg-yellow-400/10',
  HIGH:   'text-red-400     bg-red-400/10',
}

// ── sub-components ────────────────────────────────────────────────────────────

function Skeleton({ className = 'h-4 w-24' }) {
  return <div className={`${className} bg-slate-700 rounded animate-pulse`} />
}

function Badge({ label, colorClass }) {
  return (
    <span className={`inline-block px-2.5 py-0.5 rounded-full text-xs font-semibold border ${colorClass}`}>
      {label}
    </span>
  )
}

function MarketEnvCard({ env, loading }) {
  if (loading) {
    return (
      <div className="bg-card border border-border rounded-xl p-5 space-y-3">
        <Skeleton className="h-5 w-36" />
        <div className="grid grid-cols-2 gap-3">
          {[...Array(4)].map((_, i) => <Skeleton key={i} className="h-4 w-full" />)}
        </div>
      </div>
    )
  }
  if (!env) return null

  const colorClass = ENV_COLORS[env.environment] || ENV_COLORS.UNKNOWN

  return (
    <div className="bg-card border border-border rounded-xl p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Market Environment</h3>
        <Badge label={env.environment} colorClass={colorClass} />
      </div>
      <p className="text-xs text-slate-500 italic">{env.description}</p>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-center">
        <Stat label="SPY Price" value={`$${fmt(env.spy_price)}`} />
        <Stat label="200-day SMA" value={`$${fmt(env.spy_200sma)}`} />
        <Stat label="SPY 20d Return" value={`${fmt(env.spy_20d_return)}%`}
              color={env.spy_20d_return >= 0 ? 'text-emerald-400' : 'text-red-400'} />
        <Stat label="VIX" value={fmt(env.vix)}
              color={env.vix > 30 ? 'text-red-400' : env.vix > 20 ? 'text-yellow-400' : 'text-emerald-400'} />
      </div>
    </div>
  )
}

function Stat({ label, value, color = 'text-slate-100' }) {
  return (
    <div className="bg-surface rounded-lg p-3">
      <p className="text-xs text-slate-500 mb-1">{label}</p>
      <p className={`text-sm font-bold ${color}`}>{value}</p>
    </div>
  )
}

function MomentumBars({ momentum }) {
  if (!momentum) return null

  const order = ['SPY', 'EFA', 'AGG', 'BIL']
  const labels = { SPY: 'US Equities (SPY)', EFA: 'Intl Equities (EFA)', AGG: 'Bonds (AGG)', BIL: 'T-Bills (BIL)' }
  const values = order.map(k => ({ key: k, label: labels[k], val: momentum[k] ?? 0 }))

  const max = Math.max(...values.map(v => Math.abs(v.val)), 0.01)

  return (
    <div className="bg-card border border-border rounded-xl p-5 space-y-4">
      <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">12-Month Momentum</h3>
      <div className="space-y-3">
        {values.map(({ key, label, val }) => {
          const barWidth = Math.round((Math.abs(val) / max) * 100)
          const pos      = val >= 0
          return (
            <div key={key}>
              <div className="flex justify-between text-xs text-slate-400 mb-1">
                <span>{label}</span>
                <span className={pos ? 'text-emerald-400' : 'text-red-400'}>{pct(val)}</span>
              </div>
              <div className="h-2 bg-slate-800 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-500 ${pos ? 'bg-emerald-500' : 'bg-red-500'}`}
                  style={{ width: `${barWidth}%` }}
                />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function AiDecisionCard({ signal, loading }) {
  if (loading) {
    return (
      <div className="bg-card border border-border rounded-xl p-5 space-y-3">
        <Skeleton className="h-5 w-32" />
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-4 w-full" />
      </div>
    )
  }
  if (!signal) return null

  const decision  = signal.ai_verdict || 'WAIT'
  const reasoning = signal.ai_reasoning || signal.reasoning || '—'
  const colorClass = DECISION_COLORS[decision] || DECISION_COLORS.WAIT

  return (
    <div className={`bg-card border rounded-xl p-5 space-y-4 ${colorClass.includes('emerald') ? 'border-emerald-500/30' : colorClass.includes('yellow') ? 'border-yellow-500/30' : 'border-border'}`}>
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">AI Decision</h3>
        <div className="flex items-center gap-2">
          <Badge label={signal.mode?.toUpperCase() || 'PAPER'} colorClass="text-slate-400 bg-slate-700/40 border-slate-600/30" />
        </div>
      </div>

      <div className="flex items-center gap-4">
        <span className={`text-3xl font-black ${colorClass.split(' ')[0]}`}>{decision}</span>
        {signal.recommended_symbol && (
          <span className="text-2xl font-bold text-slate-200">{signal.recommended_symbol}</span>
        )}
      </div>

      <p className="text-xs text-slate-400 italic leading-relaxed">{reasoning}</p>

      <div className="flex items-center gap-2 text-xs text-slate-500">
        {signal.created_at && (
          <span>Evaluated {new Date(signal.created_at).toLocaleString()}</span>
        )}
        {signal.executed && (
          <Badge label="Executed" colorClass="text-emerald-400 bg-emerald-400/10 border-emerald-400/30" />
        )}
      </div>
    </div>
  )
}

function PositionsList({ positions, loading }) {
  if (loading) {
    return (
      <div className="bg-card border border-border rounded-xl p-5">
        <Skeleton className="h-5 w-32 mb-4" />
        <Skeleton className="h-16 w-full" />
      </div>
    )
  }

  return (
    <div className="bg-card border border-border rounded-xl p-5 space-y-4">
      <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Current Position</h3>
      {(!positions || positions.length === 0) ? (
        <p className="text-sm text-slate-500 text-center py-4">No open positions in this strategy account.</p>
      ) : (
        <div className="space-y-2">
          {positions.map(p => (
            <div key={p.symbol} className="flex items-center justify-between bg-surface rounded-lg px-4 py-3">
              <div>
                <span className="text-sm font-bold text-slate-100">{p.symbol}</span>
                <span className="ml-2 text-xs text-slate-500">{p.qty} shares @ {currency(p.entry_price)}</span>
              </div>
              <div className="text-right">
                <p className="text-sm font-semibold text-slate-200">{currency(p.market_value)}</p>
                <p className={`text-xs font-medium ${p.unrealized_pl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {p.unrealized_pl >= 0 ? '+' : ''}{currency(p.unrealized_pl)} ({fmt(p.unrealized_plpc)}%)
                </p>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function HistoryTable({ history }) {
  if (!history || history.length === 0) {
    return (
      <div className="bg-card border border-border rounded-xl p-5">
        <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide mb-4">Signal History</h3>
        <p className="text-sm text-slate-500 text-center py-4">No signals yet — run an evaluation.</p>
      </div>
    )
  }

  return (
    <div className="bg-card border border-border rounded-xl p-5 space-y-4">
      <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Signal History</h3>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 border-b border-border">
              <th className="text-left pb-2 pr-4">Date</th>
              <th className="text-left pb-2 pr-4">Symbol</th>
              <th className="text-left pb-2 pr-4">AI Verdict</th>
              <th className="text-left pb-2 pr-4">Mode</th>
              <th className="text-left pb-2">Executed</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {history.map(row => (
              <tr key={row.id} className="hover:bg-surface/50">
                <td className="py-2 pr-4 text-slate-400">
                  {new Date(row.created_at).toLocaleDateString()} {new Date(row.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </td>
                <td className="py-2 pr-4 font-bold text-slate-200">{row.recommended_symbol || '—'}</td>
                <td className="py-2 pr-4">
                  <Badge
                    label={row.ai_verdict || 'WAIT'}
                    colorClass={DECISION_COLORS[row.ai_verdict] || DECISION_COLORS.WAIT}
                  />
                </td>
                <td className="py-2 pr-4 text-slate-400 uppercase">{row.mode}</td>
                <td className="py-2">
                  {row.executed
                    ? <span className="text-emerald-400">✓</span>
                    : <span className="text-slate-600">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function StrategySettings({ config, onSave, saving }) {
  const [open, setOpen]           = useState(false)
  const [form, setForm]           = useState(null)

  // Initialise form when config arrives
  if (config && !form) {
    setForm({
      trading_mode:        config.trading_mode        || 'paper',
      is_active:           config.is_active           || false,
      auto_execute:        config.auto_execute        || false,
      lookback_months:     config.settings?.lookback_months || 12,
      alpaca_paper_key:    config.alpaca_paper_key    || '',
      alpaca_paper_secret: config.alpaca_paper_secret || '',
      alpaca_live_key:     config.alpaca_live_key     || '',
      alpaca_live_secret:  config.alpaca_live_secret  || '',
    })
  }

  function set(key, val) { setForm(f => ({ ...f, [key]: val })) }

  function handleSave() {
    if (!form) return
    const payload = { ...form, lookback_months: parseInt(form.lookback_months) || 12 }
    onSave(payload)
  }

  return (
    <div className="bg-card border border-border rounded-xl overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-5 py-4 text-sm font-semibold text-slate-300 hover:text-slate-100 transition-colors"
      >
        <span className="uppercase tracking-wide">Strategy Settings</span>
        <span className="text-slate-500 text-xs">{open ? '▲ collapse' : '▼ expand'}</span>
      </button>

      {open && form && (
        <div className="px-5 pb-5 space-y-5 border-t border-border pt-4">
          {/* Toggle row */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <label className="flex items-center gap-3 cursor-pointer">
              <input type="checkbox" className="w-4 h-4 rounded accent-blue-500"
                checked={form.is_active} onChange={e => set('is_active', e.target.checked)} />
              <span className="text-sm text-slate-300">Strategy Active</span>
            </label>
            <label className="flex items-center gap-3 cursor-pointer">
              <input type="checkbox" className="w-4 h-4 rounded accent-blue-500"
                checked={form.auto_execute} onChange={e => set('auto_execute', e.target.checked)} />
              <span className="text-sm text-slate-300">Auto-Execute Signals</span>
            </label>
            <div className="flex items-center gap-3">
              <span className="text-sm text-slate-300 whitespace-nowrap">Trading Mode</span>
              <select
                className="bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-slate-200 flex-1"
                value={form.trading_mode}
                onChange={e => set('trading_mode', e.target.value)}
              >
                <option value="paper">Paper</option>
                <option value="live">Live</option>
              </select>
            </div>
          </div>

          {/* Lookback */}
          <div className="flex items-center gap-3">
            <label className="text-sm text-slate-300 whitespace-nowrap">Lookback Months</label>
            <input
              type="number" min={1} max={24}
              className="bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-slate-200 w-24"
              value={form.lookback_months}
              onChange={e => set('lookback_months', e.target.value)}
            />
            <span className="text-xs text-slate-500">default: 12 (Antonacci GEM)</span>
          </div>

          {/* Alpaca credentials */}
          <div>
            <p className="text-xs text-slate-500 mb-3">
              Strategy-specific Alpaca keys — leave blank to use your account default keys.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {[
                ['alpaca_paper_key',    'Paper API Key'],
                ['alpaca_paper_secret', 'Paper Secret'],
                ['alpaca_live_key',     'Live API Key'],
                ['alpaca_live_secret',  'Live Secret'],
              ].map(([field, label]) => (
                <div key={field}>
                  <label className="text-xs text-slate-500 mb-1 block">{label}</label>
                  <input
                    type="password"
                    className="w-full bg-surface border border-border rounded-lg px-3 py-2 text-sm text-slate-200 font-mono"
                    placeholder="••••••••"
                    value={form[field]}
                    onChange={e => set(field, e.target.value)}
                  />
                </div>
              ))}
            </div>
          </div>

          <button
            onClick={handleSave}
            disabled={saving}
            className="px-5 py-2 bg-accent hover:bg-accent/90 rounded-lg text-sm font-semibold text-white disabled:opacity-50 transition-colors"
          >
            {saving ? 'Saving…' : 'Save Settings'}
          </button>
        </div>
      )}
    </div>
  )
}

// ── main component ────────────────────────────────────────────────────────────

export default function DualMomentumTab() {
  const qc = useQueryClient()

  const { data: env,       isLoading: envLoading   } = useQuery('market-env',     fetchMarketEnvironment, { staleTime: 60_000 })
  const { data: signal,    isLoading: sigLoading   } = useQuery('dm-signal',      fetchDMSignal,          { staleTime: 30_000 })
  const { data: positions, isLoading: posLoading   } = useQuery('dm-position',    fetchDMPosition,        { staleTime: 10_000, retry: false })
  const { data: history                            } = useQuery('dm-history',     fetchDMHistory,         { staleTime: 30_000 })
  const { data: config,    isLoading: cfgLoading   } = useQuery('dm-config',      fetchDMConfig,          { staleTime: 60_000 })

  const [toast, setToast]     = useState(null)
  const [evalResult, setEvalResult] = useState(null)

  function showToast(msg, type = 'success') {
    setToast({ msg, type })
    setTimeout(() => setToast(null), 4000)
  }

  const { mutate: runEvaluate, isLoading: evaluating } = useMutation(evaluateDualMomentum, {
    onSuccess: (data) => {
      setEvalResult(data)
      qc.invalidateQueries('dm-signal')
      qc.invalidateQueries('dm-history')
      showToast('Signal evaluated successfully')
    },
    onError: (err) => {
      showToast(err?.response?.data?.detail || 'Evaluation failed', 'error')
    },
  })

  const { mutate: runExecute, isLoading: executing } = useMutation(executeDualMomentum, {
    onSuccess: (data) => {
      qc.invalidateQueries('dm-position')
      qc.invalidateQueries('dm-signal')
      qc.invalidateQueries('dm-history')
      showToast(`Executed: bought ${data.symbol} [${data.mode}]`)
    },
    onError: (err) => {
      showToast(err?.response?.data?.detail || 'Execution failed', 'error')
    },
  })

  const { mutate: saveConfig, isLoading: saving } = useMutation(updateDMConfig, {
    onSuccess: () => {
      qc.invalidateQueries('dm-config')
      showToast('Settings saved')
    },
    onError: (err) => {
      showToast(err?.response?.data?.detail || 'Save failed', 'error')
    },
  })

  // Decide which signal data to display: latest eval result or last saved signal
  const displaySignal = evalResult
    ? {
        ai_verdict:         evalResult.ai_decision?.decision,
        ai_reasoning:       evalResult.ai_decision?.reasoning,
        recommended_symbol: evalResult.signal?.recommended_symbol,
        mode:               config?.trading_mode || 'paper',
      }
    : signal

  const momentum = evalResult?.signal?.momentum || signal?.data?.momentum
  const envData  = evalResult?.market_env        || env

  return (
    <div className="space-y-5">

      {/* Toast */}
      {toast && (
        <div className={`fixed top-4 right-4 z-50 px-5 py-3 rounded-xl text-sm font-medium shadow-lg border transition-all
          ${toast.type === 'error'
            ? 'bg-red-950 border-red-500/30 text-red-300'
            : 'bg-emerald-950 border-emerald-500/30 text-emerald-300'}`}
        >
          {toast.msg}
        </div>
      )}

      {/* Header row */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold text-slate-100">Dual Momentum <span className="text-slate-500 font-normal text-sm">(GEM)</span></h2>
          <p className="text-xs text-slate-500 mt-0.5">Gary Antonacci's Global Equity Momentum — SPY · EFA · AGG · BIL</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => runExecute()}
            disabled={executing || !signal}
            className="px-4 py-2 rounded-lg text-sm font-semibold border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {executing ? 'Executing…' : 'Execute Signal'}
          </button>
          <button
            onClick={() => runEvaluate()}
            disabled={evaluating}
            className="px-4 py-2 rounded-lg text-sm font-semibold bg-accent hover:bg-accent/90 text-white disabled:opacity-50 transition-colors"
          >
            {evaluating ? (
              <span className="flex items-center gap-2">
                <span className="w-3.5 h-3.5 border border-white border-t-transparent rounded-full animate-spin" />
                Running…
              </span>
            ) : 'Run Signal'}
          </button>
        </div>
      </div>

      {/* Quick regime + AI decision (top 2 cards) */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MarketEnvCard env={envData} loading={envLoading && !evalResult} />
        <AiDecisionCard signal={displaySignal} loading={sigLoading && !evalResult} />
      </div>

      {/* Reasoning from latest GEM evaluation */}
      {(evalResult?.signal?.reasoning || signal?.data?.reasoning) && (
        <div className="bg-card border border-border rounded-xl p-5">
          <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">GEM Signal Reasoning</h3>
          <p className="text-sm text-slate-300 leading-relaxed">
            {evalResult?.signal?.reasoning || signal?.data?.reasoning}
          </p>
        </div>
      )}

      {/* Momentum bars */}
      <MomentumBars momentum={momentum} />

      {/* Position */}
      <PositionsList positions={positions} loading={posLoading} />

      {/* History */}
      <HistoryTable history={history} />

      {/* Settings */}
      <StrategySettings config={config} onSave={saveConfig} saving={saving} />
    </div>
  )
}
