"""Environment badges: any colour, not only five presets.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-19

`environment.color` was one of ok, warn, crit, primary, muted. It may also be `#rrggbb`, so an
environment gets the colour its team uses. The five names stay valid (the console draws them in
fixed colours). Downgrading clears the custom colours (the badge turns neutral).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMES = "'ok', 'warn', 'crit', 'primary', 'muted'"


def upgrade() -> None:
    op.execute("ALTER TABLE environment DROP CONSTRAINT environment_color_check")
    op.execute(
        "ALTER TABLE environment ADD CONSTRAINT environment_color_check CHECK (color IS NULL"
        f" OR color IN ({NAMES}) OR color ~ '^#[0-9a-fA-F]{{6}}$')"
    )


def downgrade() -> None:
    op.execute("UPDATE environment SET color = NULL WHERE color LIKE '#%'")
    op.execute("ALTER TABLE environment DROP CONSTRAINT environment_color_check")
    op.execute(
        "ALTER TABLE environment ADD CONSTRAINT environment_color_check"
        f" CHECK (color IS NULL OR color IN ({NAMES}))"
    )
