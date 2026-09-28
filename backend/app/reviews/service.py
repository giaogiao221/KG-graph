from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload

from app.auth.models import Role, User
from app.documents.models import Document, DocumentVersion
from app.facts.importer import FactImportError, validate_schema59_row
from app.facts.models import RawFact
from app.facts.schema59 import FIELD, SCHEMA59_COLUMNS, evidence_hash
from app.jobs.models import DocumentJob
from app.projects.models import ProjectMembership
from app.reviews.models import ACTIONS, FactVersion, ReviewFactRoot, ReviewTask


class ReviewError(ValueError): pass
class InvalidReview(ReviewError): pass
class VersionConflict(ReviewError): pass
class LeaseConflict(ReviewError): pass


_PATCH_FIELDS = frozenset({FIELD["subject"], FIELD["property"], FIELD["value"],
    FIELD["unit"], FIELD["condition"], FIELD["evidence_text"], "客体", "客体类型",
    "主体类型", "属性类型", "时间", "地点", "限定词", "极性", "语言"})
_ACCEPTED = frozenset({"approve", "modify_approve", "publish"})
_SEMANTIC_FIELDS = {
    "subject": FIELD["subject"], "property": FIELD["property"],
    "value": FIELD["value"], "unit": FIELD["unit"],
    "condition": FIELD["condition"], "evidence": FIELD["evidence_text"],
    "evidence_text": FIELD["evidence_text"],
}


def _valid_text(value: str) -> bool:
    if not isinstance(value, str) or "\x00" in value: return False
    try: value.encode("utf-8", "strict")
    except UnicodeEncodeError: return False
    return True


def _actor(session: Session, actor_id: UUID, project_id: UUID, *, admin_only: bool = False) -> User:
    user = session.scalar(select(User).options(selectinload(User.roles).selectinload(Role.permissions)).where(User.id == actor_id))
    if user is None or user.is_disabled: raise InvalidReview("评审人员无权限")
    admin = any(role.name == "admin" for role in user.roles)
    permitted = any(p.code == "records:review" for r in user.roles for p in r.permissions)
    member = session.scalar(select(ProjectMembership.id).where(ProjectMembership.project_id == project_id, ProjectMembership.user_id == actor_id))
    if (admin_only and not admin) or not permitted or (not admin and member is None):
        raise InvalidReview("评审人员无权限")
    return user


def _patch(value: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict) or any(not _valid_text(v) for v in value.values()):
        raise InvalidReview("评审补丁无效")
    normalized: dict[str, str] = {}
    for key, field_value in value.items():
        safe_key = _SEMANTIC_FIELDS.get(key, key)
        if safe_key not in _PATCH_FIELDS or safe_key in normalized:
            raise InvalidReview("invalid review patch")
        normalized[safe_key] = field_value
    return normalized


def _row(base: dict[str, str], action: str, patch: dict[str, str]) -> dict[str, str]:
    row = dict(base); row.update(patch)
    row[FIELD["review_status"]] = {"approve":"approved","modify_approve":"approved","publish":"approved","reject":"rejected","dispute":"candidate_review","delete":"rejected","manual_create":"candidate"}[action]
    row[FIELD["evidence_hash"]] = evidence_hash(row[FIELD["evidence_text"]])
    try: validate_schema59_row(row)
    except FactImportError: raise InvalidReview("评审内容无效") from None
    return row


def _fingerprint(**values: object) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_raw_root(session: Session, fact_id: UUID, actor_id: UUID) -> ReviewFactRoot:
    raw = session.get(RawFact, fact_id)
    if raw is None: raise InvalidReview("事实不存在或不可访问")
    _actor(session,actor_id,raw.project_id)
    root = session.scalar(select(ReviewFactRoot).where(ReviewFactRoot.raw_fact_id == fact_id))
    if root is not None: return root
    root = ReviewFactRoot(project_id=raw.project_id, raw_fact_id=raw.id,
        document_id=raw.document_id, document_version_id=raw.document_version_id,
        source="raw", created_by_id=actor_id)
    try:
        with session.begin_nested(): session.add(root); session.flush()
    except IntegrityError:
        root = session.scalar(select(ReviewFactRoot).where(ReviewFactRoot.raw_fact_id == fact_id))
        if root is None: raise VersionConflict("事实根创建冲突") from None
    return root


