-- Migration 005: Make fleet names unique inside an organization, not globally.

ALTER TABLE hosts DROP CONSTRAINT IF EXISTS hosts_hostname_key;
ALTER TABLE hosts
    ADD CONSTRAINT hosts_organization_hostname_key
    UNIQUE (organization_id, hostname);
