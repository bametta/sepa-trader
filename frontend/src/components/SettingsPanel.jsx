import { useState } from 'react'
import { useQuery, useQueryClient, useMutation } from 'react-query'
import { fetchSettings, updateSetting, fetchMe, fetchTvScreeners } from '../api/client'
import TwoFactorSetup from './TwoFactorSetup'

const DAYS = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
const EXCHANGES = ['NYSE', 'NASDAQ', 'AMEX', 'CBOE', 'OTC']

// ── Hover tooltips for every settings field ───────────────────────────────────
const FIELD_TOOLTIPS = {
  // Trading Mode
  trading_mode:         'Controls which mode is shown in the UI. Both paper and live accounts run simultaneously — this only affects the display, not execution.',
  paper_auto_execute:   'When on, the system automatically places simulated orders in your Alpaca paper account. Turn off to review signals manually before any order is placed.',
  live_auto_execute:    '⚠️ Real money. When on, live orders fire automatically. Only enable after validating the full system in paper mode first.',

  // Risk & Position Sizing
  risk_pct:             'Dollar risk per trade = portfolio × this %. Share count is calculated backwards from this: shares = risk$ ÷ (entry − stop). Every trade carries the same dollar risk regardless of price or stop width.',
  stop_loss_pct:        'Fallback stop distance used when EMA20/EMA50 cannot anchor a structural stop. Screeners compute structural stops first — this is the safety floor for edge cases.',
  max_position_pct:     'Hard cap: no single position can exceed this % of portfolio value. Overrides the risk-math share count when a tight stop would otherwise create dangerous concentration.',
  min_cash_pct:         'The system checks settled cash before every buy and refuses to drop below this floor. Prevents drawing on margin and keeps dry powder available for fresh setups.',
  min_position_dollars: 'Orders with a notional value below this are rejected. Tiny positions are net-negative after slippage, bracket leg churn, and commissions.',
  max_positions:        'No new entries fire once this many positions are open across all three strategies combined. The absolute portfolio ceiling.',
  mv_max_slots:         'Minervini can hold at most this many positions simultaneously, independent of overall capacity. Prevents one strategy from crowding out the others.',
  pb_max_slots:         'Pullback can hold at most this many simultaneous positions.',
  rs_max_slots:         'RS Momentum can hold at most this many simultaneous positions.',

  // Minervini — Universe & Filters
  screener_universe:        'Comma-separated tickers to scan (e.g. AAPL,MSFT). Leave blank for a market-wide TradingView scan — recommended for maximum opportunity discovery.',
  screener_top_n:           'How many Minervini candidates to include in the weekly plan. Set to 0 to auto-derive from your position cap.',
  screener_min_score:       'Minimum SEPA score to qualify. Set to 0 for adaptive mode — the screener auto-adjusts the threshold based on how many setups are available.',
  screener_price_min:       'Stocks below this price are excluded. Filters out penny stocks and low-float names with erratic fills and wide spreads.',
  screener_price_max:       'Stocks above this price are excluded. Use to keep position sizing manageable relative to your account size.',
  screener_vol_surge_pct:   'Volume must be at least this % above the 10-day average on the signal day. Higher values demand stronger institutional conviction (e.g. 40 = requires 1.4× avg volume).',
  screener_ema20_pct:       'Stock must be within this % band near EMA20. Controls how tight the pullback to the short-term moving average must be to qualify.',
  screener_ema50_pct:       'Stock must be within this % band near EMA50. Controls pullback depth to the mid-term moving average.',
  screener_universe_size:   'How many symbols to pull from TradingView before local filtering. Larger = more thorough but slower. 1500 covers most liquid US equities.',
  screener_max_ema200_ext_pct: 'Rejects stocks extended more than this % above EMA200. Filters out late-stage parabolic runners. Set to 0 to disable.',
  screener_max_ema50_ext_pct:  'Rejects stocks extended more than this % above EMA50. Prevents buying into overextended moves prone to snap-back. Set to 0 to disable.',

  // Minervini — Plan Diversity
  screener_min_rr:      'Minimum risk-to-reward ratio = (target − entry) ÷ (entry − stop). Borderline setups below this are dropped before the AI gate reviews them.',
  max_picks_per_sector: 'Maximum picks from any single sector in the weekly plan. Prevents overconcentration in one theme (e.g. no more than 2 semiconductor picks).',

  // Minervini — Sectors & Entry
  mv_excluded_sectors:     'Stocks in these sectors are filtered out of Minervini results regardless of score or momentum.',
  mv_entry_order_type:     'How the entry order is submitted. Stop-limit is recommended for breakouts — it only activates when price actually clears the pivot, avoiding premature fills.',
  mv_entry_slippage_pct:   'Limit price = entry × (1 + slippage%). Wider tolerance increases fill rate on fast-moving breakout days.',

  // Minervini — Schedule
  screener_auto_run:         'Whether the Minervini screener runs automatically on the configured schedule. Disable to run manually only.',
  screener_schedule_days:    'Which days of the week the Slot 1 screener fires. Thursday/Friday evenings are typical for weekly plan generation.',
  screener_schedule_times:   'Times the Slot 1 screener runs (24h ET). Multiple times allowed — e.g. 20:00 after markets close.',
  screener_schedule_days_2:  'Days for an optional second screener slot — useful for a midweek refresh without replacing the main plan.',
  screener_schedule_times_2: 'Run times for the optional second screener slot.',

  // Pullback — Source
  pb_tv_screener_name: 'Name of a saved TradingView screener to use as the source universe. Leave blank to use the built-in filter settings below instead.',
  pb_exchanges:        'Which exchanges to scan for pullback candidates. NYSE + NASDAQ covers most liquid US equities.',
  pb_top_n:            'How many pullback candidates to include in the weekly plan.',

  // Pullback — Filters
  pb_price_min:         'Minimum stock price. Filters out illiquid penny stocks with wide spreads and erratic price action.',
  pb_price_max:         'Maximum stock price. Higher-priced stocks require larger notional for the same risk-dollar exposure.',
  pb_rsi_min:           'RSI must be at or above this level. Avoids stocks still deep in oversold territory where the downtrend may not be over.',
  pb_rsi_max:           'RSI must be at or below this level. Ensures the stock has genuinely pulled back and is not overbought at entry.',
  pb_avg_vol_min:       'Minimum 10-day average daily volume. Ensures liquidity for bracket order execution without significant market impact.',
  pb_rel_vol_min:       'Current volume relative to the 10-day average. Values below 1.0 indicate below-average activity — the pullback may lack conviction.',
  pb_market_cap_min:    'Filters out micro and small caps with thin float, wide spreads, and higher susceptibility to manipulation.',
  pb_week_change_min:   'Minimum 1-week price change %. Prevents entering stocks in a sharp short-term downtrend disguised as a normal pullback.',
  pb_ema50_proximity:   'Maximum % distance between price and EMA50. The pullback thesis requires price to be testing or near its mid-term moving average.',
  pb_beta_max:          'Maximum beta. High-beta stocks have wider intraday swings that can trigger stops on normal volatility before the thesis plays out.',
  pb_earnings_days_min: 'Minimum days until the next earnings announcement. Avoids entering before a binary event that can gap straight through the stop.',
  pb_ema_spread_min:    'Minimum % spread between EMA20 and EMA50. Rejects flat, compressed EMA structures where the uptrend is losing momentum.',
  pb_adx_min:           'Minimum ADX reading. ADX measures trend strength — values below 20 indicate a ranging, directionless stock with no real trend to pull back into.',
  pb_52w_high_pct_max:  'Maximum % below the 52-week high. Stocks too far from their high are likely in Stage 3/4, not the Stage 2 uptrend the pullback thesis requires.',
  pb_3m_perf_min:       'Minimum 3-month performance %. Filters out stocks that have been consistently underperforming the market over the medium term.',
  pb_min_revenue_growth:'Minimum year-over-year revenue growth %. Adds a fundamental anchor to the technical setup. Set to 0 to ignore fundamentals.',
  pb_block_unknown_earnings: 'Automatically exclude stocks whose next earnings date is unconfirmed. Prevents unexpected binary event exposure on unknown dates.',

  // Pullback — EMA Ladder
  pb_price_above_ema20:   'Requires price to be above EMA20 — the core short-term trend condition for a pullback setup. Never relaxed in adaptive mode.',
  pb_ema20_above_ema50:   'Requires EMA20 above EMA50. Confirms the short-term trend is aligned with the mid-term trend. Never relaxed.',
  pb_ema50_above_ema100:  'Requires EMA50 above EMA100. Confirms mid-term trend is above long-term. Can be relaxed automatically when the screener returns too few results.',
  pb_ema100_above_ema200: 'Requires EMA100 above EMA200 — the full Stage 2 EMA ladder. This is the first condition relaxed when the screener returns too few candidates.',

  // Pullback — PPST
  pb_ppst_required:     'Requires the Pivot Point SuperTrend to show a bullish signal. Adds a momentum confirmation layer on top of the EMA ladder conditions.',
  pb_ppst_pivot_period: 'Lookback period for the PPST pivot point calculation. Should match your TradingView chart settings (default 2).',
  pb_ppst_multiplier:   'ATR multiplier for the PPST SuperTrend bands. Higher values = wider bands = fewer, more reliable signals.',
  pb_ppst_period:       'ATR period for the PPST calculation. Matches TradingView\'s default of 10.',

  // Pullback — AI chart review
  pb_ai_chart_review:    'Send each candidate\'s chart to the AI for a technical review before including it in the weekly plan. Slower but filters out weak or ambiguous setups.',
  pb_ai_chart_min_grade: 'Minimum chart grade to pass the AI review. A = only pristine setups, B = solid setups (recommended), C = allows marginal setups through.',

  // Pullback — Sectors & Entry
  pb_excluded_sectors:    'Pullback picks from these sectors are filtered out regardless of their technical score.',
  pb_entry_order_type:    'How the entry order is submitted. Limit is recommended for pullbacks — the stock is already near your entry price and doesn\'t need chasing.',
  pb_entry_slippage_pct:  'Limit price = entry × (1 + slippage%). Small tolerance is appropriate for pullbacks where you\'re already near the target price.',

  // Pullback — Schedule
  pb_screener_auto_run:          'Whether the Pullback screener runs automatically on the configured schedule.',
  pb_screener_schedule_days:     'Which days the Slot 1 Pullback screener fires.',
  pb_screener_schedule_times:    'Times the Slot 1 Pullback screener runs (24h ET).',
  pb_screener_schedule_days_2:   'Days for the optional second Pullback screener slot.',
  pb_screener_schedule_times_2:  'Run times for the optional second Pullback screener slot.',

  // RS Momentum
  rs_screener_enabled: 'Master switch for the RS Momentum screener. When disabled, no RS picks are generated or added to the weekly plan.',
  rs_exchanges:        'Which exchanges to scan for RS Momentum candidates.',
  rs_price_min:        'Minimum stock price. Filters out penny stocks with unreliable momentum signals.',
  rs_price_max:        'Maximum stock price. Set to 0 for no ceiling.',
  rs_avg_vol_min:      'Minimum average daily volume. Ensures positions can be entered and exited cleanly without moving the market.',
  rs_market_cap_min:   'Minimum market cap. Filters out micro-caps with unpredictable momentum and thin liquidity.',
  rs_min_percentile:   'Only stocks whose RS score ranks at or above this percentile qualify. 75 = top 25% of the scanned universe by relative strength.',
  rs_max_extension:    'Maximum % above EMA50. Prevents buying stocks already overextended and likely to pull back before continuing higher.',
  rs_top_n:            'How many RS Momentum picks to include in the weekly plan.',
  rs_require_stage2:   'Requires price > EMA50 > EMA200 — the classic Stage 2 uptrend filter. Excludes stocks recovering from a downtrend or still building a base.',
  rs_excluded_sectors: 'RS picks from these sectors are excluded. Typically commodities and defensives, which skew RS rankings based on macro factors rather than company-specific momentum.',

  // RS — Schedule
  rs_screener_auto_run:          'Whether the RS Momentum screener runs automatically on the configured schedule.',
  rs_screener_schedule_days:     'Which days the Slot 1 RS screener fires.',
  rs_screener_schedule_times:    'Times the Slot 1 RS screener runs (24h ET).',
  rs_screener_schedule_days_2:   'Days for the optional second RS screener slot.',
  rs_screener_schedule_times_2:  'Run times for the optional second RS screener slot.',

  // Combined Screener
  combined_screener_auto_run:          'Runs all three screeners (Minervini + Pullback + RS) in a single TradingView API call. Recommended as the primary weekly plan generator — one scan instead of three.',
  combined_screener_schedule_days:     'Which days the Slot 1 combined screener fires.',
  combined_screener_schedule_times:    'Times the Slot 1 combined screener runs (24h ET).',
  combined_screener_schedule_days_2:   'Days for the optional second combined screener slot.',
  combined_screener_schedule_times_2:  'Run times for the optional second combined screener slot.',

  // Monitor — Cycle
  monitor_enabled:          'Master switch for the position management loop. When off, no trailing stops, exit guards, T1 partial exits, or time stops fire. Positions are unmanaged.',
  monitor_interval_minutes: 'How often the monitor cycle runs during market hours. Shorter = faster reactions to T1 hits and trailing stop milestones, but more API calls to Alpaca and TradingView.',
  auto_execute:             'Whether Monday open orders fire automatically from the weekly plan. When off, the system logs what would have been bought but places no orders.',

  // Monitor — Apex Loss Prevention
  daily_drawdown_halt_pct: 'Circuit breaker. When today\'s P&L drops below this % of yesterday\'s closing equity, all new buys are blocked for the rest of the session. Exits and trailing stops continue running. Resets automatically at next market open. Set to 0 to disable.',
  time_stop_days:           'Positions open longer than this many trading days are evaluated for closure if they haven\'t moved enough. Prevents dead money from tying up buying power. Set to 0 to disable.',
  time_stop_max_gain_pct:   'Positions with unrealized gain at or above this % survive the time stop regardless of how long they\'ve been open. Only flat or losing positions that have gone nowhere are closed.',

  // Integrations
  tv_chart_layout_id: 'TradingView chart layout ID used for AI chart reviews. Find it in your chart URL: tradingview.com/chart/YOUR_ID_HERE/',
  tv_username:        'TradingView account username. Used by the screener to authenticate and pull real-time market data.',
  tv_password:        'TradingView account password. Stored encrypted. Required for the screener to log in and access live data.',
  watchlist:          'Comma-separated tickers monitored for live breakout signals during the trading day. Entries fire independently of the weekly plan when a breakout is detected.',
  webhook_secret:     'Secret token for validating incoming webhooks (e.g. from TradingView alerts). Prevents unauthorised external triggers.',

  // Alpaca
  alpaca_paper_key:    'API key for your Alpaca paper trading account. Used for all paper mode order placement and position monitoring.',
  alpaca_paper_secret: 'API secret for your Alpaca paper trading account.',
  alpaca_live_key:     'API key for your Alpaca live (real money) account. Used only when live auto-execute is enabled.',
  alpaca_live_secret:  'API secret for your Alpaca live account.',

  // AI
  ai_provider: 'Which AI provider to use for pre-trade analysis and chart reviews. Claude (Anthropic) is the default and most tightly integrated.',
  ai_api_key:  'API key for the selected AI provider. Required for pre-trade gate analysis and AI chart reviews to function.',
  ai_model:    'Specific model ID to use (e.g. claude-opus-4-5). Leave blank to use the provider\'s recommended default.',
  ai_base_url: 'Base URL for OpenAI-compatible providers (xAI, DeepSeek, Groq, Mistral, etc.). Not needed for Anthropic or standard OpenAI.',
  block_on_warn: 'When on, the AI pre-trade gate blocks entries it flags as warnings, not just outright rejections. More conservative — fewer entries, fewer costly mistakes.',
}

