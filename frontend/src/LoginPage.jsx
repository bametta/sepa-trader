import { useState } from 'react'
import { useAuth } from './AuthContext'

export default function LoginPage({ onGoRegister }) {
  const { login, verify2fa } = useAuth()
  const [email, setEmail]         = useState('')
  const [password, setPassword]   = useState('')
  const [code, setCode]           = useState('')
  const [tempToken, setTempToken] = useState(null)
  const [error, setError]         = useState('')
  const [loading, setLoading]     = useState(false)

  async function handleLogin(e) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const result = await login(email, password)
      if (result.requires_2fa) setTempToken(result.temp_token)
    } catch (err) {
      setError(err?.response?.data?.detail || 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  async function handle2fa(e) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await verify2fa(tempToken, code)
    } catch (err) {
      setError(err?.response?.data?.detail || 'Invalid code')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-surface flex items-center justify-center px-4">
      <div className="w-full max-w-sm bg-card border border-border rounded-2xl p-8 space-y-6">
        <div className="text-center">
          <h1 className="text-xl font-bold text-slate-100">SEPA Trader</h1>
          <p className="text-slate-500 text-sm mt-1">{tempToken ? 'Two-factor authentication' : 'Sign in to your account'}</p>
        </div>

        {error && (
          <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-2.5 text-red-400 text-sm">
            {error}
          </div>
        )}

        {!tempToken ? (
          <form onSubmit={handleLogin} className="space-y-4">
            <div className="space-y-1">
              <label className="text-xs text-slate-400 font-medium">Email</label>
              <input
                type="email"
                value={email}
                onChange={e => setEmail(e.target.value)}
                required
                className="w-full bg-surface border border-border rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-accent"
                placeholder="you@example.com"
              />
            </div>
            <div className="space-y-1">
              <label className="text-xs text-slate-400 font-medium">Password</label>
              <input
                type="password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                required
                className="w-full bg-surface border border-border rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-accent"
                placeholder="••••••••"
              />
            </div>
            <button
              type="submit"
              disabled={loading}
              className="w-full bg-accent hover:bg-accent/90 text-white font-medium text-sm py-2.5 rounded-lg transition-colors disabled:opacity-50"
            >
              {loading ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        ) : (
          <form onSubmit={handle2fa} className="space-y-4">
            <p className="text-slate-400 text-sm text-center">
              Enter the 6-digit code from your authenticator app.
            </p>
            <input
              type="text"
              value={code}
              onChange={e => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              required
              autoFocus
              className="w-full bg-surface border border-border rounded-lg px-3 py-2 text-sm text-slate-200 text-center tracking-widest focus:outline-none focus:border-accent"
              placeholder="000000"
              maxLength={6}
            />
            <button
              type="submit"
              disabled={loading || code.length !== 6}
              className="w-full bg-accent hover:bg-accent/90 text-white font-medium text-sm py-2.5 rounded-lg transition-colors disabled:opacity-50"
            >
              {loading ? 'Verifying…' : 'Verify'}
            </button>
            <button
              type="button"
              onClick={() => { setTempToken(null); setCode('') }}
              className="w-full text-slate-500 hover:text-slate-300 text-sm transition-colors"
            >
              Back to login
            </button>
          </form>
        )}

        <p className="text-center text-slate-500 text-sm">
          No account?{' '}
          <button onClick={onGoRegister} className="text-accent hover:underline">
            Register
          </button>
        </p>
      </div>
    </div>
  )
}
