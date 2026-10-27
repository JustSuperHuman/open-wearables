"""merge upstream 0.10 schema heads

Revision ID: 60dc982419e8
Revises: c4d8e2f19a73, c4e7a1d93b52

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "60dc982419e8"
down_revision: Union[str, Sequence[str], None] = ("c4d8e2f19a73", "c4e7a1d93b52")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge our Strava route/elevation branch with the upstream 0.10 migration chain."""


def downgrade() -> None:
    """Split the migration graph back into its two parent branches."""