def create_review_task(session: Session, root_id: UUID, actor_id: UUID) -> ReviewTask:
    root=session.get(ReviewFactRoot,root_id)
    if root is None:raise InvalidReview("事实根不存在或不可访问")
    _actor(session, actor_id, root.project_id)
    existing = session.scalar(select(ReviewTask).where(ReviewTask.root_id == root.id,ReviewTask.status!="completed"))
    if existing:return existing
    task = ReviewTask(project_id=root.project_id, root_id=root.id)
    try:
        with session.begin_nested(): session.add(task); session.flush()
    except IntegrityError:
        task = session.scalar(select(ReviewTask).where(ReviewTask.root_id == root.id,ReviewTask.status!="completed"))
        if task is None: raise LeaseConflict("评审任务创建冲突") from None
    return task


def create_raw_review_task(session:Session,fact_id:UUID,actor_id:UUID)->ReviewTask:
    raw=session.get(RawFact,fact_id)
    if raw is None:raise InvalidReview("事实不存在或不可访问")
    _actor(session,actor_id,raw.project_id)
    return create_review_task(session,ensure_raw_root(session,fact_id,actor_id).id,actor_id)


def claim_review_task(session: Session, task_id: UUID, reviewer_id: UUID, *, now: datetime | None=None, lease_for: timedelta=timedelta(minutes=15)) -> ReviewTask:
    task = session.get(ReviewTask, task_id)
    if task is None: raise LeaseConflict("评审任务不存在")
    _actor(session, reviewer_id, task.project_id)
    if lease_for<=timedelta(0) or lease_for>timedelta(hours=24):raise LeaseConflict("租约时长无效")
    now = now or datetime.now(UTC)
    try:
        result = session.execute(update(ReviewTask).where(ReviewTask.id==task_id,
            or_(ReviewTask.status=="pending", and_(ReviewTask.status=="claimed", ReviewTask.lease_expires_at<=now)))
            .values(reviewer_id=reviewer_id, lease_expires_at=now+lease_for, lease_version=ReviewTask.lease_version+1, status="claimed")
            .execution_options(synchronize_session=False))
    except (IntegrityError, OperationalError): raise LeaseConflict("评审任务领取冲突") from None
    if result.rowcount != 1: raise LeaseConflict("评审任务已被领取")
    session.flush(); session.expire_all(); return session.get(ReviewTask, task_id)


def assign_review_task(session: Session, task_id: UUID, admin_id: UUID, reviewer_id: UUID, *, now: datetime | None=None, lease_for: timedelta=timedelta(minutes=15)) -> ReviewTask:
    task = session.get(ReviewTask, task_id)
    if task is None: raise LeaseConflict("评审任务不存在")
    _actor(session, admin_id, task.project_id, admin_only=True); _actor(session, reviewer_id, task.project_id)
    return claim_review_task(session, task_id, reviewer_id, now=now, lease_for=lease_for)


def renew_review_task(session: Session, task_id: UUID, reviewer_id: UUID, expected_version: int, *, now: datetime | None=None, lease_for: timedelta=timedelta(minutes=15)) -> ReviewTask:
    task=session.get(ReviewTask,task_id)
    if task is None: raise LeaseConflict("评审任务不存在")
    if lease_for<=timedelta(0) or lease_for>timedelta(hours=24):raise LeaseConflict("租约时长无效")
    _actor(session,reviewer_id,task.project_id); now=now or datetime.now(UTC)
    expiry=task.lease_expires_at
    if expiry is None:raise LeaseConflict("评审租约已变化")
    if expiry.tzinfo is None:expiry=expiry.replace(tzinfo=UTC)
    new_expiry=max(now,expiry)+lease_for
    result=session.execute(update(ReviewTask).where(ReviewTask.id==task_id,ReviewTask.reviewer_id==reviewer_id,ReviewTask.lease_version==expected_version,ReviewTask.lease_expires_at>now,ReviewTask.status=="claimed").values(lease_expires_at=new_expiry,lease_version=ReviewTask.lease_version+1).execution_options(synchronize_session=False))
    if result.rowcount!=1: raise LeaseConflict("评审租约已变化")
    session.flush();session.expire_all();return session.get(ReviewTask,task_id)


