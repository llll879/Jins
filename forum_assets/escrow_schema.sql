-- Escrow extension. Safe to apply repeatedly.
ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS fee_paid BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS asset_kind TEXT NOT NULL DEFAULT 'gift';
ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS asset_ref TEXT;
ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS seller_confirmed_at TIMESTAMPTZ;
ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS buyer_confirmed_at TIMESTAMPTZ;
ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS receipt_path TEXT;
ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS fee_charge_id TEXT;
ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS payment_deadline TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS escrow_config(
  id SMALLINT PRIMARY KEY DEFAULT 1 CHECK(id=1), enabled BOOLEAN NOT NULL DEFAULT FALSE,
  reserve_min BIGINT NOT NULL DEFAULT 100, reserve_balance BIGINT NOT NULL DEFAULT 0,
  fee_percent NUMERIC(6,3) NOT NULL DEFAULT 1.000, payment_timeout_minutes INT NOT NULL DEFAULT 30,
  userbot_online BOOLEAN NOT NULL DEFAULT FALSE, userbot_balance BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO escrow_config(id) VALUES(1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS escrow_assets(
  id BIGSERIAL PRIMARY KEY, deal_id BIGINT NOT NULL UNIQUE REFERENCES escrow_deals(id) ON DELETE CASCADE,
  sender_id BIGINT NOT NULL, kind TEXT NOT NULL, telegram_ref TEXT NOT NULL,
  gift_json JSONB NOT NULL DEFAULT '{}'::jsonb, convert_stars BIGINT NOT NULL DEFAULT 0,
  is_collectible BOOLEAN NOT NULL DEFAULT FALSE, transfer_stars BIGINT NOT NULL DEFAULT 0,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(), transferred_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS escrow_messages(
  id BIGSERIAL PRIMARY KEY, deal_id BIGINT NOT NULL REFERENCES escrow_deals(id) ON DELETE CASCADE,
  sender_id BIGINT NOT NULL, body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 2000),
  system BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS escrow_messages_deal_idx ON escrow_messages(deal_id,id);
CREATE TABLE IF NOT EXISTS escrow_disputes(
  id BIGSERIAL PRIMARY KEY, deal_id BIGINT NOT NULL UNIQUE REFERENCES escrow_deals(id) ON DELETE CASCADE,
  opened_by BIGINT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  resolved_by BIGINT, resolution TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS escrow_receipts(
  id BIGSERIAL PRIMARY KEY, deal_id BIGINT NOT NULL REFERENCES escrow_deals(id) ON DELETE CASCADE,
  outcome TEXT NOT NULL, data JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(deal_id,outcome)
);
CREATE TABLE IF NOT EXISTS escrow_processed_gifts(
  telegram_ref TEXT PRIMARY KEY, sender_id BIGINT NOT NULL, gift_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
