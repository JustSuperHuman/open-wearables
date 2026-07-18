"""workout_elevation_profile

Adds the workout_details.elevation_profile column for storing the per-sample
elevation-over-distance profile (decoded from Strava's altitude/distance/time
streams) so the UI can rebuild an elevation line chart.

This revision also merges the two open heads that branched from
d8a0bc9afdd9 (the route_polyline/event_name change and the
external_provider_user_id index), restoring a single linear head so
`alembic upgrade head` works again.

Revision ID: a1f4e7c2b9d0
Revises: 7e3a9c41f2b8, 5aaff4551af6

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "a1f4e7c2b9d0"
down_revision: Union[str, Sequence[str], None] = ("7e3a9c41f2b8", "5aaff4551af6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ordered elevation samples for rebuilding a line chart. Backwards compatible:
    # nullable and additive — existing route_polyline / elevation summary columns
    # are untouched and older rows simply have NULL until a resync populates them.
    op.add_column("workout_details", sa.Column("elevation_profile", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("workout_details", "elevation_profile")
