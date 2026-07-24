import os
import psycopg
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.environ["DBOS_SYSTEM_DATABASE_URL"]
SCRIPT_DIR = Path(__file__).resolve().parent
SQL_DIR = SCRIPT_DIR.parent / "schemas"


def run_migrations(sql_dir: Path):
    sql_paths = sorted(sql_dir.glob("*.sql"))
    if not sql_paths:
        print(f"No .sql files found in {sql_dir}")
        return

    conn = psycopg.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            for path in sql_paths:
                cur.execute(path.read_text())
                print(f"Applied {path.name}")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    run_migrations(SQL_DIR)