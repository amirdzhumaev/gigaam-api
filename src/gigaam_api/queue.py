"""Durable leased queue, shared protocol but no in-process background jobs."""

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    String,
    Table,
    UniqueConstraint,
    and_,
    or_,
    select,
    update,
)

from .infra import now, uid


def job_table(metadata):
    return Table(
        "jobs",
        metadata,
        Column("id", String(32), primary_key=True),
        Column("owner", String(200), nullable=False, index=True),
        Column("resource_id", String(32), nullable=False, index=True),
        Column("kind", String(32), nullable=False),
        Column("state", String(20), nullable=False, index=True),
        Column("payload", JSON, nullable=False),
        Column("result", JSON),
        Column("idempotency_key", String(200)),
        Column("fingerprint", String(64)),
        Column("attempts", Integer, nullable=False, default=0),
        Column("available_at", Float, nullable=False),
        Column("lease_token", String(32)),
        Column("lease_until", Float),
        Column("error_code", String(80)),
        Column("created_at", Float, nullable=False),
        Column("updated_at", Float, nullable=False),
        UniqueConstraint("owner", "idempotency_key"),
    )


def new_job(owner, resource_id, kind, payload, idempotency_key=None, fingerprint=None):
    stamp = now()
    return dict(
        id=uid(),
        owner=owner,
        resource_id=resource_id,
        kind=kind,
        state="queued",
        payload=payload,
        result=None,
        attempts=0,
        available_at=stamp,
        lease_token=None,
        lease_until=None,
        error_code=None,
        created_at=stamp,
        updated_at=stamp,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
    )


def claim(engine, jobs, lease_seconds=120, max_attempts=3):
    stamp = now()
    expired = and_(jobs.c.state == "running", jobs.c.lease_until < stamp)
    eligible = or_(and_(jobs.c.state == "queued", jobs.c.available_at <= stamp), expired)
    with engine.begin() as db:
        db.execute(
            update(jobs)
            .where(eligible, jobs.c.attempts >= max_attempts)
            .values(
                state="failed",
                error_code="attempts_exhausted",
                updated_at=stamp,
                lease_token=None,
                lease_until=None,
            )
        )
        candidates = db.execute(
            select(jobs.c.id, jobs.c.attempts)
            .where(eligible, jobs.c.attempts < max_attempts)
            .order_by(jobs.c.created_at)
            .limit(10)
        ).all()
        for ident, attempt in candidates:
            # CAS prevents two workers from claiming the same job on either SQLite or PostgreSQL.
            row = (
                db.execute(
                    update(jobs)
                    .where(jobs.c.id == ident, eligible, jobs.c.attempts == attempt)
                    .values(
                        state="running",
                        attempts=attempt + 1,
                        lease_token=uid(),
                        lease_until=stamp + lease_seconds,
                        updated_at=stamp,
                    )
                    .returning(jobs)
                )
                .mappings()
                .first()
            )
            if row:
                return dict(row)
    return None


def lease_condition(jobs, ident, token):
    return and_(
        jobs.c.id == ident,
        jobs.c.state == "running",
        jobs.c.lease_token == token,
        jobs.c.lease_until >= now(),
    )


def heartbeat(engine, jobs, ident, token, seconds=120):
    with engine.begin() as db:
        return (
            db.execute(
                update(jobs)
                .where(lease_condition(jobs, ident, token))
                .values(lease_until=now() + seconds, updated_at=now())
            ).rowcount
            == 1
        )


def finish(engine, jobs, ident, token, result):
    with engine.begin() as db:
        return (
            db.execute(
                update(jobs)
                .where(lease_condition(jobs, ident, token))
                .values(
                    state="completed",
                    result=result,
                    error_code=None,
                    lease_token=None,
                    lease_until=None,
                    updated_at=now(),
                )
            ).rowcount
            == 1
        )


def fail(engine, jobs, ident, token, code, retryable=True):
    with engine.begin() as db:
        row = db.execute(select(jobs).where(lease_condition(jobs, ident, token))).mappings().first()
        if not row:
            return False
        retry = retryable and row["attempts"] < 3
        return (
            db.execute(
                update(jobs)
                .where(lease_condition(jobs, ident, token))
                .values(
                    state="queued" if retry else "failed",
                    error_code=code,
                    lease_token=None,
                    lease_until=None,
                    available_at=now() + min(300, 10 * 2 ** row["attempts"]),
                    updated_at=now(),
                )
            ).rowcount
            == 1
        )


def defer(engine, jobs, ident, token, seconds=3):
    with engine.begin() as db:
        return (
            db.execute(
                update(jobs)
                .where(lease_condition(jobs, ident, token))
                .values(
                    state="queued",
                    attempts=jobs.c.attempts - 1,
                    lease_token=None,
                    lease_until=None,
                    available_at=now() + seconds,
                    updated_at=now(),
                )
            ).rowcount
            == 1
        )
