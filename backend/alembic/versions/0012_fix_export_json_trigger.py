"""fix PostgreSQL JSON comparison in export completion trigger

Revision ID: 0012_fix_export_json_trigger
Revises: 0011_archive_batches
"""

from alembic import op


revision = "0012_fix_export_json_trigger"
down_revision = "0011_archive_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS enforce_export_completion ON exports")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_export_completion() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.status = 'completed' THEN
            IF NOT (
              OLD.status = 'pending'
              AND NEW.id = OLD.id
              AND NEW.created_at = OLD.created_at
              AND NEW.project_id = OLD.project_id
              AND NEW.actor_id = OLD.actor_id
              AND NEW.filters_json::text = OLD.filters_json::text
              AND NEW.format = OLD.format
              AND NEW.include_unreviewed = OLD.include_unreviewed
              AND NEW.storage_key = OLD.storage_key
              AND NEW.owner_token = OLD.owner_token
              AND NEW.idempotency_key = OLD.idempotency_key
              AND NEW.request_fingerprint = OLD.request_fingerprint
              AND NEW.completed_at IS NOT NULL
              AND length(NEW.sha256) = 64
              AND NEW.cleanup_token IS NULL
              AND NEW.cleanup_claimed_at IS NULL
              AND (SELECT count(*) FROM export_items item WHERE item.export_id = NEW.id) = NEW.record_count
              AND (NEW.record_count = 0 OR (
                (SELECT min(ordinal) FROM export_items item WHERE item.export_id = NEW.id) = 0
                AND (SELECT max(ordinal) FROM export_items item WHERE item.export_id = NEW.id) = NEW.record_count - 1
              ))
            ) THEN RAISE EXCEPTION 'export completion mismatch'; END IF;
          ELSIF NEW.status = 'cleanup_claimed' THEN
            IF NOT (
              OLD.status IN ('pending', 'cleanup_claimed')
              AND NEW.id = OLD.id
              AND NEW.created_at = OLD.created_at
              AND NEW.project_id = OLD.project_id
              AND NEW.actor_id = OLD.actor_id
              AND NEW.filters_json::text = OLD.filters_json::text
              AND NEW.format = OLD.format
              AND NEW.include_unreviewed = OLD.include_unreviewed
              AND NEW.record_count = OLD.record_count
              AND NEW.size_bytes = OLD.size_bytes
              AND NEW.sha256 IS NOT DISTINCT FROM OLD.sha256
              AND NEW.completed_at IS NOT DISTINCT FROM OLD.completed_at
              AND NEW.storage_key = OLD.storage_key
              AND NEW.owner_token = OLD.owner_token
              AND NEW.idempotency_key = OLD.idempotency_key
              AND NEW.request_fingerprint = OLD.request_fingerprint
              AND length(NEW.cleanup_token) = 32
              AND NEW.cleanup_claimed_at IS NOT NULL
              AND (OLD.status = 'pending' OR NEW.cleanup_token <> OLD.cleanup_token OR NEW.cleanup_claimed_at >= OLD.cleanup_claimed_at)
            ) THEN RAISE EXCEPTION 'export completion mismatch'; END IF;
          ELSE
            RAISE EXCEPTION 'export completion mismatch';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER enforce_export_completion BEFORE UPDATE ON exports "
        "FOR EACH ROW EXECUTE FUNCTION enforce_export_completion()"
    )
    # Exports are synchronous. Any pending row is a remnant of the broken
    # trigger and cannot become a usable download.
    op.execute(
        "UPDATE exports SET status = 'cleanup_claimed', cleanup_token = md5(id::text), "
        "cleanup_claimed_at = CURRENT_TIMESTAMP WHERE status = 'pending'"
    )
    op.execute(
        "DELETE FROM export_items WHERE export_id IN "
        "(SELECT id FROM exports WHERE status = 'cleanup_claimed')"
    )
    op.execute("DELETE FROM exports WHERE status = 'cleanup_claimed'")


def downgrade() -> None:
    # The original trigger cannot run on PostgreSQL JSON columns, so retain
    # the corrected implementation when rolling back application code.
    pass
