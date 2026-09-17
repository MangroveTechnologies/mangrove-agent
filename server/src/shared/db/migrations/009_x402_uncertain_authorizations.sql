-- A server error/replay cache never proved that an authorization was canceled.
-- Restore previously released signatures conservatively, preserving period history.
UPDATE x402_payments
SET state = 'authorized', release_reason = NULL,
    updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
WHERE state = 'released'
  AND release_reason IN ('rejected_by_receiver', 'resource_error_not_settled',
                         'superseded_by_later_attempt');

-- Old decoders accepted empty receipts as settlement. Retain their budget but
-- stop claiming a settlement without even a structurally valid EVM transaction.
UPDATE x402_payments
SET state = 'authorized', transaction_hash = NULL,
    updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
WHERE state = 'settled'
  AND (transaction_hash IS NULL OR length(transaction_hash) != 66
       OR substr(transaction_hash, 1, 2) != '0x'
       OR substr(transaction_hash, 3) GLOB '*[^0-9a-fA-F]*');
