CREATE TABLE IF NOT EXISTS signal_log (
    id          SERIAL PRIMARY KEY,
    symbol      VARCHAR(10) NOT NULL,
    signal      VARCHAR(30) NOT NULL,
    score       INTEGER,
    price       NUMERIC(12,4),
    mode        VARCHAR(10) NOT NULL DEFAULT 'paper',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trade_log (
    id            SERIAL PRIMARY KEY,
    symbol        VARCHAR(10) NOT NULL,
    action        VARCHAR(10) NOT NULL,   -- BUY / SELL
    qty           NUMERIC(12,4),
    price         NUMERIC(12,4),
    order_id      VARCHAR(64),
    trigger       VARCHAR(30),            -- signal that triggered the trade
    mode          VARCHAR(10) NOT NULL DEFAULT 'paper',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS alert_log (
    id          SERIAL PRIMARY KEY,
    level       VARCHAR(10) NOT NULL,  -- INFO / URGENT / OPPORTUNITY
    message     TEXT NOT NULL,
    sent        BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS settings (
    key   VARCHAR(64) PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_plan (
    id            SERIAL PRIMARY KEY,
    week_start    DATE NOT NULL,
    symbol        VARCHAR(10) NOT NULL,
    rank          INTEGER NOT NULL,
    score         INTEGER NOT NULL,
    signal        VARCHAR(30),
    entry_price   NUMERIC(12,4),
    stop_price    NUMERIC(12,4),
    target1       NUMERIC(12,4),
    target2       NUMERIC(12,4),
    position_size INTEGER,
    risk_amount   NUMERIC(12,2),
    rationale     TEXT,
    status        VARCHAR(20) DEFAULT 'PENDING',
    mode          VARCHAR(10) NOT NULL DEFAULT 'paper',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Defaults
INSERT INTO settings (key, value) VALUES
    ('trading_mode',       'paper'),
    ('auto_execute',       'true'),
    ('risk_pct',           '2.0'),
    ('stop_loss_pct',      '8.0'),
    ('max_positions',      '10'),
    ('monitor_interval',   '60'),
    ('webhook_secret',     ''),
    ('screener_universe',  '')
ON CONFLICT (key) DO NOTHING;

CREATE INDEX ON signal_log   (symbol, created_at DESC);
CREATE INDEX ON trade_log    (symbol, created_at DESC);
CREATE INDEX ON alert_log    (created_at DESC);
CREATE INDEX ON weekly_plan  (week_start DESC, rank ASC);
