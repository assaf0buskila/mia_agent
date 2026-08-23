ALTER TABLE channel_identities ADD COLUMN manychat_subscriber_id VARCHAR(255) DEFAULT '' NOT NULL;
ALTER TABLE channel_identities ADD COLUMN manychat_conversation_id VARCHAR(255) DEFAULT '' NOT NULL;