// All TradingView sectors with short display labels and growth classification
// Growth = sectors that fit momentum strategies; non-growth = typically excluded by default.
// TV names (used by RS screener) are listed alongside GICS aliases for Minervini/Pullback screeners.
const ALL_SECTORS = [
  // ── Growth / momentum-friendly ───────────────────────────────────────────
  { name: 'Electronic Technology',  short: 'Tech HW',      growth: true  },  // TV: semiconductors, hardware
  { name: 'Technology Services',    short: 'Tech SW',      growth: true  },  // TV: software, IT services
  { name: 'Health Technology',      short: 'Biotech/Pharma', growth: true },  // TV: biotech, pharma, med-tech
  { name: 'Health Services',        short: 'Health Svc',   growth: true  },  // TV: hospitals, managed care
  { name: 'Communications',         short: 'Comms',        growth: true  },  // TV: telecom, media
  { name: 'Consumer Durables',      short: 'Cons. Dur.',   growth: true  },  // TV: autos, appliances
  { name: 'Consumer Services',      short: 'Cons. Svc.',   growth: true  },  // TV: restaurants, hotels
  { name: 'Retail Trade',           short: 'Retail',       growth: true  },  // TV: e-commerce, specialty retail
  { name: 'Commercial Services',    short: 'Comm. Svc.',   growth: true  },  // TV: business/professional services
  { name: 'Producer Manufacturing', short: 'Mfg',          growth: true  },  // TV: machinery, aerospace
  { name: 'Distribution Services',  short: 'Distribution', growth: true  },  // TV: wholesale distribution
  { name: 'Transportation',         short: 'Transport',    growth: true  },  // TV: airlines, shipping
  { name: 'Finance',                short: 'Finance',      growth: true  },  // TV: banks, insurance, REITs
  // ── Defensive / commodity — excluded by default from RS screener ─────────
  { name: 'Energy Minerals',        short: 'Energy',       growth: false },  // TV: oil/gas producers
  { name: 'Industrial Services',    short: 'Oilfield Svc', growth: false },  // TV: contract drillers, oilfield svc
  { name: 'Non-Energy Minerals',    short: 'Mining',       growth: false },  // TV: metals, mining
  { name: 'Process Industries',     short: 'Chemicals',    growth: false },  // TV: chemicals, plastics
  { name: 'Consumer Non-Durables',  short: 'Cons. Def.',   growth: false },  // TV: food, household products
  { name: 'Utilities',              short: 'Utilities',    growth: false },  // TV: same as GICS
  { name: 'Government',             short: 'Government',   growth: false },  // TV: government entities
  { name: 'Miscellaneous',          short: 'Misc',         growth: false },  // TV: uncategorised
]

