from pathlib import Path
import importlib.util
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_export_migration_is_chained_and_registers_models() -> None:
    migration = (Path(__file__).parents[2] / "alembic/versions/0010_exports_audit.py").read_text("utf-8")
    env = (Path(__file__).parents[2] / "alembic/env.py").read_text("utf-8")
    assert 'revision: str = "0010_exports_audit"' in migration
    assert 'down_revision: str | None = "0009_reviews"' in migration
    assert "audit_events" in migration and "exports" in migration
    assert "import app.exports.models" in env and "import app.core.audit" in env


def test_0010_real_sqlite_immutability_linkage_and_downgrade(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'exports.db'}")
    @sa.event.listens_for(engine, "connect")
    def _fk(connection, _record): connection.execute("PRAGMA foreign_keys=ON")
    metadata = sa.MetaData()
    sa.Table("projects", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table("users", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table("roles", metadata, sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("name", sa.String()))
    sa.Table("user_roles", metadata, sa.Column("user_id", sa.Uuid()), sa.Column("role_id", sa.Uuid()))
    sa.Table("project_memberships", metadata, sa.Column("project_id", sa.Uuid()), sa.Column("user_id", sa.Uuid()))
    sa.Table("fact_versions", metadata, sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("project_id", sa.Uuid(), nullable=False))
    sa.Table("raw_facts", metadata, sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("project_id", sa.Uuid(), nullable=False))
    metadata.create_all(engine)
    path = Path(__file__).parents[2] / "alembic/versions/0010_exports_audit.py"
    spec = importlib.util.spec_from_file_location("export_migration", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection); operations = Operations(context)
        with operations.context(context): module.upgrade()
        now = datetime.now(UTC); project, actor, export_id, cleanup_id, event_id, raw_id = uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
        connection.execute(sa.text("INSERT INTO projects(id) VALUES(:id)"), {"id": project.hex})
        connection.execute(sa.text("INSERT INTO users(id) VALUES(:id)"), {"id": actor.hex})
        connection.execute(sa.text("INSERT INTO project_memberships(project_id,user_id) VALUES(:p,:a)"), {"p": project.hex, "a": actor.hex})
        connection.execute(sa.text("INSERT INTO raw_facts(id,project_id) VALUES(:id,:p)"), {"id": raw_id.hex, "p": project.hex})
        common = {"c": now, "u": now, "p": project.hex, "a": actor.hex}
        connection.execute(sa.text("INSERT INTO audit_events(id,created_at,updated_at,version,actor_id,project_id,action,target_type,request_id,outcome,metadata_json) VALUES(:id,:c,:u,1,:a,:p,'export.request','export','r','requested','{}')"), {**common, "id": event_id.hex})
        connection.execute(sa.text("INSERT INTO exports(id,created_at,updated_at,version,project_id,actor_id,filters_json,format,include_unreviewed,record_count,size_bytes,storage_key,owner_token,status,idempotency_key,request_fingerprint) VALUES(:id,:c,:u,1,:p,:a,'{}','tsv',1,0,0,'exports/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/payload.tsv','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','pending','key',:h)"), {**common, "id": export_id.hex, "h": "1" * 64})
        connection.execute(sa.text("INSERT INTO exports(id,created_at,updated_at,version,project_id,actor_id,filters_json,format,include_unreviewed,record_count,size_bytes,storage_key,owner_token,status,idempotency_key,request_fingerprint) VALUES(:id,:c,:u,1,:p,:a,'{}','tsv',1,0,0,'exports/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/payload.tsv','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','pending','cleanup-key',:h)"), {**common, "id": cleanup_id.hex, "h": "2" * 64})
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text("DELETE FROM exports WHERE id=:id"), {"id": cleanup_id.hex})
        connection.execute(sa.text("UPDATE exports SET status='cleanup_claimed',cleanup_token=:t,cleanup_claimed_at=:c WHERE id=:id"), {"t":"c"*32,"c":now,"id":cleanup_id.hex})
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text("UPDATE exports SET status='completed',record_count=0,size_bytes=0,sha256=:h,completed_at=:c,cleanup_token=NULL,cleanup_claimed_at=NULL WHERE id=:id"), {"h":"3"*64,"c":now,"id":cleanup_id.hex})
        connection.execute(sa.text("DELETE FROM exports WHERE id=:id"), {"id": cleanup_id.hex})
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text("INSERT INTO audit_events(id,created_at,updated_at,version,actor_id,project_id,action,target_type,target_id,request_id,outcome,metadata_json) VALUES(:id,:c,:u,1,:a,:p,'export.success','export',:e,'pending','success','{}')"), {**common,"id":uuid4().hex,"e":export_id.hex})
        item_id=uuid4()
        connection.execute(sa.text("INSERT INTO export_items(id,created_at,updated_at,version,export_id,ordinal,project_id,source_kind,raw_fact_id) VALUES(:id,:c,:u,1,:e,0,:p,'raw',:r)"), {**common,"id":item_id.hex,"e":export_id.hex,"r":raw_id.hex})
        connection.execute(sa.text("UPDATE exports SET status='completed',record_count=1,size_bytes=1,sha256=:h,completed_at=:c WHERE id=:id"), {"h":"1"*64,"c":now,"id":export_id.hex})
        success_id=uuid4()
        connection.execute(sa.text("INSERT INTO audit_events(id,created_at,updated_at,version,actor_id,project_id,action,target_type,target_id,request_id,outcome,metadata_json) VALUES(:id,:c,:u,1,:a,:p,'export.success','export',:e,'r','success','{}')"), {**common,"id":success_id.hex,"e":export_id.hex})
        for table, identity in (("audit_events", event_id), ("exports", export_id)):
            with pytest.raises(sa.exc.IntegrityError): connection.execute(sa.text(f"DELETE FROM {table} WHERE id=:id"), {"id": identity.hex})
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text("INSERT INTO audit_events(id,created_at,updated_at,version,actor_id,project_id,action,target_type,target_id,request_id,outcome,metadata_json) VALUES(:id,:c,:u,1,:a,:p,'export.success','export',:t,'x','success','{}')"), {**common, "id": uuid4().hex, "t":uuid4().hex})
        with operations.context(context): module.downgrade()
        assert "exports" not in sa.inspect(connection).get_table_names()


