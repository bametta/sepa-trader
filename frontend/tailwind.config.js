export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        bg:      '#080c14',
        surface: '#0d1220',
        card:    '#111827',
        border:  'rgba(255,255,255,0.07)',
        accent:  '#6366f1',
        violet:  '#8b5cf6',
        muted:   '#334155',
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'Cascadia Code', 'ui-monospace', 'monospace'],
      },
      boxShadow: {
        'glow-indigo':  '0 0 20px rgba(99,102,241,0.15), 0 0 60px rgba(99,102,241,0.05)',
        'glow-emerald': '0 0 20px rgba(16,185,129,0.15), 0 0 60px rgba(16,185,129,0.05)',
        'glow-red':     '0 0 20px rgba(239,68,68,0.15),  0 0 60px rgba(239,68,68,0.05)',
        'card':         '0 4px 24px rgba(0,0,0,0.4), 0 1px 0 rgba(255,255,255,0.04) inset',
        'card-hover':   '0 8px 32px rgba(0,0,0,0.5), 0 1px 0 rgba(255,255,255,0.06) inset',
      },
      backgroundImage: {
        'gradient-radial': 'radial-gradient(var(--tw-gradient-stops))',
        'gradient-conic':  'conic-gradient(from 180deg at 50% 50%, var(--tw-gradient-stops))',
        'glass':           'linear-gradient(135deg, rgba(255,255,255,0.05) 0%, rgba(255,255,255,0.01) 100%)',
        'glass-dark':      'linear-gradient(135deg, rgba(255,255,255,0.03) 0%, rgba(255,255,255,0.005) 100%)',
      },
      animation: {
        'fade-in':    'fadeIn 0.3s ease-out',
        'slide-up':   'slideUp 0.3s ease-out',
        'glow-pulse': 'glowPulse 2s ease-in-out infinite',
      },
      keyframes: {
        fadeIn:    { '0%': { opacity: 0 }, '100%': { opacity: 1 } },
        slideUp:   { '0%': { opacity: 0, transform: 'translateY(8px)' }, '100%': { opacity: 1, transform: 'translateY(0)' } },
        glowPulse: { '0%,100%': { boxShadow: '0 0 6px rgba(99,102,241,0.4)' }, '50%': { boxShadow: '0 0 18px rgba(99,102,241,0.7)' } },
      },
    }
  },
  plugins: []
}
