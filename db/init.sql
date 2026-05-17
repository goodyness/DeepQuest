CREATE TABLE IF NOT EXISTS pages (
    id SERIAL PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    final_url TEXT,
    domain TEXT NOT NULL,
    title TEXT,
    raw_html TEXT,
    content_hash TEXT UNIQUE,
    content_type TEXT DEFAULT 'html',
    scraped_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    status_code INTEGER,
    processed BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_pages_domain ON pages(domain);
CREATE INDEX IF NOT EXISTS idx_pages_processed ON pages(processed);
CREATE INDEX IF NOT EXISTS idx_pages_content_hash ON pages(content_hash);
