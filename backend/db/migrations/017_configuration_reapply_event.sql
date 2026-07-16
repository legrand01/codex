-- Add the guarded configuration-history reapply event to existing installations.

INSERT INTO event_code_catalog (
    event_code, default_severity, component, description
) VALUES (
    'CONFIG_REAPPLY_REQUESTED', 'info', 'configuration',
    'A prior verified configuration was requested for guarded reapply'
)
ON CONFLICT (event_code) DO UPDATE SET
    default_severity = EXCLUDED.default_severity,
    component = EXCLUDED.component,
    description = EXCLUDED.description;
