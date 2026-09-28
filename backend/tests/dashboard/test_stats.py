"""Dashboard statistics endpoint tests."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.auth.models import User
from app.auth.service import seed_roles
from app.dashboard.service import aggregate_dashboard
from app.documents.models import Document, DocumentVersion
from app.facts.models import RawFact
from app.jobs.models import DocumentJob, ExtractionBatch, JobStep
from app.models.models import ModelCall, ModelConfig
from app.profiles.models import ExtractionProfile, ProfileVersion
from app.projects.models import Project
from app.reviews.models import ReviewFactRoot, ReviewTask


class _Graph:
    """A fully-linked entity graph satisfying the raw_facts trigger constraints."""

    def __init__(self, session: Session, project: Project | None = None) -> None:
        self.user = User(
            username=f"dash-{os.getpid()}-{uuid4().hex[:6]}",
            password_hash="x",
        )
        self.project = project or Project(name="Dashboard project", description=None)
        session.add_all((self.user, self.project))
        session.flush()

        self.document = Document(project_id=self.project.id, created_by_id=self.user.id)
        session.add(self.document)
        session.flush()
        self.version = DocumentVersion(
            document_id=self.document.id,
            uploader_id=self.user.id,
            version_number=1,
            original_filename="dash.md",
            storage_key=f"dash-{os.getpid()}-{uuid4().hex[:8]}",
            sha256=hashlib.sha256(b"# Dash").hexdigest(),
            size_bytes=8,
            mime_type="text/markdown",
            is_extractable=True,
        )
        session.add(self.version)
        session.flush()

        self.profile = ExtractionProfile(project_id=self.project.id, name="dash-profile")
        session.add(self.profile)
        session.flush()
        self.profile_version = ProfileVersion(
            profile_id=self.profile.id,
            created_by_id=self.user.id,
            version_number=1,
            snapshot_json={"preset": "custom", "routes": {"text_rule": True}, "rule_engine": "builtin"},
            snapshot_sha256=hashlib.sha256(b"snapshot").hexdigest(),
        )
        session.add(self.profile_version)
        session.flush()

        self.batch = ExtractionBatch(
            project_id=self.project.id,
            profile_version_id=self.profile_version.id,
            created_by_id=self.user.id,
            request_key=f"dash-{os.getpid()}-{uuid4().hex[:8]}",
            status="queued",
        )
        session.add(self.batch)
        session.flush()
        self.job = DocumentJob(
            batch_id=self.batch.id,
            document_version_id=self.version.id,
            status="queued",
        )
        session.add(self.job)
        session.flush()
        self.merge_step = JobStep(
            document_job_id=self.job.id,
            kind="merge",
            idempotency_key=hashlib.sha256(b"dash-merge").hexdigest(),
            position=4,
            stage=1,
        )
        self.validate_step = JobStep(
            document_job_id=self.job.id,
            kind="validate",
            idempotency_key=hashlib.sha256(b"dash-validate").hexdigest(),
            position=5,
            stage=2,
        )
        self.llm_step = JobStep(
            document_job_id=self.job.id,
            kind="llm_text",
            idempotency_key=hashlib.sha256(b"dash-llm").hexdigest(),
            position=6,
            stage=0,
        )
        session.add_all((self.merge_step, self.validate_step, self.llm_step))
        session.flush()

    def fact(
        self,
        session: Session,
        *,
        review_status: str = "candidate",
        route: str = "rule_text",
        extraction_source: str = "rule_text",
        confidence: float | None = None,
    ) -> RawFact:
        fact = RawFact(
            project_id=self.project.id,
            document_id=self.document.id,
            document_version_id=self.version.id,
            document_job_id=self.job.id,
            import_step_id=self.validate_step.id,
            source_step_id=self.merge_step.id,
            graph_fact_key=None,
            dedup_key="g:" + hashlib.sha256(f"dedup-{uuid4().hex}".encode()).hexdigest(),
            subject="主体",
            property="属性",
            value="值",
            unit="",
            condition="",
            source_type="text",
            confidence=confidence,
            review_status=review_status,
            evidence_text="证据",
            evidence_hash=hashlib.sha256(f"evidence-{uuid4().hex}".encode()).hexdigest(),
            extraction_source=extraction_source,
            route=route,
            row_json={},
        )
        session.add(fact)
        session.flush()
        return fact


def _make_model_call(
    session: Session,
    graph: _Graph,
    *,
    prompt_tokens: int = 1000,
    completion_tokens: int = 500,
    cost: Decimal = Decimal("0.01"),
    currency: str = "CNY",
) -> ModelCall:
    config = ModelConfig(
        name=f"test-model-{uuid4().hex[:8]}",
        provider="openai-compatible",
        endpoint="https://api.example.com/v1",
        model_name="qwen-max",
        encrypted_api_key=b"test-key",
        allowed_hosts=["api.example.com"],
    )
    session.add(config)
    session.flush()
    call = ModelCall(
        model_config_id=config.id,
        called_at=datetime(2026, 9, 19, tzinfo=UTC),
        user_id=graph.batch.created_by_id,
        project_id=graph.project.id,
        batch_id=graph.batch.id,
        document_id=graph.document.id,
        document_version_id=graph.version.id,
        document_job_id=graph.job.id,
        step_id=graph.llm_step.id,
        provider="openai-compatible",
        model_name="qwen-max",
        endpoint="https://api.example.com/v1",
        purpose="extraction",
        route="llm_text",
        status="success",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=0,
        total_tokens=prompt_tokens + completion_tokens,
        cost=cost,
        currency=currency,
        idempotency_key=uuid4().hex,
        attempt=1,
        completed_at=datetime(2026, 9, 19, 0, 0, 1, tzinfo=UTC),
    )
    session.add(call)
    session.flush()
    return call


class TestAggregateDashboard:
    def test_empty_scope_returns_zeros(
        self, app_session_factory: sessionmaker[Session], users: dict[str, User]
    ):
        with app_session_factory.begin() as session:
            result = aggregate_dashboard(session, project_ids=[uuid4()])
        assert result["jobs_total"] == 0
        assert result["facts_total"] == 0
        assert result["review_progress_pct"] == 0.0

    def test_facts_grouped_by_review_status(
        self, app_session_factory: sessionmaker[Session], users: dict[str, User], project: Project
    ):
        with app_session_factory.begin() as session:
            graph = _Graph(session, project)
            graph.fact(session, review_status="candidate", route="rule_text")
            graph.fact(session, review_status="approved", route="llm_text")
            graph.fact(session, review_status="approved", route="rule_table")
            result = aggregate_dashboard(session, project_ids=[project.id])
        assert result["facts_total"] == 3
        assert result["facts_by_status"]["candidate"] == 1
        assert result["facts_by_status"]["approved"] == 2
        assert result["facts_by_route"]["rule_text"] == 1
        assert result["facts_by_route"]["llm_text"] == 1
        assert result["facts_by_route"]["rule_table"] == 1

    def test_jobs_grouped_by_status(
        self, app_session_factory: sessionmaker[Session], users: dict[str, User], project: Project
    ):
        with app_session_factory.begin() as session:
            graph = _Graph(session, project)
            graph.batch.status = "completed"
            session.flush()
            result = aggregate_dashboard(session, project_ids=[project.id])
        assert result["jobs_total"] == 1
        assert result["jobs_by_status"]["completed"] == 1
        assert result["document_jobs_total"] == 1

    def test_review_tasks_counted(
        self, app_session_factory: sessionmaker[Session], users: dict[str, User], project: Project
    ):
        with app_session_factory.begin() as session:
            graph = _Graph(session, project)
            fact = graph.fact(session)
            root = ReviewFactRoot(
                project_id=project.id,
                raw_fact_id=fact.id,
                document_id=graph.document.id,
                document_version_id=graph.version.id,
                source="raw",
                created_by_id=users["reviewer"].id,
            )
            session.add(root)
            session.flush()
            session.add(ReviewTask(project_id=project.id, root_id=root.id, status="pending"))
            session.flush()
            result = aggregate_dashboard(session, project_ids=[project.id])
        assert result["review_tasks_pending"] == 1

    def test_project_ids_filter_isolates_scope(
        self,
        app_session_factory: sessionmaker[Session],
        users: dict[str, User],
        project: Project,
        foreign_project: Project,
    ):
        with app_session_factory.begin() as session:
            graph = _Graph(session, project)
            graph.fact(session, review_status="approved")
            other = _Graph(session, foreign_project)
            other.fact(session, review_status="rejected")
            result = aggregate_dashboard(session, project_ids=[project.id])
        assert result["facts_total"] == 1
        assert result["facts_by_status"].get("approved") == 1
        assert result["facts_by_status"].get("rejected") is None

    def test_empty_project_ids_returns_zeros(
        self, app_session_factory: sessionmaker[Session], users: dict[str, User]
    ):
        with app_session_factory.begin() as session:
            result = aggregate_dashboard(session, project_ids=[])
        assert result["jobs_total"] == 0
        assert result["facts_total"] == 0

    def test_tokens_aggregated_by_currency(
        self, app_session_factory: sessionmaker[Session], users: dict[str, User], project: Project
    ):
        with app_session_factory.begin() as session:
            graph = _Graph(session, project)
            _make_model_call(session, graph, prompt_tokens=1000, cost=Decimal("0.05"), currency="CNY")
            _make_model_call(session, graph, prompt_tokens=2000, cost=Decimal("0.02"), currency="USD")
            result = aggregate_dashboard(session, project_ids=[project.id])
        assert len(result["tokens"]) == 2
        cny = next(t for t in result["tokens"] if t["currency"] == "CNY")
        usd = next(t for t in result["tokens"] if t["currency"] == "USD")
        assert cny["total_tokens"] == 1500
        assert usd["total_tokens"] == 2500


class TestDashboardRoute:
    def test_viewer_can_read_stats(self, client_factory, project: Project):
        response = client_factory("viewer").get("/api/dashboard/stats")
        assert response.status_code == 200
        body = response.json()
        assert "facts_total" in body
        assert "jobs_by_status" in body
        assert "review_progress_pct" in body

    def test_viewer_cannot_see_foreign_project(self, client_factory, foreign_project: Project):
        response = client_factory("viewer").get(
            f"/api/dashboard/stats?project_id={foreign_project.id}"
        )
        assert response.status_code == 403

    def test_admin_can_filter_any_project(self, client_factory, foreign_project: Project):
        response = client_factory("admin").get(
            f"/api/dashboard/stats?project_id={foreign_project.id}"
        )
        assert response.status_code == 200

    def test_stats_reflect_seeded_data(
        self, client_factory, app_session_factory: sessionmaker[Session], users: dict[str, User], project: Project
    ):
        with app_session_factory.begin() as session:
            graph = _Graph(session, project)
            graph.fact(session, review_status="approved")
        response = client_factory("operator").get("/api/dashboard/stats")
        assert response.status_code == 200
        body = response.json()
        assert body["facts_total"] == 1
        assert body["facts_by_status"]["approved"] == 1
