"""Add timezone column to users.

Revision ID: 2dd8b091742b
Revises: 2d7a23f85081
Create Date: 2015-07-24 20:28:35.906469

"""

# revision identifiers, used by Alembic.
revision = '2dd8b091742b'
down_revision = '2d7a23f85081'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.add_column('users', sa.Column('timezone', sa.Unicode(length=255), nullable=True))
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('users', 'timezone')
    ### end Alembic commands ###