// Settings sections — grouped by strategy so each one is self-contained.
// Order: Trading mode → Risk & sizing → per-strategy panels → Monitor → infra.
const SECTIONS = [
  {
    title: 'Trading Mode',
    description: 'Switch between paper and live; enable/disable auto-execute per mode.',
    fields: [
      { key: 'trading_mode', label: 'View Mode (display only — both modes always run)', type: 'select',
        options: [{ value: 'paper', label: 'Paper' }, { value: 'live', label: 'Live' }] },
      { key: 'paper_auto_execute', label: 'Paper auto-execute (place simulated orders)',  type: 'toggle', defaultValue: 'true'  },
      { key: 'live_auto_execute',  label: 'Live auto-execute ⚠️ REAL MONEY — enable only when ready', type: 'toggle', defaultValue: 'false' },
    ],
  },
  {
    title: 'Risk & Position Sizing',
    description: 'Applies to all three SEPA-family strategies (Minervini, Pullback, RS).',
    fields: [
      { key: 'risk_pct',              label: 'Risk per trade % (default 2.0)',                            type: 'number' },
      { key: 'stop_loss_pct',         label: 'Default stop loss % (default 8.0)',                         type: 'number' },
      { key: 'max_position_pct',      label: 'Max position size % of portfolio (default 20)',             type: 'number' },
      { key: 'min_cash_pct',          label: 'Cash reserve floor % — never deploy below this (default 10)', type: 'number' },
      { key: 'min_position_dollars',  label: 'Min position value $ — sub-economic order floor (default 500)', type: 'number' },
      { key: 'max_positions',         label: 'Max simultaneous positions overall (default 10)',            type: 'number' },
      { key: 'mv_max_slots',          label: 'Minervini slots — breakout picks (default 3)',              type: 'number' },
      { key: 'pb_max_slots',          label: 'Pullback slots — EMA pullback picks (default 2)',           type: 'number' },
      { key: 'rs_max_slots',          label: 'RS Momentum slots (default 2)',                             type: 'number' },
    ],
  },

  // ── Minervini ─────────────────────────────────────────────────────────────
  {
    title: 'Minervini Screener',
    description: 'SEPA breakout setups — market-wide TV scan ranks the top candidates by score.',
    subsections: [
      {
        title: 'Universe & Filters',
        fields: [
          { key: 'screener_universe',         label: 'Universe override (CSV — blank = market-wide TV scan)', type: 'text', span: true },
          { key: 'screener_top_n',            label: 'Stocks to select (0 = auto from position cap)', type: 'number' },
          { key: 'screener_min_score',        label: 'Min score (0 = adaptive)',                       type: 'number' },
          { key: 'screener_price_min',        label: 'Min price $ (0 = off)',                          type: 'number' },
          { key: 'screener_price_max',        label: 'Max price $ (0 = off)',                          type: 'number' },
          { key: 'screener_vol_surge_pct',       label: 'Volume surge threshold % above avg (e.g. 40 = 1.4×)', type: 'number' },
          { key: 'screener_ema20_pct',           label: 'EMA20 proximity band %',                              type: 'number' },
          { key: 'screener_ema50_pct',           label: 'EMA50 proximity band %',                              type: 'number' },
          { key: 'screener_universe_size',        label: 'Exchange scan depth — symbols pulled from TV (default 1500)', type: 'number' },
          { key: 'screener_max_ema200_ext_pct',  label: 'Max % above EMA200 — early setup filter (0 = off, default 65)', type: 'number' },
          { key: 'screener_max_ema50_ext_pct',   label: 'Max % above EMA50 — overextension filter (0 = off)',  type: 'number' },
        ],
      },
      {
        title: 'Plan Diversity',
        fields: [
          { key: 'screener_min_rr',      label: 'Min R:R to include in plan (default 1.5 — drops borderline setups before the AI gate sees them)', type: 'number' },
          { key: 'max_picks_per_sector', label: 'Max picks per sector (default 2 — caps concentration in any single sector)', type: 'number' },
        ],
      },
      {
        title: 'Sectors',
        fields: [
          { key: 'mv_excluded_sectors', label: 'Excluded sectors', type: 'sector_picker', span: true, defaultValue: '' },
        ],
      },
      {
        title: 'Entry Order',
        fields: [
          { key: 'mv_entry_order_type', label: 'Entry order type', type: 'select',
            options: [
              { value: 'stop_limit', label: 'Stop-limit — activates only when price breaks out (recommended for Minervini)' },
              { value: 'limit',      label: 'Limit — fills up to entry + slippage%' },
              { value: 'market',     label: 'Market — immediate fill at any price' },
            ]
          },
          { key: 'mv_entry_slippage_pct', label: 'Slippage tolerance % (default 1.0)', type: 'number' },
        ],
      },
      {
        title: 'Schedule (ET)',
        fields: [
          { key: 'screener_auto_run',         label: 'Auto-run enabled',                       type: 'toggle',     defaultValue: 'true' },
          { key: 'screener_schedule_days',    label: 'Slot 1 — Days (click to toggle)',         type: 'day_picker', span: true },
          { key: 'screener_schedule_times',   label: 'Slot 1 — Run times (24h ET, e.g. 20:00)',type: 'time_list',  span: true },
          { key: 'screener_schedule_days_2',  label: 'Slot 2 — Days (optional)',                type: 'day_picker', span: true },
          { key: 'screener_schedule_times_2', label: 'Slot 2 — Run times (24h ET, e.g. 16:30)',type: 'time_list',  span: true },
        ],
      },
    ],
  },

  // ── Pullback ──────────────────────────────────────────────────────────────
  {
    title: 'Pullback Screener',
    description: 'Pullback-to-MA setups — saved TV screener or in-app filters.',
    subsections: [
      {
        title: 'Source & Universe',
        fields: [
          { key: 'pb_tv_screener_name', label: 'TradingView Screener name (leave blank to use app filters below)', type: 'tv_screener', span: true },
          { key: 'pb_exchanges',        label: 'Exchanges to scan', type: 'exchange_picker', span: true, defaultValue: 'NYSE,NASDAQ' },
          { key: 'pb_top_n',            label: 'Top N from pullback screener (default 5)', type: 'number' },
        ],
      },
      {
        title: 'Filters',
        fields: [
          { key: 'pb_price_min',         label: 'Min price $ (default 10)',             type: 'number' },
          { key: 'pb_price_max',         label: 'Max price $ (default 200)',            type: 'number' },
          { key: 'pb_rsi_min',           label: 'RSI min (reset zone, default 40)',     type: 'number' },
          { key: 'pb_rsi_max',           label: 'RSI max (reset zone, default 60)',     type: 'number' },
          { key: 'pb_avg_vol_min',       label: 'Avg 10D volume min (default 1000000)', type: 'number' },
          { key: 'pb_rel_vol_min',       label: 'Relative volume min (default 0.75)',   type: 'number' },
          { key: 'pb_market_cap_min',    label: 'Min market cap $ (default 500000000)', type: 'number' },
          { key: 'pb_week_change_min',   label: '1-week change min % (default -3)',     type: 'number' },
          { key: 'pb_ema50_proximity',   label: 'Max % from EMA50 (default 8)',         type: 'number' },
          { key: 'pb_beta_max',          label: 'Max beta (default 2.5)',               type: 'number' },
          { key: 'pb_earnings_days_min', label: 'Min days to earnings (default 15)',    type: 'number' },
          { key: 'pb_ema_spread_min',    label: 'Min EMA20/50 spread % — rejects flat EMA structures (default 1)', type: 'number' },
          { key: 'pb_adx_min',           label: 'Min ADX — trend strength gate (default 20, 0 = off)',            type: 'number' },
          { key: 'pb_52w_high_pct_max',  label: 'Max % below 52-week high — Stage 2 guard (default 30)',           type: 'number' },
          { key: 'pb_3m_perf_min',       label: 'Min 3-month performance % (default -5, e.g. -10 = lenient)',      type: 'number' },
          { key: 'pb_min_revenue_growth', label: 'Min revenue growth % YoY (0 = off, e.g. 10 = require ≥10% top-line growth)', type: 'number', defaultValue: '0' },
          { key: 'pb_block_unknown_earnings', label: 'Block stocks with unknown earnings date (recommended)', type: 'toggle', defaultValue: 'true' },
        ],
      },
      {
        title: 'EMA Ladder',
        fields: [
          { key: 'pb_price_above_ema20',   label: 'Require price > EMA20',   type: 'toggle', defaultValue: 'true' },
          { key: 'pb_ema20_above_ema50',   label: 'Require EMA20 > EMA50',   type: 'toggle', defaultValue: 'true' },
          { key: 'pb_ema50_above_ema100',  label: 'Require EMA50 > EMA100',  type: 'toggle', defaultValue: 'true' },
          { key: 'pb_ema100_above_ema200', label: 'Require EMA100 > EMA200', type: 'toggle', defaultValue: 'true' },
        ],
      },
      {
        title: 'PPST Confirmation',
        fields: [
          { key: 'pb_ppst_required',     label: 'Require PPST bullish confirmation', type: 'toggle', defaultValue: 'true' },
          { key: 'pb_ppst_pivot_period', label: 'Pivot Point Period (TV default 2)', type: 'number' },
          { key: 'pb_ppst_multiplier',   label: 'ATR Factor (TV default 3)',         type: 'number' },
          { key: 'pb_ppst_period',       label: 'ATR Period (TV default 10)',        type: 'number' },
        ],
      },
      {
        title: 'AI Chart Review',
        fields: [
          { key: 'pb_ai_chart_review',    label: 'Enable AI chart review', type: 'toggle', defaultValue: 'false' },
          { key: 'pb_ai_chart_min_grade', label: 'Minimum AI chart grade to pass',
            type: 'select', options: [
              { value: 'A', label: 'A — Pristine setups only' },
              { value: 'B', label: 'B — Solid setups (recommended)' },
              { value: 'C', label: 'C — Allow marginal setups' },
            ] },
        ],
      },
      {
        title: 'Sectors',
        fields: [
          { key: 'pb_excluded_sectors', label: 'Excluded sectors', type: 'sector_picker', span: true,
            defaultValue: 'Consumer Defensive,Energy,Utilities,Real Estate,Basic Materials,Financial Services' },
        ],
      },
      {
        title: 'Entry Order',
        fields: [
          { key: 'pb_entry_order_type', label: 'Entry order type', type: 'select',
            options: [
              { value: 'limit',      label: 'Limit — fills up to entry + slippage% (recommended for pullbacks)' },
              { value: 'stop_limit', label: 'Stop-limit — activates only when price reaches entry' },
              { value: 'market',     label: 'Market — immediate fill at any price' },
            ]
          },
          { key: 'pb_entry_slippage_pct', label: 'Slippage tolerance % (default 0.5)', type: 'number' },
        ],
      },
      {
        title: 'Schedule (ET)',
        fields: [
          { key: 'pb_screener_auto_run',         label: 'Auto-run enabled',                        type: 'toggle',     defaultValue: 'true' },
          { key: 'pb_screener_schedule_days',    label: 'Slot 1 — Days (click to toggle)',          type: 'day_picker', span: true },
          { key: 'pb_screener_schedule_times',   label: 'Slot 1 — Run times (24h ET, e.g. 20:00)', type: 'time_list',  span: true },
          { key: 'pb_screener_schedule_days_2',  label: 'Slot 2 — Days (optional)',                 type: 'day_picker', span: true },
          { key: 'pb_screener_schedule_times_2', label: 'Slot 2 — Run times (24h ET, e.g. 16:30)', type: 'time_list',  span: true },
        ],
      },
    ],
  },

  // ── RS Momentum ───────────────────────────────────────────────────────────
  {
    title: 'RS Momentum Screener',
    description: 'Top relative-strength leaders. Runs alongside Minervini and Pullback.',
    subsections: [
      {
        title: 'Universe & Filters',
        fields: [
          { key: 'rs_screener_enabled',  label: 'Enable RS Momentum screener',                            type: 'toggle', defaultValue: 'true', span: true },
          { key: 'rs_exchanges',         label: 'Exchanges to scan',                                       type: 'exchange_picker', span: true, defaultValue: 'NYSE,NASDAQ' },
          { key: 'rs_price_min',         label: 'Min price $ (default 10)',                                type: 'number' },
          { key: 'rs_price_max',         label: 'Max price $ (0 = no ceiling)',                            type: 'number' },
          { key: 'rs_avg_vol_min',       label: 'Min avg daily volume (default 500000)',                   type: 'number' },
          { key: 'rs_market_cap_min',    label: 'Min market cap $ (default 500000000)',                    type: 'number' },
          { key: 'rs_min_percentile',    label: 'Min RS percentile to qualify (default 75 = top 25%)',     type: 'number' },
          { key: 'rs_max_extension',     label: 'Max % above EMA50 — rejects over-extended stocks (default 15)', type: 'number' },
          { key: 'rs_top_n',             label: 'Top N picks (default 5)',                                 type: 'number' },
          { key: 'rs_require_stage2',    label: 'Require Stage 2 uptrend (price > EMA50 > EMA200)',        type: 'toggle', defaultValue: 'true' },
        ],
      },
      {
        title: 'Sectors',
        fields: [
          { key: 'rs_excluded_sectors',  label: 'Excluded sectors', type: 'sector_picker', span: true,
            defaultValue: 'Energy Minerals,Industrial Services,Non-Energy Minerals,Process Industries,Utilities,Consumer Non-Durables' },
        ],
      },
      {
        title: 'Schedule (ET)',
        fields: [
          { key: 'rs_screener_auto_run',         label: 'Auto-run enabled',                        type: 'toggle',     defaultValue: 'true' },
          { key: 'rs_screener_schedule_days',    label: 'Slot 1 — Days (click to toggle)',          type: 'day_picker', span: true },
          { key: 'rs_screener_schedule_times',   label: 'Slot 1 — Run times (24h ET, e.g. 20:00)', type: 'time_list',  span: true },
          { key: 'rs_screener_schedule_days_2',  label: 'Slot 2 — Days (optional)',                 type: 'day_picker', span: true },
          { key: 'rs_screener_schedule_times_2', label: 'Slot 2 — Run times (24h ET, e.g. 16:30)', type: 'time_list',  span: true },
        ],
      },
    ],
  },

  // ── Combined Screener ─────────────────────────────────────────────────────
  {
    title: 'Combined Screener',
    description: 'Runs all three strategies (Minervini + Pullback + RS) in a single scan. Recommended as the primary weekly plan generator — one TV API call instead of three.',
    subsections: [
      {
        title: 'Schedule (ET)',
        fields: [
          { key: 'combined_screener_auto_run',         label: 'Auto-run enabled',                        type: 'toggle',     defaultValue: 'true' },
          { key: 'combined_screener_schedule_days',    label: 'Slot 1 — Days (click to toggle)',          type: 'day_picker', span: true },
          { key: 'combined_screener_schedule_times',   label: 'Slot 1 — Run times (24h ET, e.g. 20:00)', type: 'time_list',  span: true },
          { key: 'combined_screener_schedule_days_2',  label: 'Slot 2 — Days (optional)',                 type: 'day_picker', span: true },
          { key: 'combined_screener_schedule_times_2', label: 'Slot 2 — Run times (24h ET, e.g. 16:30)', type: 'time_list',  span: true },
        ],
      },
    ],
  },

  // ── Monitor ────────────────────────────────────────────────────────────────
  {
    title: 'Monitor',
    description: 'In-cycle position management — exits, trailing stops, slot refills.',
    subsections: [
      {
        title: 'Cycle',
        fields: [
          { key: 'monitor_enabled',          label: 'Monitor enabled (auto-place exits & manage positions)', type: 'toggle', defaultValue: 'true' },
          { key: 'monitor_interval_minutes', label: 'Monitor check frequency', type: 'select',
            options: [
              { value: '1',  label: 'Every 1 minute (fastest reactions, max API load)' },
              { value: '5',  label: 'Every 5 minutes' },
              { value: '10', label: 'Every 10 minutes' },
              { value: '15', label: 'Every 15 minutes' },
              { value: '30', label: 'Every 30 minutes (default)' },
              { value: '60', label: 'Every 60 minutes' },
            ],
            defaultValue: '30',
          },
          { key: 'auto_execute', label: 'Auto-execute new entries on Monday open', type: 'toggle', defaultValue: 'true' },
        ],
      },
      {
        title: 'Apex Loss Prevention',
        fields: [
          { key: 'daily_drawdown_halt_pct',  label: 'Daily drawdown halt % — blocks all new buys when day P&L drops below (default 5.0, 0 = off)', type: 'number' },
          { key: 'time_stop_days',           label: 'Time stop — trading days before dead-money exit fires (default 10, 0 = off)',           type: 'number' },
          { key: 'time_stop_max_gain_pct',   label: 'Time stop — min unrealized gain % to survive; above this the position is left alone (default 2.0)', type: 'number' },
        ],
      },
    ],
  },

  // ── Infra ──────────────────────────────────────────────────────────────────
  {
    title: 'Integrations',
    fields: [
      { key: 'tv_chart_layout_id', label: 'TradingView chart layout ID (paste from chart URL — optional)', type: 'text', span: true },
      { key: 'tv_username',    label: 'TradingView Username', type: 'text'     },
      { key: 'tv_password',    label: 'TradingView Password', type: 'password' },
      { key: 'watchlist',      label: 'Monitor Watchlist (CSV)',  type: 'text', span: true },
      { key: 'webhook_secret', label: 'Webhook Secret',       type: 'password' },
    ],
  },
  {
    title: 'Alpaca Credentials',
    description: 'Shared by Minervini, Pullback, and RS. Dual Momentum has its own keys in the DM tab.',
    fields: [
      { key: 'alpaca_paper_key',    label: 'Paper API Key',    type: 'password', span: true },
      { key: 'alpaca_paper_secret', label: 'Paper API Secret', type: 'password', span: true },
      { key: 'alpaca_live_key',     label: 'Live API Key',     type: 'password', span: true },
      { key: 'alpaca_live_secret',  label: 'Live API Secret',  type: 'password', span: true },
    ],
  },
  {
    title: 'AI Analysis',
    fields: [
      {
        key: 'ai_provider', label: 'Provider', type: 'select',
        options: [
          { value: 'anthropic',         label: 'Anthropic (Claude)' },
          { value: 'openai',            label: 'OpenAI (GPT-4 / o-series)' },
          { value: 'openai_compatible', label: 'OpenAI-compatible (xAI, DeepSeek, Mistral, Groq…)' },
        ],
      },
      { key: 'ai_api_key',  label: 'API Key',                              type: 'password', span: true },
      { key: 'ai_model',    label: 'Model ID (leave blank for provider default)', type: 'text' },
      { key: 'ai_base_url', label: 'Base URL (OpenAI-compatible only)',    type: 'text', span: true },
    ],
  },
]

