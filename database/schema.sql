-- Third-party Vendor Advertising Management Schema

CREATE TABLE IF NOT EXISTS advertisers (
    id INTEGER PRIMARY KEY,
    company_name TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    industry TEXT NOT NULL,
    company_size TEXT NOT NULL,
    annual_revenue_tier TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review',
    assigned_cpc_cents INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_advertisers_status ON advertisers(status);
CREATE INDEX IF NOT EXISTS idx_advertisers_industry ON advertisers(industry);

CREATE TABLE IF NOT EXISTS ads (
    id INTEGER PRIMARY KEY,
    advertiser_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    verbiage TEXT NOT NULL,
    image_path TEXT NOT NULL,
    target_url TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (advertiser_id) REFERENCES advertisers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ads_advertiser_id ON ads(advertiser_id);
CREATE INDEX IF NOT EXISTS idx_ads_is_active ON ads(is_active);

CREATE TABLE IF NOT EXISTS ad_analytics (
    id INTEGER PRIMARY KEY,
    ad_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (ad_id) REFERENCES ads(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ad_analytics_ad_id ON ad_analytics(ad_id);
CREATE INDEX IF NOT EXISTS idx_ad_analytics_event_type ON ad_analytics(event_type);
CREATE INDEX IF NOT EXISTS idx_ad_analytics_timestamp ON ad_analytics(timestamp);
