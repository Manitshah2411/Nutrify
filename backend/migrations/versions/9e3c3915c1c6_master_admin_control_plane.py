"""master admin control plane

Revision ID: 9e3c3915c1c6
Revises: 4a287724d2e0
Create Date: 2026-04-26 20:05:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9e3c3915c1c6'
down_revision = '4a287724d2e0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('is_locked', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('locked_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('force_password_reset', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('ai_access_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.create_index(batch_op.f('ix_users_is_active'), ['is_active'], unique=False)
        batch_op.create_index(batch_op.f('ix_users_is_locked'), ['is_locked'], unique=False)
        batch_op.create_index(batch_op.f('ix_users_ai_access_enabled'), ['ai_access_enabled'], unique=False)

    op.create_table(
        'platform_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=120), nullable=False),
        sa.Column('value', sa.JSON(), nullable=True),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('updated_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['updated_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_platform_settings_key'), 'platform_settings', ['key'], unique=True)
    op.create_index(op.f('ix_platform_settings_updated_by_user_id'), 'platform_settings', ['updated_by_user_id'], unique=False)

    op.create_table(
        'ai_access_policies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('feature', sa.String(length=64), nullable=False),
        sa.Column('daily_limit', sa.Integer(), nullable=True),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['school_id'], ['users.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ai_access_policies_created_by_user_id'), 'ai_access_policies', ['created_by_user_id'], unique=False)
    op.create_index(op.f('ix_ai_access_policies_feature'), 'ai_access_policies', ['feature'], unique=False)
    op.create_index(op.f('ix_ai_access_policies_school_id'), 'ai_access_policies', ['school_id'], unique=False)
    op.create_index(op.f('ix_ai_access_policies_user_id'), 'ai_access_policies', ['user_id'], unique=False)
    op.create_index('ix_ai_access_policy_scope_feature', 'ai_access_policies', ['school_id', 'user_id', 'feature'], unique=False)


def downgrade():
    op.drop_index('ix_ai_access_policy_scope_feature', table_name='ai_access_policies')
    op.drop_index(op.f('ix_ai_access_policies_user_id'), table_name='ai_access_policies')
    op.drop_index(op.f('ix_ai_access_policies_school_id'), table_name='ai_access_policies')
    op.drop_index(op.f('ix_ai_access_policies_feature'), table_name='ai_access_policies')
    op.drop_index(op.f('ix_ai_access_policies_created_by_user_id'), table_name='ai_access_policies')
    op.drop_table('ai_access_policies')

    op.drop_index(op.f('ix_platform_settings_updated_by_user_id'), table_name='platform_settings')
    op.drop_index(op.f('ix_platform_settings_key'), table_name='platform_settings')
    op.drop_table('platform_settings')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_ai_access_enabled'))
        batch_op.drop_index(batch_op.f('ix_users_is_locked'))
        batch_op.drop_index(batch_op.f('ix_users_is_active'))
        batch_op.drop_column('ai_access_enabled')
        batch_op.drop_column('force_password_reset')
        batch_op.drop_column('locked_at')
        batch_op.drop_column('is_locked')
        batch_op.drop_column('is_active')
