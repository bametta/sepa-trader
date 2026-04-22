import { useQuery } from 'react-query'
import { fetchAccount } from '../api/client'

function fmt(n, sign = false) {
  const prefix = sign ? (n >= 0 ? '+' : '-') : ''
  return `${prefix}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function MetricCard({ label, value, sub, valueClass = 'text-slate-100', glow }) {
  return (
    <div className={`stat-card flex flex-col gap-1 ${glow ? `shadow-${glow}` : ''}`}>
      <span className="label">{label}</span>
      <span className={`text-xl font-bold num tracking-tight ${valueClass}`}>{value}</span>
      {sub && <span className="text-xs text-slate-500 num">{sub}</span>}
    </div>
  )
}

export default function AccountSummary({ onModeChange, refetchInterval = 5000 }) {
  const { data, isLoading, isError, error, dataUpdatedAt } = useQuery(
    'account',
    () => fetchAccount(),
    { refetchInterval, refetchIntervalInBackground: true, staleTime: 2000 }
  )

  if (isLoading) {
    return (
      <div className="card p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="h-5 w-36 bg-white/5 rounded-lg animate-pulse" />
          <div className="h-7 w-20 bg-white/5 rounded-lg animate-pulse" />
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="stat-card h-16 animate-pulse" />
          ))}
        </div>
      </div>
    )
  }

  if (isError || !data) {
    const missing = error?.response?.data?.detail === 'alpaca_credentials_missing'
    return (
      <div className={`card p-5 border ${missing ? 'border-amber-500/20' : 'border-red-500/20'}`}>
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

  const isPaper  = data.mode === 'paper'
  const isProfit = data.day_pnl >= 0
  const lastSync = dataUpdatedAt ? new Date(dataUpdatedAt).toLocaleTimeString() : null

  return (
    <div className={`card p-4 ${!isPaper ? 'shadow-[0_0_0_1px_rgba(249,115,22,0.2),0_8px_32px_rgba(249,115,22,0.06)]' : ''}`}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-semibold text-slate-300">Account Overview</h2>

          {!isPaper && (
            <span className="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full bg-orange-500/15 text-orange-400 border border-orange-500/30 uppercase tracking-wider">
              <span className="w-1 h-1 rounded-full bg-orange-400 animate-pulse" />
              Live
            </span>
          )}

          <div className="flex items-center gap-1.5" title={`Last synced: ${lastSync}`}>
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
            <span className="text-[10px] text-slate-600">Live data</span>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <span className={`text-xs font-bold px-2.5 py-1 rounded-lg border ${
            isPaper
              ? 'bg-blue-500/10 text-blue-400 border-blue-500/20'
              : 'bg-orange-500/10 text-orange-400 border-orange-500/20'
          }`}>
            {isPaper ? 'PAPER' : '⚡ LIVE'}
          </span>
          <button
            onClick={() => onModeChange && onModeChange(isPaper ? 'live' : 'paper')}
            className="text-xs text-slate-500 hover:text-slate-300 transition-colors underline underline-offset-2"
          >
            switch
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <MetricCard
          label="Portfolio Value"
          value={fmt(data.portfolio_value)}
        />
        <MetricCard
          label="Cash"
          value={fmt(data.cash)}
        />
        <MetricCard
          label="Buying Power"
          value={fmt(data.buying_power)}
        />
        <MetricCard
          label="Day P&L"
          value={fmt(data.day_pnl, true)}
          sub={`${data.day_pnl_pct >= 0 ? '+' : ''}${data.day_pnl_pct.toFixed(2)}%`}
          valueClass={isProfit ? 'text-emerald-400' : 'text-red-400'}
          glow={isProfit ? 'glow-emerald' : 'glow-red'}
        />
      </div>

      {lastSync && (
        <p className="text-[10px] text-slate-700 mt-2 text-right">
          Synced {lastSync}
        </p>
      )}
    </div>
  )
}