def test_real_postgresql_export_migration_when_test_dsn_is_available() -> None:
    import os
    import subprocess
    import sys
    dsn = os.getenv("EXTRACTION_TEST_POSTGRES_DSN")
    if not dsn: pytest.skip("EXTRACTION_TEST_POSTGRES_DSN is not configured")
    backend = Path(__file__).parents[2]; env = os.environ.copy(); env["EXTRACTION_DATABASE_URL"] = dsn
    upgrade = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=backend, env=env, capture_output=True, text=True, check=False)
    assert upgrade.returncode == 0, upgrade.stderr
    downgrade = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0009_reviews"], cwd=backend, env=env, capture_output=True, text=True, check=False)
    assert downgrade.returncode == 0, downgrade.stderr


def test_postgresql_offline_sql_contains_export_guards(monkeypatch) -> None:
    import os, subprocess, sys
    backend=Path(__file__).parents[2];env=os.environ.copy();env["EXTRACTION_DATABASE_URL"]="postgresql+psycopg://test:test@localhost/test"
    result=subprocess.run([sys.executable,"-m","alembic","upgrade","head","--sql"],cwd=backend,env=env,capture_output=True,text=True,check=False)
    assert result.returncode==0,result.stderr
    assert "CREATE TABLE export_items" in result.stdout
    assert "enforce_export_completion" in result.stdout and "enforce_audit_target" in result.stdout
    assert "cleanup_claimed_at" in result.stdout and "cleanup_claimed" in result.stdout
