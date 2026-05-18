-- SMS conversation history for Angelina text-chat feature
-- Run this in your Supabase SQL editor (Dashboard → SQL Editor → New query)

CREATE TABLE IF NOT EXISTS sms_conversations (
  id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  phone_number   TEXT        NOT NULL,
  -- business_id intentionally left as plain UUID (no FK) so the table works
  -- before a businesses table exists. Add the constraint later if needed:
  --   ALTER TABLE sms_conversations
  --     ADD CONSTRAINT fk_business FOREIGN KEY (business_id) REFERENCES businesses(id);
  business_id    UUID,
  messages       JSONB       NOT NULL DEFAULT '[]',
  last_activity  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sms_phone ON sms_conversations(phone_number);
CREATE INDEX IF NOT EXISTS idx_sms_activity ON sms_conversations(last_activity DESC);

-- Each message object in the messages JSONB array has the shape:
--   { "role": "user"|"assistant", "content": "...", "ts": "<iso8601>" }
