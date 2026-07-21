import os
import pytest
from dbos import DBOS, DBOSConfig
from dotenv import load_dotenv

load_dotenv()

@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_DBOS_TESTS") != "1",
    reason="set RUN_DBOS_TESTS=1 to run against a live DBOS/Postgres instance",
)
def test_dbos_connects():
    config: DBOSConfig = {
        "name": "edge-agent-swarm",
        "system_database_url": os.environ["DBOS_SYSTEM_DATABASE_URL"],
        "admin_port": 3002,
    }
    DBOS(config=config)
    DBOS.launch()