// All sections start collapsed — click any header to expand.
const DEFAULT_OPEN = new Set()

export default function SettingsPanel() {
  const qc            = useQueryClient()
  const { data = {} } = useQuery('settings', fetchSettings)
  const { data: me, refetch: refetchMe } = useQuery('me', fetchMe, { staleTime: 60000 })
  const [saving, setSaving]   = useState(null)
  const [tvOpen, setTvOpen]   = useState(false)
  const [tvList, setTvList]   = useState([])
  const [tvLoading, setTvLoading] = useState(false)
  const [tvError, setTvError] = useState('')
  const [openSections, setOpenSections] = useState(DEFAULT_OPEN)

  function toggleSection(title) {
    setOpenSections(prev => {
      const next = new Set(prev)
      if (next.has(title)) next.delete(title); else next.add(title)
      return next
    })
  }

  async function loadTvScreeners() {
    setTvLoading(true); setTvError('')
    try {
      const res = await fetchTvScreeners()
      const list = res.screeners || []
      setTvList(list)
      if (res.message && list.length === 0) {
        setTvError(res.message)
      } else {
        setTvOpen(true)
      }
    } catch (e) {
      setTvError(e?.response?.data?.detail || 'Could not fetch screeners — check TradingView credentials in Settings → Integrations.')
    } finally {
      setTvLoading(false)
    }
  }

  async function save(key, value) {
    setSaving(key)
    try {
      await updateSetting(key, value)
      qc.invalidateQueries('settings')
      qc.invalidateQueries('account')
    } finally { setSaving(null) }
  }

  return (
    <div className="space-y-3">

      {/* Account & Security */}
      {(() => {
        const isOpen = openSections.has('Account & Security')
        return (
          <div className="bg-card border border-border rounded-xl overflow-hidden">
            <button
              onClick={() => toggleSection('Account & Security')}
              className="w-full flex items-center justify-between px-4 py-3 hover:bg-white/5 transition-colors"
            >
              <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">Account & Security</h3>
              <svg className={`w-4 h-4 text-slate-400 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`}
                   fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
              </svg>
            </button>
            {isOpen && (
              <div className="px-4 pb-4 border-t border-border mt-0 space-y-3 pt-3">
                {me && (
                  <div className="flex items-center gap-3 pb-3 border-b border-border">
                    <div className="w-9 h-9 rounded-full bg-accent/20 text-accent flex items-center justify-center font-bold">
                      {me.username[0].toUpperCase()}
                    </div>
                    <div>
                      <p className="text-sm text-slate-200 font-medium">{me.username}</p>
                      <p className="text-xs text-slate-500">{me.email} · <span className="capitalize">{me.role}</span></p>
                    </div>
                  </div>
                )}
                <TwoFactorSetup enabled={me?.totp_enabled ?? false} onChanged={refetchMe} />
              </div>
            )}
          </div>
        )
      })()}

      {SECTIONS.map(section => {
        const isOpen = openSections.has(section.title)
        return (
          <div key={section.title} className="bg-card border border-border rounded-xl overflow-hidden">
            {/* Header — always visible, click to toggle */}
            <button
              onClick={() => toggleSection(section.title)}
              className="w-full flex items-center justify-between px-4 py-3 hover:bg-white/5 transition-colors"
            >
              <div className="text-left">
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">
                  {section.title}
                </h3>
                {section.description && (
                  <p className="text-xs text-slate-500 mt-0.5 normal-case font-normal tracking-normal">
                    {section.description}
                  </p>
                )}
              </div>
              <svg
                className={`w-4 h-4 text-slate-400 transition-transform duration-200 flex-shrink-0 ${isOpen ? 'rotate-180' : ''}`}
                fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
              </svg>
            </button>

            {/* Body — shown only when open */}
            {isOpen && (
              <div className="px-4 pb-4 border-t border-border">
                {section.subsections ? (
                  <div className="space-y-5 mt-4">
                    {section.subsections.map(sub => (
                      <div key={sub.title}>
                        <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2 pb-1 border-b border-border/50">
                          {sub.title}
                        </h4>
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                          {sub.fields.map(f => (
                            <div key={f.key} className={f.span ? 'sm:col-span-2' : ''}>
                              <Field
                                field={f}
                                value={data[f.key] ?? ''}
                                saving={saving === f.key}
                                onSave={val => save(f.key, val)}
                                tvScreeners={f.type === 'tv_screener' ? tvList : undefined}
                                tvLoading={f.type === 'tv_screener' ? tvLoading : undefined}
                                tvError={f.type === 'tv_screener' ? tvError : undefined}
                                tvOpen={f.type === 'tv_screener' ? tvOpen : undefined}
                                onBrowseTv={f.type === 'tv_screener' ? loadTvScreeners : undefined}
                                onCloseTv={f.type === 'tv_screener' ? () => setTvOpen(false) : undefined}
                              />
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-3">
                    {section.fields.map(f => (
                      <div key={f.key} className={f.span ? 'sm:col-span-2' : ''}>
                        <Field
                          field={f}
                          value={data[f.key] ?? ''}
                          saving={saving === f.key}
                          onSave={val => save(f.key, val)}
                          tvScreeners={f.type === 'tv_screener' ? tvList : undefined}
                          tvLoading={f.type === 'tv_screener' ? tvLoading : undefined}
                          tvError={f.type === 'tv_screener' ? tvError : undefined}
                          tvOpen={f.type === 'tv_screener' ? tvOpen : undefined}
                          onBrowseTv={f.type === 'tv_screener' ? loadTvScreeners : undefined}
                          onCloseTv={f.type === 'tv_screener' ? () => setTvOpen(false) : undefined}
                        />
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Info tooltip shown on hover next to every field label ────────────────────
function InfoTooltip({ text }) {
  const [visible, setVisible] = useState(false)
  if (!text) return null
  return (
    <span
      className="relative inline-flex items-center flex-shrink-0 cursor-help"
      onMouseEnter={() => setVisible(true)}
      onMouseLeave={() => setVisible(false)}
    >
      {/* ℹ circle icon */}
      <svg
        className="w-3.5 h-3.5 text-slate-500 hover:text-slate-300 transition-colors"
        viewBox="0 0 20 20" fill="currentColor"
      >
        <path fillRule="evenodd" clipRule="evenodd"
          d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a.75.75 0 000 1.5h.253a.25.25 0 01.244.304l-.459 2.066A1.75 1.75 0 0010.747 15H11a.75.75 0 000-1.5h-.253a.25.25 0 01-.244-.304l.459-2.066A1.75 1.75 0 009.253 9H9z"
        />
      </svg>
      {visible && (
        <div className="absolute bottom-full left-0 mb-2 w-64 bg-slate-900 border border-slate-600 rounded-lg p-2.5 text-xs text-slate-300 shadow-2xl z-50 pointer-events-none leading-relaxed">
          {text}
          {/* caret */}
          <div className="absolute top-full left-3 border-[5px] border-transparent border-t-slate-600" />
        </div>
      )}
    </span>
  )
}

function Field({ field, value, saving, onSave,
                 tvScreeners, tvLoading, tvError, tvOpen, onBrowseTv, onCloseTv }) {
  const [local, setLocal] = useState(null)
  // Use local (optimistic) → DB value → field default → empty string
  const current = local ?? (value !== '' && value !== undefined ? value : (field.defaultValue ?? ''))
  // Tooltip text: field-level override first, then central lookup
  const tip = field.tooltip || FIELD_TOOLTIPS[field.key] || ''

  if (field.type === 'tv_screener') {
    return (
      <div className="bg-surface rounded-lg p-3">
        <label className="text-xs text-slate-400 flex items-center gap-1 mb-1">
          <span>{field.label}</span><InfoTooltip text={tip} />
        </label>
        <div className="flex gap-2 items-center">
          <input
            type="text"
            value={local ?? value ?? ''}
            onChange={e => setLocal(e.target.value)}
            placeholder="e.g. My Pullback Screener"
            className="flex-1 bg-transparent text-slate-200 text-sm outline-none border-b border-border focus:border-accent"
          />
          {(local !== null && local !== value) && (
            <button
              onClick={() => { onSave(local); setLocal(null) }}
              disabled={saving}
              className="text-xs text-accent hover:text-indigo-300 disabled:opacity-50 flex-shrink-0"
            >{saving ? '…' : 'Save'}</button>
          )}
          <button
            onClick={onBrowseTv}
            disabled={tvLoading}
            className="text-xs bg-accent/20 text-accent hover:bg-accent/30 rounded px-2 py-1 flex-shrink-0 disabled:opacity-50"
          >{tvLoading ? 'Loading…' : 'Browse'}</button>
        </div>
        {tvError && <p className="text-xs text-red-400 mt-1">{tvError}</p>}
        {(local ?? value) && (
          <p className="text-xs text-emerald-400 mt-1">
            ✓ Using TV screener — app filters below are bypassed
          </p>
        )}
        {!(local ?? value) && (
          <p className="text-xs text-slate-500 mt-1">
            Blank = use app filters below (Option A — server-side TV scan)
          </p>
        )}
        {tvOpen && (
          <TvScreenerPicker
            screeners={tvScreeners}
            onSelect={name => { setLocal(name); onSave(name); onCloseTv() }}
            onClose={onCloseTv}
          />
        )}
      </div>
    )
  }

  if (field.type === 'toggle') {
    const on = current === 'true'
    return (
      <div className="flex items-center justify-between bg-surface rounded-lg p-3 h-full">
        <span className="text-sm text-slate-300 flex items-center gap-1">
          {field.label}<InfoTooltip text={tip} />
        </span>
        <button
          onClick={() => {
            const next = on ? 'false' : 'true'
            setLocal(next)   // optimistic — shows instantly
            onSave(next)
          }}
          disabled={saving}
          className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 ${on ? 'bg-accent' : 'bg-slate-700'} disabled:opacity-50`}
        >
          <span className={`absolute top-1 w-4 h-4 bg-white rounded-full transition-all ${on ? 'left-6' : 'left-1'}`} />
        </button>
      </div>
    )
  }

  if (field.type === 'exchange_picker') {
    const selected = new Set(
      (current || field.defaultValue || 'NYSE,NASDAQ')
        .split(',').map(e => e.trim().toUpperCase()).filter(Boolean)
    )
    function toggleExchange(ex) {
      const next = new Set(selected)
      if (next.has(ex)) next.delete(ex); else next.add(ex)
      const val = EXCHANGES.filter(e => next.has(e)).join(',')
      setLocal(val)
      onSave(val || 'NYSE,NASDAQ')
    }
    return (
      <div className="bg-surface rounded-lg p-3">
        <label className="text-xs text-slate-400 flex items-center gap-1 mb-2">
          <span>{field.label}</span><InfoTooltip text={tip} />
        </label>
        <div className="flex gap-1.5 flex-wrap">
          {EXCHANGES.map(ex => (
            <button
              key={ex}
              onClick={() => toggleExchange(ex)}
              disabled={saving}
              className={`px-3 py-1 rounded text-xs font-medium transition-colors disabled:opacity-50 ${
                selected.has(ex)
                  ? 'bg-accent text-white'
                  : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
              }`}
            >{ex}</button>
          ))}
          {selected.size === 0 && (
            <span className="text-xs text-amber-400 self-center ml-1">⚠ No exchanges selected — screener will return nothing</span>
          )}
        </div>
        <p className="text-[10px] text-slate-500 mt-1.5">
          NYSE + NASDAQ covers ~95% of liquid US equities. Add AMEX for small-caps, OTC for pink sheets.
        </p>
      </div>
    )
  }

  if (field.type === 'sector_picker') {
    const defaultVal = field.defaultValue || 'Energy Minerals,Industrial Services,Non-Energy Minerals,Process Industries,Utilities,Consumer Non-Durables'
    const excluded = new Set(
      (current || defaultVal).split(',').map(s => s.trim()).filter(Boolean)
    )
    function toggleSector(name) {
      const next = new Set(excluded)
      if (next.has(name)) next.delete(name); else next.add(name)
      const val = ALL_SECTORS.filter(s => next.has(s.name)).map(s => s.name).join(',')
      setLocal(val)
      onSave(val)
    }
    const growthSectors = ALL_SECTORS.filter(s => s.growth)
    const defensiveSectors = ALL_SECTORS.filter(s => !s.growth)
    return (
      <div className="bg-surface rounded-lg p-3">
        <label className="text-xs text-slate-400 flex items-center gap-1 mb-1">
          <span>{field.label}</span><InfoTooltip text={tip} />
        </label>
        <p className="text-[10px] text-slate-500 mb-2">
          Highlighted sectors are <span className="text-red-400 font-medium">blocked</span>. Click to toggle. Growth sectors are shown first.
        </p>
        <div className="space-y-2">
          <div>
            <p className="text-[10px] text-emerald-500 font-medium uppercase tracking-wide mb-1">Growth</p>
            <div className="flex gap-1.5 flex-wrap">
              {growthSectors.map(s => (
                <button
                  key={s.name}
                  onClick={() => toggleSector(s.name)}
                  disabled={saving}
                  title={s.name}
                  className={`px-2.5 py-1 rounded text-xs font-medium transition-colors disabled:opacity-50 ${
                    excluded.has(s.name)
                      ? 'bg-red-500/30 text-red-300 ring-1 ring-red-500/50'
                      : 'bg-emerald-500/15 text-emerald-400 hover:bg-emerald-500/25'
                  }`}
                >{s.short}</button>
              ))}
            </div>
          </div>
          <div>
            <p className="text-[10px] text-slate-500 font-medium uppercase tracking-wide mb-1">Defensive / Commodity</p>
            <div className="flex gap-1.5 flex-wrap">
              {defensiveSectors.map(s => (
                <button
                  key={s.name}
                  onClick={() => toggleSector(s.name)}
                  disabled={saving}
                  title={s.name}
                  className={`px-2.5 py-1 rounded text-xs font-medium transition-colors disabled:opacity-50 ${
                    excluded.has(s.name)
                      ? 'bg-red-500/30 text-red-300 ring-1 ring-red-500/50'
                      : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
                  }`}
                >{s.short}</button>
              ))}
            </div>
          </div>
        </div>
        <p className="text-[10px] text-slate-500 mt-2">
          {excluded.size === 0
            ? '⚠ No sectors blocked — all sectors allowed through'
            : `${excluded.size} sector${excluded.size > 1 ? 's' : ''} blocked`}
        </p>
      </div>
    )
  }

  if (field.type === 'day_picker') {
    const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    const selected   = new Set(
      (current || '').split(',').map(d => d.trim()).filter(Boolean).map(Number)
    )
    function toggleDay(idx) {
      const next = new Set(selected)
      if (next.has(idx)) next.delete(idx); else next.add(idx)
      const val = [...next].sort((a, b) => a - b).join(',')
      setLocal(val)
      onSave(val)
    }
    return (
      <div className="bg-surface rounded-lg p-3">
        <label className="text-xs text-slate-400 flex items-center gap-1 mb-2">
          <span>{field.label}</span><InfoTooltip text={tip} />
        </label>
        <div className="flex gap-1.5 flex-wrap">
          {DAY_LABELS.map((name, idx) => (
            <button
              key={idx}
              onClick={() => toggleDay(idx)}
              disabled={saving}
              className={`px-2.5 py-1 rounded text-xs font-medium transition-colors disabled:opacity-50 ${
                selected.has(idx)
                  ? 'bg-accent text-white'
                  : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
              }`}
            >{name}</button>
          ))}
          {selected.size === 0 && (
            <span className="text-xs text-slate-500 self-center ml-1">No days selected — won't auto-run</span>
          )}
        </div>
      </div>
    )
  }

  if (field.type === 'time_list') {
    const rawVal = local ?? current ?? ''
    const times  = rawVal.split(',').map(t => t.trim()).filter(Boolean)

    function saveTimes(newTimes) {
      const val = newTimes.filter(Boolean).join(',')
      setLocal(val)
      onSave(val)
    }

    return (
      <div className="bg-surface rounded-lg p-3">
        <label className="text-xs text-slate-400 flex items-center gap-1 mb-2">
          <span>{field.label}</span><InfoTooltip text={tip} />
        </label>
        <div className="space-y-2">
          {times.map((t, idx) => (
            <div key={`${t}-${idx}`} className="flex gap-2 items-center">
              <input
                type="time"
                defaultValue={t}
                onBlur={e => {
                  if (e.target.value !== t) {
                    const next = [...times]; next[idx] = e.target.value; saveTimes(next)
                  }
                }}
                className="flex-1 bg-transparent text-slate-200 text-sm outline-none border-b border-border focus:border-accent"
              />
              <button
                onClick={() => saveTimes(times.filter((_, i) => i !== idx))}
                className="text-slate-500 hover:text-red-400 text-base leading-none flex-shrink-0"
              >×</button>
            </div>
          ))}
          {times.length === 0 && (
            <p className="text-xs text-slate-500">No times set — screener won't auto-run.</p>
          )}
        </div>
        <button
          onClick={() => saveTimes([...times, '20:00'])}
          className="mt-2 text-xs text-accent hover:text-indigo-300"
        >+ Add time</button>
      </div>
    )
  }

  if (field.type === 'select') {
    return (
      <div className="bg-surface rounded-lg p-3">
        <label className="text-xs text-slate-400 flex items-center gap-1 mb-1">
          <span>{field.label}</span><InfoTooltip text={tip} />
        </label>
        <select
          value={current}
          onChange={e => { setLocal(e.target.value); onSave(e.target.value) }}
          disabled={saving}
          className="w-full bg-transparent text-slate-200 text-sm outline-none border-b border-border focus:border-accent cursor-pointer"
        >
          {field.options.map(o => (
            <option key={o.value} value={o.value} className="bg-slate-800">{o.label}</option>
          ))}
        </select>
      </div>
    )
  }

  const isDirty = local !== null && local !== value

  return (
    <div className="bg-surface rounded-lg p-3">
      <label className="text-xs text-slate-400 flex items-center gap-1 mb-1">
        <span>{field.label}</span><InfoTooltip text={tip} />
      </label>
      <div className="flex gap-2 items-center">
        <input
          type={field.type === 'number' ? 'number' : field.type === 'password' ? 'password' : field.type === 'time' ? 'time' : 'text'}
          value={current}
          onChange={e => setLocal(e.target.value)}
          onBlur={() => { if (field.type === 'time' && isDirty) { onSave(local); setLocal(null) } }}
          className="flex-1 bg-transparent text-slate-200 text-sm outline-none border-b border-border focus:border-accent"
        />
        {isDirty && field.type !== 'time' && (
          <button
            onClick={() => { onSave(local); setLocal(null) }}
            disabled={saving}
            className="text-xs text-accent hover:text-indigo-300 disabled:opacity-50 flex-shrink-0"
          >
            {saving ? '…' : 'Save'}
          </button>
        )}
      </div>
    </div>
  )
}

function TvScreenerPicker({ screeners, onSelect, onClose }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
         onClick={onClose}>
      <div className="bg-card border border-border rounded-xl p-4 w-80 max-h-96 flex flex-col shadow-2xl"
           onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-slate-200">Your TradingView Screeners</h3>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-200 text-lg leading-none">×</button>
        </div>
        {screeners.length === 0 ? (
          <p className="text-sm text-slate-400 py-4 text-center">
            No saved screeners found.<br />
            <span className="text-xs text-slate-500">Create and save a screener in TradingView first.</span>
          </p>
        ) : (
          <ul className="overflow-y-auto space-y-1">
            {screeners.map(s => (
              <li key={s.id}>
                <button
                  onClick={() => onSelect(s.name)}
                  className="w-full text-left px-3 py-2 rounded-lg hover:bg-accent/20 text-sm text-slate-200 flex items-center justify-between group"
                >
                  <span>{s.name}</span>
                  {s.symbol_count != null && (
                    <span className="text-xs text-slate-500 group-hover:text-slate-300">
                      {s.symbol_count} stocks
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-slate-500 mt-3 border-t border-border pt-2">
          Selecting a screener will use its exact TV filter set. App filters below will be bypassed.
        </p>
      </div>
    </div>
  )
}
