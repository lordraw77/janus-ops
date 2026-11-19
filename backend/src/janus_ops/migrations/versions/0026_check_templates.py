"""Check templates (F8): a generic template for any kind of check, with macros.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-19

- `check_template` generalizes `snmp_template`: a named bundle of checks of any kind (not only
  SNMP), a `requires` node capability, `macros` (name, default, description: `{$NAME}` in the
  items), and `detect` rules (used by the agent's service discovery). Built-in templates have a
  `slug` and a `version`; they are read only and kept in sync from the files shipped with the
  product, the user duplicates one to change it.
- `template_node` links a template to the nodes it was applied to, with the macro values of that
  node and where each value comes from (`manual` or `auto`, from the agent's discovery).
- `service_check.template_id` and `template_item` tie a check to the template and the item that
  created it.
- The rows of `snmp_template` are copied (their items become `snmp` items) and the table is dropped.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UP = [
    """
    CREATE TABLE check_template (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        slug        text CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
        name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
        category    text NOT NULL CHECK (category IN
                    ('proxmox', 'database', 'web', 'server', 'network', 'nas', 'ups', 'generic')),
        description text NOT NULL DEFAULT '' CHECK (char_length(description) <= 500),
        requires    text CHECK (requires IN
                    ('ssh', 'agent', 'proxmox', 'docker', 'snmp', 'fritzbox')),
        macros      jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(macros) = 'array'),
        items       jsonb NOT NULL CHECK (jsonb_typeof(items) = 'array'),
        detect      jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(detect) = 'object'),
        source      text NOT NULL DEFAULT 'manual' CHECK (source IN ('built-in', 'manual')),
        version     integer NOT NULL DEFAULT 1 CHECK (version >= 1),
        created_by  text NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX check_template_name_idx ON check_template (lower(name))",
    "CREATE UNIQUE INDEX check_template_slug_idx ON check_template (slug) WHERE slug IS NOT NULL",
    """
    CREATE TABLE template_node (
        template_id     uuid NOT NULL REFERENCES check_template (id) ON DELETE CASCADE,
        node_id         uuid NOT NULL REFERENCES node (id) ON DELETE CASCADE,
        macros          jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(macros) = 'object'),
        applied_version integer NOT NULL,
        applied_by      text NOT NULL,
        applied_at      timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (template_id, node_id)
    )
    """,
    "CREATE INDEX template_node_node_idx ON template_node (node_id)",
    # `template_item` is the position of the item in the template that made the check: when a macro
    # found by the agent changes, the checks of that item are updated
    "ALTER TABLE service_check ADD COLUMN template_id uuid"
    " REFERENCES check_template (id) ON DELETE SET NULL, ADD COLUMN template_item integer",
    "CREATE INDEX service_check_template_idx ON service_check (template_id)"
    " WHERE template_id IS NOT NULL",
    # the SNMP templates, as items of kind snmp (oid and scale move into params)
    """
    INSERT INTO check_template
        (slug, name, category, description, requires, items, source, created_by, created_at)
    SELECT CASE WHEN source = 'built-in' THEN 'snmp-' || device_type END,
           name,
           CASE device_type WHEN 'nas' THEN 'nas' WHEN 'ups' THEN 'ups'
                            WHEN 'switch' THEN 'network' WHEN 'router' THEN 'network'
                            WHEN 'server' THEN 'server' ELSE 'generic' END,
           description,
           'snmp',
           (SELECT coalesce(jsonb_agg(jsonb_build_object(
                'name', i->'name', 'kind', 'snmp',
                'params', jsonb_build_object('oid', i->'oid', 'scale', i->'scale'),
                'indexed', i->'indexed', 'comparison', i->'comparison',
                'warn', i->'warn', 'crit', i->'crit',
                'interval_s', i->'interval_s', 'check_cycles', i->'check_cycles')), '[]'::jsonb)
              FROM jsonb_array_elements(t.items) AS i),
           CASE WHEN source = 'built-in' THEN 'built-in' ELSE 'manual' END,
           created_by,
           created_at
    FROM snmp_template t
    """,
    "DROP TABLE snmp_template",
]

DOWN = [
    """
    CREATE TABLE snmp_template (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
        device_type text NOT NULL CHECK (device_type IN
                    ('nas', 'ups', 'switch', 'router', 'server', 'generic')),
        description text NOT NULL DEFAULT '' CHECK (char_length(description) <= 500),
        items       jsonb NOT NULL CHECK (jsonb_typeof(items) = 'array'),
        source      text NOT NULL DEFAULT 'manual' CHECK (char_length(source) <= 120),
        created_by  text NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX snmp_template_name_idx ON snmp_template (lower(name))",
    # only the templates made of SNMP items can go back
    """
    INSERT INTO snmp_template
        (name, device_type, description, items, source, created_by, created_at)
    SELECT name,
           CASE category WHEN 'nas' THEN 'nas' WHEN 'ups' THEN 'ups' WHEN 'network' THEN 'switch'
                         WHEN 'server' THEN 'server' ELSE 'generic' END,
           description,
           (SELECT jsonb_agg(jsonb_build_object(
                'name', i->'name', 'oid', i->'params'->'oid', 'indexed', i->'indexed',
                'comparison', i->'comparison', 'warn', i->'warn', 'crit', i->'crit',
                'scale', coalesce(i->'params'->'scale', '1'::jsonb),
                'interval_s', i->'interval_s', 'check_cycles', i->'check_cycles'))
              FROM jsonb_array_elements(t.items) AS i),
           source, created_by, created_at
    FROM check_template t
    WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(t.items) AS i WHERE i->>'kind' <> 'snmp')
    """,
    "DROP INDEX service_check_template_idx",
    "ALTER TABLE service_check DROP COLUMN template_item, DROP COLUMN template_id",
    "DROP TABLE template_node",
    "DROP TABLE check_template",
]


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)
