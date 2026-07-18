"""merge upstream schema heads

Revision ID: c4d8e2f19a73
Revises: a1f4e7c2b9d0, 9f0940493a9b

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "c4d8e2f19a73"
down_revision: Union[str, Sequence[str], None] = ("a1f4e7c2b9d0", "9f0940493a9b")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge the elevation-profile and daily-total migration branches."""


def downgrade() -> None:
    """Split the migration graph back into its two parent branches."""
