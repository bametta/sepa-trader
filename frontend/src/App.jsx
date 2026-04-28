import { useState } from 'react'
import { useQuery, useQueryClient } from 'react-query'
import { AuthProvider, useAuth } from './AuthContext'
import LoginPage from './LoginPage'
import RegisterPage from './RegisterPage'
import Navbar from './components/Navbar'
import AccountSummary from './components/AccountSummary'
import PositionCard from './components/PositionCard'
import { OpenOrdersTable, AlpacaHistoryTable } from './components/OrdersTable'
import SettingsPanel from './components/SettingsPanel'
import WeeklyPlan from './components/WeeklyPlan'
import AdminPanel from './components/AdminPanel'
import DualMomentumTab from './components/DualMomentumTab'
import PreTradeLog from './components/PreTradeLog'
import { fetchPositions, updateSetting, fetchSettings } from './api/client'

const POSITIONS_INTERVAL = 5000
const ACCOUNT_INTERVAL   = 5000

const TAB_CONFIG = [
  { id: 'Positions',      icon: '⬡', label: 'Positions' },
  { id: 'Orders',         icon: '↕', label: 'Orders' },
  { id: 'History',        icon: '◷', label: 'History' },
  { id: 'Weekly Plan',    icon: '✦', label: 'Weekly Plan' },
  { id: 'Dual Momentum',  icon: '⟳', label: 'Dual Momentum' },
  { id: 'AI Gate',        icon: '✓', label: 'AI Gate' },
  { id: 'Settings',       icon: '⚙', label: 'Settings' },
]