def release_review_task(session: Session, task_id: UUID, reviewer_id: UUID, expected_version: int, *, now: datetime | None=None) -> ReviewTask:
    task=session.get(ReviewTask,task_id)
    if task is None: raise LeaseConflict("评审任务不存在")
    _actor(session,reviewer_id,task.project_id); now=now or datetime.now(UTC)
    result=session.execute(update(ReviewTask).where(ReviewTask.id==task_id,ReviewTask.reviewer_id==reviewer_id,ReviewTask.lease_version==expected_version,ReviewTask.lease_expires_at>now,ReviewTask.status=="claimed").values(reviewer_id=None,lease_expires_at=None,lease_version=ReviewTask.lease_version+1,status="pending").execution_options(synchronize_session=False))
    if result.rowcount!=1: raise LeaseConflict("评审租约已变化")
    session.flush();session.expire_all();return session.get(ReviewTask,task_id)


def review_fact(session: Session, root_id: UUID, action: str, patch: dict[str,str], expected_version: int, reviewer_id: UUID, *, review_task_id: UUID, expected_lease_version: int, idempotency_key: str|None=None, audit: dict[str,object]|None=None, now: datetime|None=None) -> FactVersion:
    if action not in ACTIONS or action=="manual_create": raise InvalidReview("评审请求无效")
    safe=_patch(patch)
    if action!="modify_approve" and safe: raise InvalidReview("该操作不接受修改内容")
    task=session.scalar(select(ReviewTask).options(selectinload(ReviewTask.root)).where(ReviewTask.id==review_task_id).with_for_update())
    if task is None or task.root_id!=root_id: raise LeaseConflict("评审任务不匹配")
    _actor(session,reviewer_id,task.project_id); key=idempotency_key or str(uuid4())
    fp=_fingerprint(actor=reviewer_id,action=action,patch=safe,expected_version=expected_version,task=task.id,lease=expected_lease_version,root=task.root_id)
    existing=session.scalar(select(FactVersion).where(FactVersion.project_id==task.project_id,FactVersion.actor_id==reviewer_id,FactVersion.idempotency_key==key))
    if existing is not None:
        if existing.request_fingerprint!=fp: raise VersionConflict("幂等键请求不一致")
        return existing
    now=now or datetime.now(UTC)
    if now.tzinfo is None:now=now.replace(tzinfo=UTC)
    if task.status!="claimed" or task.reviewer_id!=reviewer_id or task.lease_version!=expected_lease_version or task.lease_expires_at is None or (task.lease_expires_at.replace(tzinfo=UTC) if task.lease_expires_at.tzinfo is None else task.lease_expires_at)<=now:
        raise LeaseConflict("评审租约无效")
    previous=session.scalar(select(FactVersion).where(FactVersion.root_id==task.root_id).order_by(FactVersion.version_number.desc()).limit(1))
    latest=previous.version_number if previous else 0
    if latest!=expected_version: raise VersionConflict("事实版本已变化")
    if previous is not None:base=previous.row_json
    elif task.root.raw_fact_id is not None:base=session.get(RawFact,task.root.raw_fact_id).row_json
    else:raise InvalidReview("手工事实缺少初始版本")
    value=FactVersion(root_id=task.root_id,project_id=task.project_id,version_number=latest+1,actor_id=reviewer_id,action=action,row_json=_row(base,action,safe),patch_json=safe,audit_json=dict(audit or {}),is_tombstone=action=="delete",is_published=action=="publish",idempotency_key=key,request_fingerprint=fp,review_task_id=task.id,review_lease_version=expected_lease_version)
    try:
        with session.begin_nested():
            session.add(value);session.flush()
            result=session.execute(update(ReviewTask).where(ReviewTask.id==task.id,ReviewTask.status=="claimed",ReviewTask.reviewer_id==reviewer_id,ReviewTask.lease_version==expected_lease_version,ReviewTask.lease_expires_at>now).values(status="completed",lease_expires_at=None,lease_version=ReviewTask.lease_version+1).execution_options(synchronize_session=False))
            if result.rowcount!=1: raise LeaseConflict("评审租约已变化")
    except (IntegrityError,OperationalError): raise VersionConflict("事实版本写入冲突") from None
    return value


