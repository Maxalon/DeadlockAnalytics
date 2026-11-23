"""The site for the browser tests: the fixtures' game data, no scheduler, no network.

python -m tests.browser.serve <port>
"""

import sys
import tempfile
from pathlib import Path

import uvicorn

from app import db, jobs
from app.config import Settings
from app.main import create_app
from tests.test_theorycraft import GameApi


def main(port: int) -> None:
    settings = Settings(database_path=str(Path(tempfile.mkdtemp()) / "browser.sqlite3"), run_scheduler=False)
    api = GameApi()
    app = create_app(settings, api=api)
    conn = db.connect(settings.database_path)
    jobs.refresh_game_data(conn, api)
    conn.close()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main(int(sys.argv[1]))
