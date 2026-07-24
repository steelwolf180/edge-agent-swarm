import argparse
import os
import psycopg
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SCRIPT_DIR = Path(__file__).resolve().parent
SQL_DIR = SCRIPT_DIR.parent / "schemas"


def run_migrations(sql_dir: Path, db_url: str):
    sql_paths = sorted(sql_dir.glob("*.sql"))
    if not sql_paths:
        print(f"No .sql files found in {sql_dir}")
        return

    conn = psycopg.connect(db_url)
    try:
        with conn.cursor() as cur:
            for path in sql_paths:
                cur.execute(path.read_text())
                print(f"Applied {path.name}")
        conn.commit()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Apply schemas/*.sql migrations.")
    parser.add_argument(
        "--target",
        choices=["system", "testing"],
        default="system",
        help="'system' = DBOS_SYSTEM_DATABASE_URL (default, real DB), "
             "'testing' = TESTING_DATABASE_URL.",
    )
    args = parser.parse_args()

    env_var = "DBOS_SYSTEM_DATABASE_URL" if args.target == "system" else "TESTING_DATABASE_URL"
    db_url = os.environ.get(env_var)
    if not db_url:
        raise ValueError(f"{env_var} not set in .env")

    print(f"Applying migrations to {args.target} database ({env_var})...")
    run_migrations(SQL_DIR, db_url)


if __name__ == "__main__":
    main()