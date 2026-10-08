"""Delete terminal job inputs without deleting transcripts or breaking active leases."""

import logging
from pathlib import Path

from sqlalchemy import or_, select, update

log = logging.getLogger(__name__)


def cleanup_terminal_media(engine, jobs):
    terminal = jobs.c.state.in_(["completed", "failed", "deleted"])
    with engine.connect() as db:
        rows = (
            db.execute(
                select(jobs)
                .where(
                    terminal,
                    or_(
                        jobs.c.payload["path"].as_string().is_not(None),
                        jobs.c.payload["pending_delete_path"].as_string().is_not(None),
                    ),
                )
                .limit(100)
            )
            .mappings()
            .all()
        )
    for row in rows:
        payload = dict(row["payload"])
        path = payload.pop("path", None) or payload.get("pending_delete_path")
        payload["pending_delete_path"] = path
        with engine.begin() as db:
            changed = db.execute(
                update(jobs)
                .where(
                    jobs.c.id == row["id"],
                    jobs.c.state == row["state"],
                    jobs.c.updated_at == row["updated_at"],
                    jobs.c.payload["path"].as_string() == row["payload"].get("path"),
                    jobs.c.payload["pending_delete_path"].as_string()
                    == row["payload"].get("pending_delete_path"),
                )
                .values(payload=payload)
            ).rowcount
        if not changed:
            continue
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Media cleanup deferred for %s (%s)", row["id"], type(exc).__name__)
            continue
        payload.pop("pending_delete_path", None)
        with engine.begin() as db:
            db.execute(
                update(jobs)
                .where(
                    jobs.c.id == row["id"],
                    jobs.c.state == row["state"],
                    jobs.c.updated_at == row["updated_at"],
                    jobs.c.payload["pending_delete_path"].as_string() == path,
                )
                .values(payload=payload)
            )
