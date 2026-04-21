import { useState } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { setup2fa, enable2fa, disable2fa } from '../api/client'

export default function TwoFactorSetup({ enabled, onChanged }) {
  const [step, setStep]         = useState('idle')   // idle | setup | disable
  const [uri, setUri]           = useState('')
  const [secret, setSecret]     = useState('')
  const [code, setCode]         = useState('')
  const [password, setPassword] = useState('')
  const [error, setError]       = useState('')
  const [loading, setLoading]   = useState(false)

  async function startSetup() {
    setError('')
    setLoading(true)
    try {
      const data = await setup2fa()
      setUri(data.uri)
      setSecret(data.secret)
      setStep('setup')
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to generate secret')
    } finally {
      setLoading(false)
    }
  }

  async function handleEnable(e) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await enable2fa(code)
      setStep('idle')
      setCode('')
      onChanged()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Invalid code')
    } finally {
      setLoading(false)
    }
  }

  async function handleDisable(e) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await disable2fa(password)
      setStep('idle')
      setPassword('')
      onChanged()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Incorrect password')
    } finally {
      setLoading(false)
    }
  }

  if (step === 'setup') {
    return (
      <div className="space-y-4">
        <p className="text-slate-400 text-sm">
          Scan this QR code with your authenticator app (Google Authenticator, Authy, etc.)
        </p>
        <div className="flex justify-center bg-white p-4 rounded-xl w-fit mx-auto">
          <QRCodeSVG value={uri} size={180} />
        </div>
        <p className="text-xs text-slate-500 text-center">
          Or enter this secret manually: <span className="font-mono text-slate-300">{secret}</span>
        </p>
        {error && <p className="text-red-400 text-sm">{error}</p>}
        <form onSubmit={handleEnable} className="flex gap-2">
          <input
            type="text"
            value={code}
            onChange={e => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
            placeholder="Enter 6-digit code to confirm"
            maxLength={6}
            className="flex-1 bg-surface border border-border rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-accent"
          />
          <button
            type="submit"
            disabled={loading || code.length !== 6}
            className="bg-accent hover:bg-accent/90 text-white text-sm px-4 py-2 rounded-lg font-medium disabled:opacity-50"
          >
            {loading ? '…' : 'Enable'}
          </button>
        </form>
        <button onClick={() => { setStep('idle'); setError('') }} className="text-slate-500 hover:text-slate-300 text-sm">
          Cancel
        </button>
      </div>
    )
  }

  if (step === 'disable') {
    return (
      <div className="space-y-4">
        <p className="text-slate-400 text-sm">Enter your password to disable two-factor authentication.</p>
        {error && <p className="text-red-400 text-sm">{error}</p>}
        <form onSubmit={handleDisable} className="flex gap-2">
          <input
            type="password"
            value={password}
            onChange={e => setPassword(e.target.value)}
            placeholder="Your password"
            className="flex-1 bg-surface border border-border rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-accent"
          />
          <button
            type="submit"
            disabled={loading || !password}
            className="bg-red-500 hover:bg-red-600 text-white text-sm px-4 py-2 rounded-lg font-medium disabled:opacity-50"
          >
            {loading ? '…' : 'Disable 2FA'}
          </button>
        </form>
        <button onClick={() => { setStep('idle'); setError('') }} className="text-slate-500 hover:text-slate-300 text-sm">
          Cancel
        </button>
      </div>
    )
  }

  return (
    <div className="flex items-center justify-between">
      <div>
        <p className="text-sm text-slate-300 font-medium">Two-factor authentication</p>
        <p className="text-xs text-slate-500 mt-0.5">
          {enabled ? 'Your account is protected with an authenticator app.' : 'Add extra security to your account.'}
        </p>
      </div>
      {enabled ? (
        <button
          onClick={() => setStep('disable')}
          className="text-xs bg-red-500/10 text-red-400 hover:bg-red-500/20 px-3 py-1.5 rounded-lg font-medium transition-colors"
        >
          Disable
        </button>
      ) : (
        <button
          onClick={startSetup}
          disabled={loading}
          className="text-xs bg-accent/10 text-accent hover:bg-accent/20 px-3 py-1.5 rounded-lg font-medium transition-colors disabled:opacity-50"
        >
          {loading ? 'Loading…' : 'Enable 2FA'}
        </button>
      )}
    </div>
  )
}
