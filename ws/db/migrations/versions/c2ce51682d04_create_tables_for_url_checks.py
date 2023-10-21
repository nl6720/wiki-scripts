"""create tables for URL checks

Revision ID: c2ce51682d04
Revises: b22ed21a9d64
Create Date: 2023-05-06 09:10:29.438479

"""
from alembic import op
import sqlalchemy as sa

# add our project root into the path so that we can import the "ws" module
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "../../../.."))

import ws.db.sql_types



# revision identifiers, used by Alembic.
revision = 'c2ce51682d04'
down_revision = 'b22ed21a9d64'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('ws_domain',
    sa.Column('name', sa.UnicodeText(), nullable=False),
    sa.Column('last_check', sa.DateTime(), nullable=True),
    sa.Column('resolved', sa.Boolean(), nullable=True),
    sa.Column('server', sa.UnicodeText(), nullable=True),
    sa.Column('ssl_error', sa.UnicodeText(), nullable=True),
    sa.CheckConstraint('resolved or ssl_error is null', name='check_ssl_error_implies_resolved'),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_index('ws_dom_last_check', 'ws_domain', ['last_check'], unique=False)
    op.create_index('ws_dom_resolved_ssl_error', 'ws_domain', ['resolved', 'ssl_error'], unique=False)
    op.create_table('ws_url_check',
    sa.Column('domain_name', sa.UnicodeText(), nullable=False),
    sa.Column('url', sa.UnicodeText(), nullable=False),
    sa.Column('last_check', sa.DateTime(), nullable=True),
    sa.Column('check_duration', sa.Interval(), nullable=True),
    sa.Column('http_status', sa.Integer(), nullable=True),
    sa.Column('text_status', sa.UnicodeText(), nullable=True),
    sa.Column('result', sa.UnicodeText(), nullable=True),
    sa.CheckConstraint("position('://' || domain_name in url) > 0", name='check_wsuc_domain_in_url'),
    sa.ForeignKeyConstraint(['domain_name'], ['ws_domain.name'], ),
    sa.PrimaryKeyConstraint('url')
    )
    op.create_index('wsuc_domain_name', 'ws_url_check', ['domain_name'], unique=False)
    op.create_index('wsuc_last_check', 'ws_url_check', ['last_check'], unique=False)
    op.create_index('wsuc_status', 'ws_url_check', ['http_status', 'text_status'], unique=False)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('wsuc_status', table_name='ws_url_check')
    op.drop_index('wsuc_last_check', table_name='ws_url_check')
    op.drop_index('wsuc_domain_name', table_name='ws_url_check')
    op.drop_table('ws_url_check')
    op.drop_index('ws_dom_resolved_ssl_error', table_name='ws_domain')
    op.drop_index('ws_dom_last_check', table_name='ws_domain')
    op.drop_table('ws_domain')
    # ### end Alembic commands ###