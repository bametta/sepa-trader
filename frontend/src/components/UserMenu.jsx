import { useState, useRef, useEffect } from 'react'
import { useAuth } from '../AuthContext'
import { changePassword } from '../api/client'

export default function UserMenu() {
  const { user, logout }      = useAuth()
  const [open, setOpen]       = useState(false)
  const [showPw, setShowPw]   = useState(false)
  const [current, setCurrent] = useState('')
  const [next, setNext]       = useState('')
  const [pwError, setPwError] = useState('')
  const [pwOk, setPwOk]       = useState(false)
  const ref                   = useRef(null)

  useEffect(() => {
    function handler(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  async function handleChangePassword(e) {
    e.preventDefault()
    setPwError('')
    setPwOk(false)
    try {
      await changePassword(current, next)
      setPwOk(true)
      setCurrent('')
      setNext('')
    } catch (err) {
      setPwError(err?.response?.data?.detail || 'Failed to change password')
    }
  }

  if (!user) return null

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-2 bg-surface hover:bg-card border border-border rounded-lg px-3 py-1.5 text-sm transition-colors"
      >
        <span className="w-6 h-6 rounded-full bg-accent/20 text-accent flex items-center justify-center text-xs font-bold">
          {user.username[0].toUpperCase()}
        </span>
        <span className="text-slate-300 hidden sm:block">{user.username}</span>
        {user.role === 'admin' && (
          <span className="text-xs bg-accent/20 text-accent px-1.5 py-0.5 rounded-full hidden sm:block">admin</span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-72 bg-card border border-border rounded-xl shadow-xl z-50 py-2">
          <div className="px-4 py-2 border-b border-border">
            <p className="text-sm text-slate-200 font-medium">{user.username}</p>
            <p className="text-xs text-slate-500">{user.email}</p>
          </div>

          {/* Change password */}
          <div className="px-4 py-3 border-b border-border">
            <button
              onClick={() => { setShowPw(s => !s); setPwError(''); setPwOk(false) }}
              className="text-xs text-slate-400 hover:text-slate-200 transition-colors"
            >
              {showPw ? 'Hide' : 'Change password'}
            </button>
            {showPw && (
              <form onSubmit={handleChangePassword} className="mt-2 space-y-2">
                {pwError && <p className="text-xs text-red-400">{pwError}</p>}
                {pwOk    && <p className="text-xs text-emerald-400">Password changed.</p>}
                <input
                  type="password"
                  value={current}
                  onChange={e => setCurrent(e.target.value)}
                  placeholder="Current password"
                  className="w-full bg-surface border border-border rounded px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-accent"
                />
                <input
                  type="password"
                  value={next}
                  onChange={e => setNext(e.target.value)}
                  placeholder="New password (min 8 chars)"
                  minLength={8}
                  className="w-full bg-surface border border-border rounded px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-accent"
                />
                <button
                  type="submit"
                  disabled={!current || next.length < 8}
                  className="text-xs bg-accent/10 text-accent hover:bg-accent/20 px-3 py-1.5 rounded font-medium disabled:opacity-50"
                >
                  Update
                </button>
              </form>
            )}
          </div>

          {/* Logout */}
          <div className="px-2 pt-1">
            <button
              onClick={logout}
              className="w-full text-left text-sm text-red-400 hover:bg-red-500/10 px-2 py-2 rounded-lg transition-colors"
            >
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
