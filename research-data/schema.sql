PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
  code TEXT PRIMARY KEY,
  market TEXT NOT NULL CHECK (market IN ('SH','SZ','BJ','UNKNOWN')),
  name TEXT NOT NULL,
  industry_l1 TEXT,
  industry_source TEXT,
  name_asof TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS filings (
  announcement_id TEXT PRIMARY KEY,
  code TEXT NOT NULL REFERENCES companies(code),
  report_type TEXT NOT NULL,
  fiscal_period TEXT NOT NULL,
  title TEXT NOT NULL,
  disclosed_at TEXT NOT NULL,
  source_site TEXT NOT NULL,
  source_url TEXT NOT NULL,
  original_json TEXT NOT NULL,
  local_pdf TEXT,
  pdf_sha256 TEXT,
  pdf_bytes INTEGER,
  fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_site, source_url)
);
CREATE INDEX IF NOT EXISTS idx_filings_disclosed ON filings(disclosed_at DESC);
CREATE INDEX IF NOT EXISTS idx_filings_company_period ON filings(code, fiscal_period, disclosed_at DESC);

CREATE TABLE IF NOT EXISTS financial_metrics (
  code TEXT NOT NULL REFERENCES companies(code),
  fiscal_period TEXT NOT NULL,
  metric TEXT NOT NULL,
  value REAL,
  unit TEXT NOT NULL,
  basis TEXT NOT NULL,
  source_announcement_id TEXT NOT NULL REFERENCES filings(announcement_id),
  extracted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (code, fiscal_period, metric, basis, source_announcement_id)
);

CREATE TABLE IF NOT EXISTS daily_prices (
  code TEXT NOT NULL REFERENCES companies(code),
  trade_date TEXT NOT NULL,
  close REAL NOT NULL,
  volume REAL,
  turnover REAL,
  adjustment TEXT NOT NULL DEFAULT 'none',
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (code, trade_date, adjustment, source)
);

CREATE TABLE IF NOT EXISTS research_reports (
  report_id TEXT PRIMARY KEY,
  code TEXT NOT NULL REFERENCES companies(code),
  fiscal_period TEXT,
  written_at TEXT NOT NULL,
  rating TEXT NOT NULL CHECK (rating IN ('buy','watch','avoid','insufficient')),
  vault_note TEXT NOT NULL UNIQUE,
  source_announcement_id TEXT REFERENCES filings(announcement_id),
  status TEXT NOT NULL DEFAULT 'draft',
  reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_code_date ON research_reports(code, written_at DESC);

CREATE TABLE IF NOT EXISTS collection_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  query TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  examined INTEGER NOT NULL DEFAULT 0,
  accepted INTEGER NOT NULL DEFAULT 0,
  pdf_saved INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