def review_raw_fact(session:Session,fact_id:UUID,*args,reviewer_id:UUID,**kwargs)->FactVersion:
    root=ensure_raw_root(session,fact_id,reviewer_id)
    return review_fact(session,root.id,*args,reviewer_id=reviewer_id,**kwargs)


def create_manual_fact(session:Session,project_id:UUID,actor_id:UUID,fields:dict[str,str],*,document_id:UUID|None=None,document_version_id:UUID|None=None,idempotency_key:str) -> FactVersion:
    _actor(session,actor_id,project_id); safe=_patch(fields)
    required={FIELD["subject"],FIELD["property"],FIELD["value"],FIELD["evidence_text"]}
    if not required.issubset(safe): raise InvalidReview("手工事实字段不完整")
    derived_document_id=document_id
    if document_version_id is not None:
        ver=session.scalar(select(DocumentVersion).join(Document).where(DocumentVersion.id==document_version_id,Document.project_id==project_id))
        if ver is None: raise InvalidReview("文档上下文无效")
        if document_id is not None and document_id!=ver.document_id:raise InvalidReview("文档上下文无效")
        derived_document_id=ver.document_id
    if derived_document_id is not None:
        doc=session.scalar(select(Document).where(Document.id==derived_document_id,Document.project_id==project_id))
        if doc is None: raise InvalidReview("文档上下文无效")
    fp=_fingerprint(actor=actor_id,project=project_id,fields=safe,document=document_id,document_version=document_version_id)
    old=session.scalar(select(FactVersion).where(FactVersion.project_id==project_id,FactVersion.actor_id==actor_id,FactVersion.idempotency_key==idempotency_key))
    if old:
        if old.request_fingerprint!=fp: raise VersionConflict("幂等键请求不一致")
        return old
    root=ReviewFactRoot(project_id=project_id,raw_fact_id=None,document_id=derived_document_id,document_version_id=document_version_id,source="manual",created_by_id=actor_id)
    try:
        with session.begin_nested():
            session.add(root);session.flush()
            row={c:"" for c in SCHEMA59_COLUMNS};row.update(safe);row["fact_id"]=str(root.id);row[FIELD["document_id"]]=str(document_version_id or "");row[FIELD["extraction_source"]]="manual";row[FIELD["route"]]="manual";row[FIELD["schema_version"]]="schema59-v1";row[FIELD["evidence_hash"]]=evidence_hash(row[FIELD["evidence_text"]]);row[FIELD["review_status"]]="candidate"
            value=FactVersion(root=root,project_id=project_id,version_number=1,actor_id=actor_id,action="manual_create",row_json=_row(row,"manual_create",{}),patch_json=safe,audit_json={},is_tombstone=False,is_published=False,idempotency_key=idempotency_key,request_fingerprint=fp,review_task_id=None,review_lease_version=None)
            session.add(value);session.flush()
    except (IntegrityError,OperationalError):
        old=session.scalar(select(FactVersion).where(FactVersion.project_id==project_id,FactVersion.actor_id==actor_id,FactVersion.idempotency_key==idempotency_key))
        if old is not None and old.request_fingerprint==fp:return old
        raise VersionConflict("手工事实写入冲突") from None
    return value


def current_fact(session:Session,fact_id:UUID)->FactVersion|None:
    root=session.scalar(select(ReviewFactRoot).where(ReviewFactRoot.raw_fact_id==fact_id));
    if root is None:return None
    value=session.scalar(select(FactVersion).where(FactVersion.root_id==root.id).order_by(FactVersion.version_number.desc()).limit(1));return None if value is None or value.is_tombstone else value


def _latest(project_id:UUID): return select(FactVersion.root_id,func.max(FactVersion.version_number).label("n")).where(FactVersion.project_id==project_id).group_by(FactVersion.root_id).subquery()
def query_current_facts(session:Session,project_id:UUID,*,accepted_only:bool=False,action:str|None=None,offset:int=0,limit:int=50):
    latest=_latest(project_id);conds=[FactVersion.project_id==project_id,FactVersion.is_tombstone.is_(False)]
    if accepted_only:conds.append(FactVersion.action.in_(_ACCEPTED))
    if action:conds.append(FactVersion.action==action)
    base=select(FactVersion).join(latest,and_(FactVersion.root_id==latest.c.root_id,FactVersion.version_number==latest.c.n)).where(*conds);total=session.scalar(select(func.count()).select_from(base.subquery())) or 0;return list(session.scalars(base.order_by(FactVersion.created_at,FactVersion.id).offset(offset).limit(limit))),int(total)
