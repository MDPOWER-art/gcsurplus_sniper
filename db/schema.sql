-- GC Surplus Sniper SQLite Schema
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- Table for parsed auction listings
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    current_bid REAL NOT NULL DEFAULT 0.0,
    minimum_bid REAL NOT NULL DEFAULT 0.0,
    location TEXT NOT NULL DEFAULT '',
    estimated_value REAL DEFAULT 0.0,
    cfo_net_profit REAL DEFAULT 0.0,
    suggested_max_bid REAL DEFAULT 0.0,
    closing_date TEXT NOT NULL,
    url TEXT NOT NULL,
    image_url TEXT,
    description TEXT,
    is_analyzed INTEGER NOT NULL DEFAULT 0,
    is_viable INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    rejection_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'utc'))
);

-- Table for active/executed bids and snipes
CREATE TABLE IF NOT EXISTS active_bids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    bid_amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING, SNIPED, OUTBID, WON, REJECTED_EXPOSURE, FAILED
    error_message TEXT,
    placed_at TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    FOREIGN KEY (listing_id) REFERENCES listings (id)
);

-- Table for internal system logs and audit trail
CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL, -- INFO, WARNING, ERROR, CRITICAL
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'utc'))
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_listings_closing_date ON listings(closing_date);
CREATE INDEX IF NOT EXISTS idx_listings_is_viable ON listings(is_viable);
CREATE INDEX IF NOT EXISTS idx_active_bids_listing ON active_bids(listing_id);
CREATE INDEX IF NOT EXISTS idx_active_bids_status ON active_bids(status);
