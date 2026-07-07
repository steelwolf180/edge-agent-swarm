from dbos import DBOS, DBOSConfig

config: DBOSConfig = {
    "name": "edge-agent-swarm",
    "system_database_url": "postgresql://localhost:5432/edge_agent_swarm",
}
DBOS(config=config)
DBOS.launch()