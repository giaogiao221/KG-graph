from pathlib import Path
import pytest


def test_review_migration_is_chained_and_enforces_append_only_linkage() -> None:
    source = (Path(__file__).parents[2] / "alembic/versions/0009_reviews.py").read_text(encoding="utf-8")
    assert 'revision: str = "0009_reviews"' in source
    assert 'down_revision: str | None = "0008_model_usage"' in source
    assert "prevent_fact_version_mutation" in source
    assert "enforce_fact_version_linkage" in source
    assert "review_tasks" in source


def test_alembic_registers_review_models() -> None:
    source = (Path(__file__).parents[2] / "alembic/env.py").read_text(encoding="utf-8")
    assert "import app.reviews.models" in source


def test_0009_executes_real_sqlite_constraints_and_downgrade(tmp_path) -> None:
    import importlib.util
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import sqlalchemy as sa

    engine=sa.create_engine(f"sqlite:///{tmp_path/'review.db'}")
    @sa.event.listens_for(engine,"connect")
    def _fk(dbapi_connection,_record):dbapi_connection.execute("PRAGMA foreign_keys=ON")
    metadata=sa.MetaData()
    sa.Table("projects",metadata,sa.Column("id",sa.Uuid(),primary_key=True));sa.Table("users",metadata,sa.Column("id",sa.Uuid(),primary_key=True))
    sa.Table("documents",metadata,sa.Column("id",sa.Uuid(),primary_key=True),sa.Column("project_id",sa.Uuid(),nullable=False))
    sa.Table("document_versions",metadata,sa.Column("id",sa.Uuid(),primary_key=True),sa.Column("document_id",sa.Uuid(),nullable=False))
    sa.Table("raw_facts",metadata,sa.Column("id",sa.Uuid(),primary_key=True),sa.Column("project_id",sa.Uuid(),nullable=False),sa.Column("document_id",sa.Uuid(),nullable=False),sa.Column("document_version_id",sa.Uuid(),nullable=False))
    metadata.create_all(engine)
    path=Path(__file__).parents[2]/"alembic/versions/0009_reviews.py"
    spec=importlib.util.spec_from_file_location("review_migration",path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with engine.begin() as connection:
        context=MigrationContext.configure(connection);operations=Operations(context)
        with operations.context(context):module.upgrade()
        foreign_keys=sa.inspect(connection).get_foreign_keys("fact_versions")
        assert any(fk["referred_table"]=="review_tasks" and fk["constrained_columns"]==["review_task_id"] for fk in foreign_keys)
        indexes=sa.inspect(connection).get_indexes("review_tasks")
        assert any(index["name"]=="uq_review_task_active_root" and index["unique"] for index in indexes)
        now=datetime.now(UTC);project,user,raw,root,document,document_version=uuid4(),uuid4(),uuid4(),uuid4(),uuid4(),uuid4()
        connection.execute(sa.text("INSERT INTO projects(id) VALUES(:id)"),{"id":project.hex})
        connection.execute(sa.text("INSERT INTO users(id) VALUES(:id)"),{"id":user.hex})
        connection.execute(sa.text("INSERT INTO documents(id,project_id) VALUES(:id,:p)"),{"id":document.hex,"p":project.hex});connection.execute(sa.text("INSERT INTO document_versions(id,document_id) VALUES(:id,:d)"),{"id":document_version.hex,"d":document.hex})
        connection.execute(sa.text("INSERT INTO raw_facts(id,project_id,document_id,document_version_id) VALUES(:id,:p,:d,:dv)"),{"id":raw.hex,"p":project.hex,"d":document.hex,"dv":document_version.hex})
        common={"id":root.hex,"c":now,"u":now,"v":1,"p":project.hex,"r":raw.hex,"a":user.hex,"d":document.hex,"dv":document_version.hex}
        connection.execute(sa.text("INSERT INTO review_fact_roots(id,created_at,updated_at,version,project_id,raw_fact_id,document_id,document_version_id,source,created_by_id) VALUES(:id,:c,:u,:v,:p,:r,:d,:dv,'raw',:a)"),common)
        with pytest.raises(sa.exc.IntegrityError):connection.execute(sa.text("UPDATE review_fact_roots SET source='manual' WHERE id=:id"),{"id":root.hex})
        with pytest.raises(sa.exc.IntegrityError):connection.execute(sa.text("INSERT INTO review_fact_roots(id,created_at,updated_at,version,project_id,document_id,document_version_id,source,created_by_id) VALUES(:id,:c,:u,1,:p,:d,:dv,'manual',:a)"),{"id":uuid4().hex,"c":now,"u":now,"p":project.hex,"d":uuid4().hex,"dv":document_version.hex,"a":user.hex})
        task=uuid4()
        connection.execute(sa.text("INSERT INTO review_tasks(id,created_at,updated_at,version,project_id,root_id,lease_version,status) VALUES(:id,:c,:u,1,:p,:r,0,'pending')"),{"id":task.hex,"c":now,"u":now,"p":project.hex,"r":root.hex})
        with pytest.raises(sa.exc.IntegrityError):connection.execute(sa.text("INSERT INTO review_tasks(id,created_at,updated_at,version,project_id,root_id,lease_version,status) VALUES(:id,:c,:u,1,:p,:r,0,'pending')"),{"id":uuid4().hex,"c":now,"u":now,"p":project.hex,"r":root.hex})
        with pytest.raises(sa.exc.IntegrityError):connection.execute(sa.text("UPDATE review_tasks SET status='completed',reviewer_id=:a,lease_version=1 WHERE id=:id"),{"a":user.hex,"id":task.hex})
        connection.execute(sa.text("UPDATE review_tasks SET status='claimed',reviewer_id=:a,lease_expires_at=:e,lease_version=1 WHERE id=:id"),{"a":user.hex,"e":now+timedelta(hours=1),"id":task.hex})
        valid_version=uuid4()
        connection.execute(sa.text("INSERT INTO fact_versions(id,created_at,updated_at,version,root_id,project_id,version_number,actor_id,action,row_json,patch_json,audit_json,is_tombstone,is_published,idempotency_key,request_fingerprint,review_task_id,review_lease_version) VALUES(:id,:c,:u,1,:r,:p,1,:a,'approve','{}','{}','{}',0,0,'valid',:f,:t,1)"),{"id":valid_version.hex,"c":now,"u":now,"r":root.hex,"p":project.hex,"a":user.hex,"f":"1"*64,"t":task.hex})
        with pytest.raises(sa.exc.IntegrityError):connection.execute(sa.text("DELETE FROM review_tasks WHERE id=:id"),{"id":task.hex})
        bad=dict(common);bad["id"]=uuid4().hex
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text("INSERT INTO fact_versions(id,created_at,updated_at,version,root_id,project_id,version_number,actor_id,action,row_json,patch_json,audit_json,is_tombstone,is_published,idempotency_key,request_fingerprint) VALUES(:id,:c,:u,:v,:r,:p,2,:a,'approve','{}','{}','{}',0,0,'k',:f)"),{**bad,"r":root.hex,"f":"0"*64})
        with operations.context(context):module.downgrade()
        assert "fact_versions" not in sa.inspect(connection).get_table_names()


def test_real_postgresql_review_migration_when_test_dsn_is_available(monkeypatch) -> None:
    import os
    import subprocess
    import sys
    dsn=os.getenv("EXTRACTION_TEST_POSTGRES_DSN")
    if not dsn:pytest.skip("EXTRACTION_TEST_POSTGRES_DSN is not configured")
    backend=Path(__file__).parents[2];env=os.environ.copy();env["EXTRACTION_DATABASE_URL"]=dsn
    upgrade=subprocess.run([sys.executable,"-m","alembic","upgrade","head"],cwd=backend,env=env,capture_output=True,text=True,check=False)
    assert upgrade.returncode==0,upgrade.stderr
    downgrade=subprocess.run([sys.executable,"-m","alembic","downgrade","0008_model_usage"],cwd=backend,env=env,capture_output=True,text=True,check=False)
    assert downgrade.returncode==0,downgrade.stderr