def publication_facts(session:Session,project_id:UUID,*,offset:int=0,limit:int=50):return query_current_facts(session,project_id,accepted_only=True,offset=offset,limit=limit)[0]
def version_history(session:Session,project_id:UUID,root_id:UUID,*,offset:int=0,limit:int=50):
    root=session.scalar(select(ReviewFactRoot).where(ReviewFactRoot.project_id==project_id,ReviewFactRoot.id==root_id));
    if root is None:return [],0
    total=session.scalar(select(func.count()).select_from(FactVersion).where(FactVersion.root_id==root.id)) or 0;return list(session.scalars(select(FactVersion).where(FactVersion.root_id==root.id).order_by(FactVersion.version_number).offset(offset).limit(limit))),int(total)
def review_batch_summaries(session: Session, project_id: UUID):
    from app.jobs.models import ExtractionBatch
    from app.profiles.models import ProfileVersion
    rows = session.execute(
        select(
            ExtractionBatch.id,
            ExtractionBatch.created_at,
            ExtractionBatch.profile_version_id,
            func.count(ReviewTask.id),
            func.sum(case((ReviewTask.status == "pending", 1), else_=0)),
            func.sum(case((ReviewTask.status == "claimed", 1), else_=0)),
            func.sum(case((ReviewTask.status == "completed", 1), else_=0)),
        )
        .join(DocumentJob, DocumentJob.batch_id == ExtractionBatch.id)
        .join(RawFact, RawFact.document_job_id == DocumentJob.id)
        .join(ReviewFactRoot, ReviewFactRoot.raw_fact_id == RawFact.id)
        .join(ReviewTask, ReviewTask.root_id == ReviewFactRoot.id)
        .where(ExtractionBatch.project_id == project_id)
        .group_by(
            ExtractionBatch.id,
            ExtractionBatch.created_at,
            ExtractionBatch.profile_version_id,
        )
        .order_by(ExtractionBatch.created_at.desc(), ExtractionBatch.id)
    ).all()
    profile_ids = {row[2] for row in rows}
    presets = {
        profile.id: str((profile.snapshot_json or {}).get("preset", ""))
        for profile in session.scalars(
            select(ProfileVersion).where(ProfileVersion.id.in_(profile_ids))
        )
    } if profile_ids else {}
    return [
        {
            "batch_id": row[0], "created_at": row[1],
            "preset": presets.get(row[2], ""),
            "total": int(row[3] or 0), "pending": int(row[4] or 0),
            "claimed": int(row[5] or 0), "completed": int(row[6] or 0),
        }
        for row in rows
    ]


def review_queue(session:Session,project_id:UUID,*,status:str|None=None,reviewer_id:UUID|None=None,batch_id:UUID|None=None,offset:int=0,limit:int=50,now:datetime|None=None):
    now=now or datetime.now(UTC);conds=[ReviewTask.project_id==project_id]
    if status=="available":conds.append(or_(ReviewTask.status=="pending",and_(ReviewTask.status=="claimed",ReviewTask.lease_expires_at<=now)))
    elif status:conds.append(ReviewTask.status==status)
    if reviewer_id is not None:conds.append(ReviewTask.reviewer_id==reviewer_id)
    statement=select(ReviewTask).options(selectinload(ReviewTask.root).selectinload(ReviewFactRoot.raw_fact).selectinload(RawFact.document_job))
    if batch_id is not None:
        statement=statement.join(ReviewFactRoot,ReviewFactRoot.id==ReviewTask.root_id).join(RawFact,RawFact.id==ReviewFactRoot.raw_fact_id).join(DocumentJob,DocumentJob.id==RawFact.document_job_id)
        conds.append(DocumentJob.batch_id==batch_id)
    total=session.scalar(select(func.count()).select_from(statement.where(*conds).subquery())) or 0;return list(session.scalars(statement.where(*conds).order_by(ReviewTask.created_at,ReviewTask.id).offset(offset).limit(limit))),int(total)