function Dashboard() {
  const { user }                  = useAuth()
  const [switching, setSwitching] = useState(false)
  const qc                        = useQueryClient()

  const tabs = [
    ...TAB_CONFIG,
    ...(user?.role === 'admin' ? [{ id: 'Admin', icon: '⛭', label: 'Admin' }] : []),
  ]
  const [tab, setTab] = useState('Positions')

  const { data: positions = [], isLoading: posLoading, isError: posError } = useQuery(
    'positions',
    fetchPositions,
    { refetchInterval: POSITIONS_INTERVAL, refetchIntervalInBackground: true, staleTime: 2000 },
  )

  async function handleModeChange(newMode) {
    if (switching) return
    if (newMode === 'live') {
      const confirmed = window.confirm(
        '⚠️ Switch to LIVE trading?\n\n' +
        'Real money will be used. Ensure your live Alpaca credentials are set and that ' +
        'you have reviewed your positions and exit orders.\n\nPress OK to confirm.'
      )
      if (!confirmed) return
    }
    setSwitching(true)
    try {
      await updateSetting('trading_mode', newMode)
      await qc.invalidateQueries()
    } catch (err) {
      alert(`Failed to switch mode: ${err?.response?.data?.detail || err.message}`)
    } finally {
      setSwitching(false)
    }
  }

  const { data: settings = {} } = useQuery('settings', fetchSettings, { staleTime: 30000 })

  const paperAE = settings.paper_auto_execute !== 'false'
  const liveAE  = settings.live_auto_execute  === 'true'

  const urgent    = positions.filter(p => p.signal === 'NO_SETUP')
  const breakouts = positions.filter(p => p.signal === 'BREAKOUT')

  return (
    <div className="min-h-screen" style={{ background: '#080c14' }}>
      <Navbar onModeChange={handleModeChange} />

      {/* Mode-switch overlay */}
      {switching && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center">
          <div className="card px-8 py-7 text-center space-y-3 max-w-xs">
            <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin mx-auto" />
            <p className="text-slate-200 text-sm font-semibold">Switching trading mode…</p>
            <p className="text-slate-500 text-xs">Refreshing all data for the new account</p>
          </div>
        </div>
      )}

      <main className="max-w-7xl mx-auto px-3 sm:px-5 py-3 space-y-3">

        {/* Alert banners */}
        {urgent.length > 0 && (
          <div className="flex items-center gap-3 bg-red-500/8 border border-red-500/20 rounded-xl px-4 py-2 animate-fade-in">
            <div className="w-2 h-2 rounded-full bg-red-400 animate-pulse flex-shrink-0" />
            <div>
              <span className="text-red-400 font-bold text-xs uppercase tracking-wide">Stage 2 Lost</span>
              <span className="text-red-300 text-sm ml-2">{urgent.map(p => p.symbol).join(', ')} — review immediately</span>
            </div>
          </div>
        )}
        {breakouts.length > 0 && (
          <div className="flex items-center gap-3 bg-emerald-500/8 border border-emerald-500/20 rounded-xl px-4 py-2 animate-fade-in">
            <div className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse flex-shrink-0" />
            <div>
              <span className="text-emerald-400 font-bold text-xs uppercase tracking-wide">Breakout</span>
              <span className="text-emerald-300 text-sm ml-2">{breakouts.map(p => p.symbol).join(', ')} breaking out on volume</span>
            </div>
          </div>
        )}

        {/* Dual-mode status bar — always visible */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[10px] font-bold uppercase tracking-widest text-slate-600">Monitor</span>
          {/* Paper */}
          <span className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-1 rounded-lg border ${
            paperAE
              ? 'bg-blue-500/10 border-blue-500/25 text-blue-300'
              : 'bg-white/3 border-white/8 text-slate-500'
          }`}>
            <span className={`w-1.5 h-1.5 rounded-full ${paperAE ? 'bg-blue-400 animate-pulse' : 'bg-slate-600'}`} />
            Paper {paperAE ? 'AUTO' : 'observe'}
          </span>
          {/* Live */}
          <span className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-1 rounded-lg border ${
            liveAE
              ? 'bg-orange-500/10 border-orange-500/30 text-orange-300'
              : 'bg-white/3 border-white/8 text-slate-500'
          }`}>
            <span className={`w-1.5 h-1.5 rounded-full ${liveAE ? 'bg-orange-400 animate-pulse' : 'bg-slate-600'}`} />
            Live {liveAE ? '⚡ AUTO' : 'observe'}
          </span>
          {liveAE && (
            <span className="text-[10px] text-orange-400/70 italic">Real money orders will execute</span>
          )}
        </div>

        <AccountSummary onModeChange={handleModeChange} refetchInterval={ACCOUNT_INTERVAL} />

        {/* Tab bar */}
        <div className="flex gap-1 p-1 w-fit max-w-full overflow-x-auto"
             style={{ background: 'rgba(255,255,255,0.02)', borderRadius: 14, border: '1px solid rgba(255,255,255,0.05)' }}>
          {tabs.map(t => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-sm font-medium transition-all whitespace-nowrap ${
                tab === t.id ? 'tab-active' : 'tab-inactive'
              }`}
            >
              <span className="text-xs opacity-70">{t.icon}</span>
              {t.label}
              {t.id === 'Positions' && positions.length > 0 && (
                <span className="ml-0.5 bg-white/10 text-slate-300 text-[10px] px-1.5 py-0.5 rounded-full font-semibold">
                  {positions.length}
                </span>
              )}
            </button>
          ))}
        </div>

        {/* Tab content */}
        <div className="animate-fade-in" key={tab}>
          {tab === 'Positions' && (
            posLoading ? (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
                {[...Array(3)].map((_, i) => (
                  <div key={i} className="card h-64 animate-pulse" />
                ))}
              </div>
            ) : posError ? (
              <div className="card p-12 text-center">
                {posError?.response?.data?.detail === 'alpaca_credentials_missing'
                  ? <p className="text-amber-300 text-sm">No Alpaca credentials set — add them in <strong>Settings → Alpaca Credentials</strong>.</p>
                  : <p className="text-red-400 text-sm">Failed to load positions — check backend logs.</p>
                }
              </div>
            ) : positions.length === 0 ? (
              <div className="card p-16 text-center">
                <p className="text-4xl mb-4 opacity-20">⬡</p>
                <p className="text-slate-500 text-sm">No open positions</p>
              </div>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
                {positions.map(p => <PositionCard key={p.symbol} pos={p} />)}
              </div>
            )
          )}

          {tab === 'Orders'        && <div className="space-y-5"><OpenOrdersTable /><AlpacaHistoryTable /></div>}
          {tab === 'History'       && <AlpacaHistoryTable />}
          {tab === 'Weekly Plan'   && <WeeklyPlan />}
          {tab === 'Dual Momentum' && <DualMomentumTab />}
          {tab === 'AI Gate'       && <PreTradeLog />}
          {tab === 'Settings'      && <SettingsPanel />}
          {tab === 'Admin'         && <AdminPanel />}
        </div>
      </main>
    </div>
  )
}

function AuthGate() {
  const { user, loading } = useAuth()
  const [page, setPage]   = useState('login')

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: '#080c14' }}>
        <div className="space-y-4 text-center">
          <div className="w-10 h-10 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin mx-auto" />
          <p className="text-slate-600 text-sm">Loading…</p>
        </div>
      </div>
    )
  }

  if (!user) {
    return page === 'login'
      ? <LoginPage onGoRegister={() => setPage('register')} />
      : <RegisterPage onGoLogin={() => setPage('login')} />
  }

  return <Dashboard />
}

export default function App() {
  return (
    <AuthProvider>
      <AuthGate />
    </AuthProvider>
  )
}
