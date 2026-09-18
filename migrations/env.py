import os
from pathlib import Path

from alembic import context
from sqlalchemy import MetaData, create_engine, pool

from gigaam_api.queue import job_table

storage = Path(os.environ.get("ASR_STORAGE", "data/asr")).resolve()
storage.mkdir(parents=True, exist_ok=True)
url = os.environ.get("ASR_DATABASE_URL", f"sqlite:///{storage / 'asr.db'}")
target_metadata = MetaData()
job_table(target_metadata)

if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
