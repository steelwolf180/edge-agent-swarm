import os
import psycopg2

DB_URL = os.environ.get("APP_DATABASE_URL", "postgresql://localhost:5432/edge_agent_swarm")

def run_migration(sql_path: str):
    with open(sql_path) as f:
        sql = f.read()
    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print(f"Applied {sql_path}")
    finally:
        conn.close()

if __name__ == "__main__":
    run_migration("schemas/001_app_tables.sql")