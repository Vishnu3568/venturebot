-- Step 74: D1 schema for VentureBot GUIDE_ACCESS aggregate telemetry
CREATE TABLE IF NOT EXISTS guide_access_daily (
    experiment_id TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'guide_access',
    date_bucket TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (experiment_id, event_type, date_bucket)
);
