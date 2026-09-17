-- Public EIP-3009 identity only. Never persist a signature or secret here.
-- Legacy rows remain NULL; their authorization cannot be reconstructed safely.
ALTER TABLE x402_payments ADD COLUMN authorization_nonce TEXT;
ALTER TABLE x402_payments ADD COLUMN asset TEXT;
ALTER TABLE x402_payments ADD COLUMN valid_after INTEGER;
CREATE INDEX IF NOT EXISTS idx_x402_authorization_identity
ON x402_payments(network, asset, wallet_address, authorization_nonce);
