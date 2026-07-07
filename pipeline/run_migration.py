import os
import psycopg
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.environ["DBOS_SYSTEM_DATABASE_URL"]
SCRIPT_DIR = Path(__file__).resolve().parent
SQL_PATH = SCRIPT_DIR.parent / "schemas" / "001_app_tables.sql"


def run_migration(sql_path: str):
    with open(sql_path) as f:
        sql = f.read()
    conn = psycopg.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print(f"Applied {sql_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    run_migration(str(SQL_PATH))
