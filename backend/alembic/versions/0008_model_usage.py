"""add encrypted model configuration and immutable usage accounting

Revision ID: 0008_model_usage
Revises: 0007_raw_facts
"""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "0008_model_usage"
down_revision: str | None = "0007_raw_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common() -> list[sa.Column]:
    return [sa.Column("id", sa.Uuid(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("version", sa.Integer(), nullable=False)]


def upgrade() -> None:
    op.add_column("extraction_batches", sa.Column("created_by_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_extraction_batches_created_by_id_users", "extraction_batches", "users", ["created_by_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_extraction_batches_created_by_id", "extraction_batches", ["created_by_id"])
    op.create_table("model_configs", *_common(),
        sa.Column("name", sa.String(200), nullable=False), sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("endpoint", sa.String(2048), nullable=False), sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=False), sa.Column("cipher_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("key_id", sa.String(64), server_default=sa.text("'primary'"), nullable=False), sa.Column("allowed_hosts", sa.JSON(), nullable=False),
        sa.Column("allow_private_network", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("allow_insecure_http", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("provider_supports_idempotency", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint("length(name)>0", name="ck_model_config_name"), sa.CheckConstraint("length(provider)>0", name="ck_model_config_provider"),
        sa.CheckConstraint("length(model_name)>0", name="ck_model_config_model_name"), sa.CheckConstraint("cipher_version>=1", name="ck_model_config_cipher_version"),
        sa.CheckConstraint("length(key_id)>0", name="ck_model_config_key_id"), sa.CheckConstraint("version>=1", name="ck_model_config_version"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("name", name="uq_model_config_name"))
    op.create_index("ix_model_configs_is_enabled", "model_configs", ["is_enabled"])

    op.create_table("model_price_versions", *_common(),
        sa.Column("model_config_id", sa.Uuid(), nullable=False), sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True)), sa.Column("prompt_per_million", sa.Numeric(20,8), nullable=False),
        sa.Column("completion_per_million", sa.Numeric(20,8), nullable=False), sa.Column("cached_per_million", sa.Numeric(20,8), nullable=False),
        sa.Column("currency", sa.String(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.CheckConstraint("effective_to IS NULL OR effective_to>effective_from", name="ck_model_price_interval"),
        sa.CheckConstraint("prompt_per_million>=0", name="ck_model_price_prompt"), sa.CheckConstraint("completion_per_million>=0", name="ck_model_price_completion"),
        sa.CheckConstraint("cached_per_million>=0", name="ck_model_price_cached"), sa.CheckConstraint("version>=1", name="ck_model_price_version"),
        sa.ForeignKeyConstraint(["model_config_id"],["model_configs.id"],ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_config_id","effective_from",name="uq_model_price_start"))
    op.create_index("ix_model_price_versions_model_config_id", "model_price_versions", ["model_config_id"])
    op.create_index("ix_model_price_versions_effective_from", "model_price_versions", ["effective_from"])

    op.create_table("model_calls", *_common(),
        sa.Column("user_id",sa.Uuid(),nullable=False), sa.Column("project_id",sa.Uuid(),nullable=False), sa.Column("batch_id",sa.Uuid(),nullable=False),
        sa.Column("document_id",sa.Uuid(),nullable=False), sa.Column("document_version_id",sa.Uuid(),nullable=False),
        sa.Column("document_job_id",sa.Uuid(),nullable=False), sa.Column("step_id",sa.Uuid(),nullable=False), sa.Column("model_config_id",sa.Uuid(),nullable=False),
        sa.Column("price_version_id",sa.Uuid()), sa.Column("purpose",sa.String(64),nullable=False), sa.Column("route",sa.String(32),nullable=False),
        sa.Column("provider",sa.String(64),nullable=False), sa.Column("model_name",sa.String(200),nullable=False), sa.Column("endpoint",sa.String(2048),nullable=False),
        sa.Column("status",sa.String(16),nullable=False), sa.Column("prompt_tokens",sa.Integer(),server_default=sa.text("0"),nullable=False),
        sa.Column("completion_tokens",sa.Integer(),server_default=sa.text("0"),nullable=False), sa.Column("cached_tokens",sa.Integer(),server_default=sa.text("0"),nullable=False),
        sa.Column("total_tokens",sa.Integer(),server_default=sa.text("0"),nullable=False), sa.Column("cost",sa.Numeric(24,10),server_default=sa.text("0"),nullable=False),
        sa.Column("currency",sa.String(3),server_default=sa.text("'CNY'"),nullable=False), sa.Column("error_code",sa.String(64)),
        sa.Column("idempotency_key",sa.String(64),nullable=False), sa.Column("attempt",sa.Integer(),nullable=False),
        sa.Column("called_at",sa.DateTime(timezone=True),nullable=False), sa.Column("completed_at",sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint("status IN ('success','failure','retry','cache_hit')",name="ck_model_call_status"),
        sa.CheckConstraint("route IN ('llm_text','llm_table')",name="ck_model_call_route"),
        sa.CheckConstraint("prompt_tokens>=0 AND completion_tokens>=0 AND cached_tokens>=0 AND total_tokens>=0",name="ck_model_call_tokens_nonnegative"),
        sa.CheckConstraint("total_tokens=prompt_tokens+completion_tokens+cached_tokens",name="ck_model_call_tokens_total"),
        sa.CheckConstraint("cost>=0",name="ck_model_call_cost"), sa.CheckConstraint("attempt>=1",name="ck_model_call_attempt"),
        sa.CheckConstraint("completed_at>=called_at",name="ck_model_call_times"),sa.CheckConstraint("version>=1",name="ck_model_call_version"),
        *[sa.ForeignKeyConstraint([c],[t],ondelete="RESTRICT") for c,t in (("user_id","users.id"),("project_id","projects.id"),("batch_id","extraction_batches.id"),("document_id","documents.id"),("document_version_id","document_versions.id"),("document_job_id","document_jobs.id"),("step_id","job_steps.id"),("model_config_id","model_configs.id"),("price_version_id","model_price_versions.id"))],
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("step_id","idempotency_key","attempt","status",name="uq_model_call_attempt_status"))
    for column in ("user_id","project_id","batch_id","document_id","document_version_id","document_job_id","step_id","model_config_id","price_version_id","purpose","route","status","called_at"):
        op.create_index(f"ix_model_calls_{column}","model_calls",[column])

    op.execute("""CREATE FUNCTION enforce_model_price_history() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      PERFORM pg_advisory_xact_lock(hashtextextended(NEW.model_config_id::text,0));
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'model price history is immutable'; END IF;
      IF EXISTS (SELECT 1 FROM model_price_versions p WHERE p.model_config_id=NEW.model_config_id AND p.effective_from < COALESCE(NEW.effective_to,'infinity') AND NEW.effective_from < COALESCE(p.effective_to,'infinity')) THEN RAISE EXCEPTION 'model price interval overlaps'; END IF;
      RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER enforce_model_price_history BEFORE INSERT OR UPDATE OR DELETE ON model_price_versions FOR EACH ROW EXECUTE FUNCTION enforce_model_price_history()")
    op.execute("""CREATE FUNCTION prevent_model_call_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'model calls are immutable'; END $$""")
    op.execute("CREATE TRIGGER prevent_model_call_mutation BEFORE UPDATE OR DELETE ON model_calls FOR EACH ROW EXECUTE FUNCTION prevent_model_call_mutation()")
    op.execute("""CREATE FUNCTION enforce_model_call_linkage() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      PERFORM 1 FROM job_steps s JOIN document_jobs j ON j.id=s.document_job_id JOIN extraction_batches b ON b.id=j.batch_id JOIN document_versions v ON v.id=j.document_version_id JOIN documents d ON d.id=v.document_id WHERE s.id=NEW.step_id AND s.kind=NEW.route AND j.id=NEW.document_job_id AND b.id=NEW.batch_id AND b.project_id=NEW.project_id AND v.id=NEW.document_version_id AND d.id=NEW.document_id AND COALESCE(b.created_by_id,d.created_by_id)=NEW.user_id AND EXISTS (SELECT 1 FROM model_configs mc WHERE mc.id=NEW.model_config_id AND mc.provider=NEW.provider AND mc.model_name=NEW.model_name AND mc.endpoint=NEW.endpoint) AND (NEW.price_version_id IS NULL OR EXISTS (SELECT 1 FROM model_price_versions p WHERE p.id=NEW.price_version_id AND p.model_config_id=NEW.model_config_id AND p.effective_from<=NEW.called_at AND (p.effective_to IS NULL OR p.effective_to>NEW.called_at)));
      IF NOT FOUND THEN RAISE EXCEPTION 'model call linkage mismatch'; END IF; RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER enforce_model_call_linkage BEFORE INSERT ON model_calls FOR EACH ROW EXECUTE FUNCTION enforce_model_call_linkage()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS enforce_model_call_linkage ON model_calls"); op.execute("DROP FUNCTION IF EXISTS enforce_model_call_linkage()")
    op.execute("DROP TRIGGER IF EXISTS prevent_model_call_mutation ON model_calls"); op.execute("DROP FUNCTION IF EXISTS prevent_model_call_mutation()")
    op.execute("DROP TRIGGER IF EXISTS enforce_model_price_history ON model_price_versions"); op.execute("DROP FUNCTION IF EXISTS enforce_model_price_history()")
    for column in reversed(("user_id","project_id","batch_id","document_id","document_version_id","document_job_id","step_id","model_config_id","price_version_id","purpose","route","status","called_at")):
        op.drop_index(f"ix_model_calls_{column}",table_name="model_calls")
    op.drop_table("model_calls")
    op.drop_index("ix_model_price_versions_effective_from",table_name="model_price_versions"); op.drop_index("ix_model_price_versions_model_config_id",table_name="model_price_versions")
    op.drop_table("model_price_versions"); op.drop_index("ix_model_configs_is_enabled",table_name="model_configs"); op.drop_table("model_configs")
    op.drop_index("ix_extraction_batches_created_by_id",table_name="extraction_batches")
    op.drop_constraint("fk_extraction_batches_created_by_id_users","extraction_batches",type_="foreignkey")
    op.drop_column("extraction_batches","created_by_id")
