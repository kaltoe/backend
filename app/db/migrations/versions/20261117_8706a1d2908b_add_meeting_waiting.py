"""회의 대기 상태 WAITING 추가

만든 회의는 [회의 시작] 전까지 WAITING 이고 started_at 이 비어 있다.
기존 회의는 모두 시작된 회의라 upgrade 에서 바꿀 데이터가 없다.

Revision ID: 8706a1d2908b
Revises: 4fc44ba82fe1
Create Date: 2026-11-17 15:12:53.458456

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8706a1d2908b"
down_revision: str | Sequence[str] | None = "4fc44ba82fe1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f("ck_meetings_status"), "meetings", type_="check")
    op.create_check_constraint(
        op.f("ck_meetings_status"), "meetings", "status IN ('WAITING', 'IN_PROGRESS', 'PAUSED', 'ENDED')"
    )
    op.alter_column("meetings", "status", server_default="WAITING")
    op.alter_column("meetings", "started_at", nullable=True)
    op.create_check_constraint(
        op.f("ck_meetings_started_at"), "meetings", "(status = 'WAITING') = (started_at IS NULL)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 대기 회의는 만든 시각(동의 시각)에 시작한 것으로 되돌린다. 진행 중 회의에는 진행 중 안건이 하나 있어야 한다
    op.execute(
        sa.text(
            "UPDATE agendas SET status = 'IN_PROGRESS', recording_start_ms = 0 "
            "WHERE order_index = 0 AND meeting_id IN (SELECT id FROM meetings WHERE status = 'WAITING')"
        )
    )
    op.execute(
        sa.text("UPDATE meetings SET status = 'IN_PROGRESS', started_at = consent_agreed_at WHERE status = 'WAITING'")
    )
    op.drop_constraint(op.f("ck_meetings_started_at"), "meetings", type_="check")
    op.alter_column("meetings", "started_at", nullable=False)
    op.alter_column("meetings", "status", server_default="IN_PROGRESS")
    op.drop_constraint(op.f("ck_meetings_status"), "meetings", type_="check")
    op.create_check_constraint(op.f("ck_meetings_status"), "meetings", "status IN ('IN_PROGRESS', 'PAUSED', 'ENDED')")
