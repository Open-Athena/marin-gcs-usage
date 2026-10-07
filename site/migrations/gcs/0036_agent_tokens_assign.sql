-- Ownership writes opened to every signed-in viewer (`gcs:assign`, `_lib/auth.ts`
-- `baseAssignScope`). New personal agent tokens are minted with it; this gives
-- the ones already issued (scopes stored as a space-separated list) the same,
-- so nobody has to rotate. Share links are untouched.
UPDATE grants SET scopes = 'gcs gcs:assign'
WHERE scopes = 'gcs' AND id IN (SELECT grant_id FROM agent_tokens);
