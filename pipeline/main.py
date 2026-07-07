import os
from dbos import DBOS, DBOSConfig
from dotenv import load_dotenv

load_dotenv()

config: DBOSConfig = {
    "name": "edge-agent-swarm",
    "system_database_url": os.environ["DBOS_SYSTEM_DATABASE_URL"],
    "admin_port": 3002,
}
DBOS(config=config)
DBOS.launch()