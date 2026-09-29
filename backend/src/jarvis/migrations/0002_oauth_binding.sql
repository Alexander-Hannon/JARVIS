-- Binds an OAuth state to the browser that started the flow (SameSite=Lax cookie hash),
-- because the SameSite=Strict session cookie is not sent on the cross-site redirect back from the provider.
ALTER TABLE oauth_states ADD COLUMN binding_hash TEXT;
