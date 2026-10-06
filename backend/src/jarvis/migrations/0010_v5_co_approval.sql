-- V5 M1a: org-impacting action types need a second person. The proposal owner's approval is `primary`; a different
-- user holding the type's co-approver permission adds a `co` approval on the same version + hash.
ALTER TABLE approvals ADD COLUMN kind TEXT NOT NULL DEFAULT 'primary' CHECK (kind IN ('primary','co'));
CREATE INDEX approvals_version ON approvals(proposal_version_id, kind);
