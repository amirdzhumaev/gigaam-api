"""Generate the client contract without credentials or external API calls."""

import json
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    from gigaam_api.app import Settings, create_app

    app = create_app(
        Settings(f"sqlite:///{path / 'schema.db'}", path, {"schema-token": "example"}, "schema-worker")
    )
    destination = Path("docs/openapi.json")
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n")
