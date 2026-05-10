import { useState, useRef } from 'react'
import { useQuery, useQueryClient } from 'react-query'
import { fetchAccountsOverview, fetchAccount, updateTotalDeposited } from '../api/client'


function fmt(n, sign = false) {
  if (n == null) return '—'
  const prefix = sign ? (n >= 0 ? '+' : '-') : ''
  return `${prefix}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function AccountCard({ acct, onModeChange, totalDeposited, onDepositSaved }) {
  const isProfit      = acct.day_pnl >= 0
  const plColor       = isProfit ? 'text-emerald-400' : 'text-red-400'
  const totalPlColor  = (acct.unrealized_pl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
  const cashColor     = (acct.cash ?? 0) < 0 ? 'text-red-400' : 'text-slate-100'
  const glowClass     = isProfit
    ? 'shadow-[0_0_16px_rgba(16,185,129,0.08)]'
    : 'shadow-[0_0_16px_rgba(239,68,68,0.08)]'

  const netPnl        = totalDeposited > 0 ? (acct.portfolio_value - totalDeposited) : null
  const netPnlColor   = netPnl == null ? 'text-slate-400' : netPnl >= 0 ? 'text-emerald-400' : 'text-red-400'
  const netPnlPct     = totalDeposited > 0 ? ((acct.portfolio_value / totalDeposited - 1) * 100) : null

  const [editing, setEditing]   = useState(false)
  const [inputVal, setInputVal] = useState('')
  const [saving, setSaving]     = useState(false)
  const inputRef = useRef(null)

  const startEdit = () => {
    setInputVal(totalDeposited > 0 ? totalDeposited.toString() : '')
    setEditing(true)
    setTimeout(() => inputRef.current?.focus(), 50)
  }

  const saveDeposit = async () => {
    const amount = parseFloat(inputVal)
    if (isNaN(amount) || amount < 0) { setEditing(false); return }
    setSaving(true)
    try {
      await updateTotalDeposited(amount)
      onDepositSaved?.()
    } catch (e) { /* silent */ }
    setSaving(false)
    setEditing(false)
  }

  const handleKey = (e) => {
    if (e.key === 'Enter') saveDeposit()
    if (e.key === 'Escape') setEditing(false)
  }

  return (
    <div className={`card p-4 flex flex-col gap-3 ${glowClass}`}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-slate-300">{acct.name}</span>
        <div className="flex items-center gap-2">
          {acct.mode === 'live' && (
            <span className="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full bg-orange-500/15 text-orange-400 border border-orange-500/30 uppercase tracking-wider">
              <span className="w-1 h-1 rounded-full bg-orange-400 animate-pulse" />
              Live
            </span>
          )}
        </div>
      </div>

      {/* Stats grid — 3 rows × 2 cols */}
      <div className="grid grid-cols-2 gap-2">
        {/* Row 1: Portfolio + Cash */}
        <div className="stat-card">
          <div className="label mb-1">Portfolio</div>
          <div className="text-sm font-bold text-slate-100 num">{fmt(acct.portfolio_value)}</div>
        </div>
        <div className="stat-card">
          <div className="label mb-1">Cash</div>
          <div className={`text-sm font-bold num ${cashColor}`}>{fmt(acct.cash, acct.cash < 0)}</div>
        </div>

        {/* Row 2: Marginable BP + Non-Marginable BP */}
        <div className="stat-card">
          <div className="label mb-1">Marginable BP</div>
          <div className="text-sm font-bold text-slate-100 num">{fmt(acct.buying_power)}</div>
        </div>
        <div className="stat-card">
          <div className="label mb-1">Non-Marg BP</div>
          <div className="text-sm font-bold text-slate-100 num">{fmt(acct.non_marginable_bp ?? acct.buying_power)}</div>
        </div>

        {/* Row 3: Day P&L + Total P&L */}
        <div className={`stat-card ${isProfit ? 'border-emerald-500/15' : 'border-red-500/15'}`}>
          <div className="label mb-1">Day P&L</div>
          <div className={`text-sm font-bold num ${plColor}`}>{fmt(acct.day_pnl, true)}</div>
          <div className={`text-[10px] num ${plColor} opacity-70`}>
            {acct.day_pnl_pct >= 0 ? '+' : ''}{acct.day_pnl_pct?.toFixed(2)}%
          </div>
        </div>
        <div className={`stat-card ${(acct.unrealized_pl ?? 0) >= 0 ? 'border-emerald-500/10' : 'border-red-500/10'}`}>
          <div className="label mb-1">Total P&L</div>
          <div className={`text-sm font-bold num ${totalPlColor}`}>{fmt(acct.unrealized_pl ?? 0, true)}</div>
          <div className="text-[10px] text-slate-600">realized + unrealized</div>
        </div>

        {/* Row 4: Net vs Deposits — spans full width */}
        <div className={`stat-card col-span-2 ${netPnl == null ? '' : netPnl >= 0 ? 'border-emerald-500/10' : 'border-red-500/10'}`}>
          <div className="flex items-center justify-between mb-1">
            <div className="label">Net vs Deposits</div>
            <button
              onClick={startEdit}
              className="text-[10px] text-slate-500 hover:text-slate-300 transition-colors px-1"
              title="Set total deposited capital"
            >
              {totalDeposited > 0 ? `Deposited: ${fmt(totalDeposited)} ✎` : '+ Set deposit ✎'}
            </button>
          </div>

          {editing ? (
            <div className="flex items-center gap-2 mt-1">
              <span className="text-slate-400 text-sm">$</span>
              <input
                ref={inputRef}
                type="number"
                value={inputVal}
                onChange={e => setInputVal(e.target.value)}
                onKeyDown={handleKey}
                placeholder="Total deposited"
                className="flex-1 bg-slate-700 text-slate-100 text-sm rounded px-2 py-0.5 outline-none border border-slate-500 focus:border-sky-500 num"
              />
              <button
                onClick={saveDeposit}
                disabled={saving}
                className="text-xs px-2 py-0.5 rounded bg-sky-500/20 text-sky-300 hover:bg-sky-500/30 disabled:opacity-50"
              >
                {saving ? '…' : 'Save'}
              </button>
              <button onClick={() => setEditing(false)} className="text-xs text-slate-500 hover:text-slate-300">✕</button>
            </div>
          ) : (
            <div className="flex items-baseline gap-3">
              <div className={`text-sm font-bold num ${netPnlColor}`}>
                {netPnl == null ? '— set deposit above' : fmt(netPnl, true)}
              </div>
              {netPnlPct != null && (
                <div className={`text-[10px] num ${netPnlColor} opacity-70`}>
                  {netPnlPct >= 0 ? '+' : ''}{netPnlPct.toFixed(2)}%
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default function AccountSummary({ onModeChange, refetchInterval = 5000 }) {
  // Read active mode from the same 'account' query the Navbar already keeps warm —
  // no extra network request, just shares the cache.
  const { data: accountMeta } = useQuery('account', fetchAccount, {
    staleTime: 5000,
    refetchInterval,
  })
  const activeMode = accountMeta?.mode ?? 'paper'
  const isPaper    = activeMode === 'paper'
  const otherMode  = isPaper ? 'live' : 'paper'

  const { data, isLoading, isError, error, dataUpdatedAt } = useQuery(
    'accounts-overview',
    fetchAccountsOverview,
    { refetchInterval, refetchIntervalInBackground: true, staleTime: 2000 }
  )

  if (isLoading) {
    return (
      <div className="card p-4 space-y-3 animate-pulse">
        <div className="h-3 w-24 bg-white/5 rounded" />
        <div className="grid grid-cols-2 gap-2">
          {[...Array(4)].map((_, j) => <div key={j} className="stat-card h-14" />)}
        </div>
      </div>
    )
  }

  if (isError) {
    const missing = error?.response?.data?.detail === 'alpaca_credentials_missing'
    return (
      <div className={`card p-4 border ${missing ? 'border-amber-500/20' : 'border-red-500/20'}`}>
        <div className="flex items-start gap-3">
          <span className={`text-lg mt-0.5 ${missing ? 'text-amber-400' : 'text-red-400'}`}>
            {missing ? '⚠' : '✕'}
          </span>
          <div>
            {missing ? (
              <>
                <p className="text-amber-300 font-medium text-sm">No Alpaca credentials configured</p>
                <p className="text-slate-500 text-xs mt-1">
                  Go to <span className="text-slate-300 font-medium">Settings → Alpaca Credentials</span> to add your paper or live API keys.
                </p>
              </>
            ) : (
              <>
                <p className="text-red-300 font-medium text-sm">Cannot reach Alpaca API</p>
                <p className="text-slate-500 text-xs mt-1">Verify your credentials in Settings.</p>
              </>
            )}
          </div>
        </div>
      </div>
    )
  }

  const accounts        = (isPaper ? data?.paper : data?.live) ?? []
  const totalDeposited  = data?.total_deposited ?? 0
  const lastSync        = dataUpdatedAt ? new Date(dataUpdatedAt).toLocaleTimeString() : null
  const qc              = useQueryClient()
  const liveStyle  = !isPaper
    ? 'border-orange-500/20 shadow-[0_0_0_1px_rgba(249,115,22,0.1)]'
    : 'border-border'

  return (
    <div className="space-y-1.5">
      <div className={`card p-4 border ${liveStyle}`}>

        {/* Section header */}
        <div className="flex items-center gap-2 mb-3">
          <span className={`w-2 h-2 rounded-full flex-shrink-0 ${!isPaper ? 'bg-orange-400 animate-pulse' : 'bg-blue-400'}`} />
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400">
            {isPaper ? 'Paper Accounts' : 'Live Accounts'}
          </h3>
          {accounts.length > 1 && (
            <span className="text-[10px] text-slate-600">{accounts.length} accounts</span>
          )}
          {/* Switch mode button */}
          <button
            onClick={() => onModeChange?.(otherMode)}
            className={`ml-auto text-[10px] font-medium px-2 py-0.5 rounded-md border transition-colors ${
              isPaper
                ? 'border-orange-500/30 text-orange-400/70 hover:text-orange-400 hover:bg-orange-500/10'
                : 'border-blue-500/30 text-blue-400/70 hover:text-blue-400 hover:bg-blue-500/10'
            }`}
          >
            Switch to {isPaper ? '⚡ Live' : 'Paper'}
          </button>
        </div>

        {accounts.length === 0 ? (
          <p className="text-xs text-slate-600 py-2">
            No {activeMode} credentials configured — add Alpaca {activeMode} keys in Settings.
          </p>
        ) : (
          <div className={`grid gap-3 ${accounts.length > 1 ? 'grid-cols-1 sm:grid-cols-2' : 'grid-cols-1'}`}>
            {accounts.map(acct => (
              <AccountCard
                key={acct.name}
                acct={acct}
                onModeChange={acct.name === 'Main' ? onModeChange : null}
                totalDeposited={acct.name === 'Main' ? totalDeposited : 0}
                onDepositSaved={() => qc.invalidateQueries('accounts-overview')}
              />
            ))}
          </div>
        )}
      </div>

      {lastSync && (
        <p className="text-[10px] text-slate-700 text-right">Synced {lastSync}</p>
      )}
    </div>
  )
}
