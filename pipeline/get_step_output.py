"""
Fetch a single step's persisted output from a DBOS workflow, even if the
workflow itself errored downstream before reaching human review.

Usage:
    python get_step_output.py <workflow_id> [step_name_substring]

Example:
    python get_step_output.py 8b81d888-84be-48a0-933c-44d92f206ca0 scribe_step
"""
import json
import os
import sys

from dbos import DBOS, DBOSConfig
from dotenv import load_dotenv

load_dotenv()


def main():
    if len(sys.argv) < 2:
        print("Usage: python get_step_output.py <workflow_id> [step_name_substring]")
        sys.exit(1)

    workflow_id = sys.argv[1]
    name_filter = sys.argv[2] if len(sys.argv) > 2 else None

    config: DBOSConfig = {
        "name": "edge-agent-swarm",
        "system_database_url": os.environ.get("DBOS_SYSTEM_DATABASE_URL"),
    }
    DBOS(config=config)
    DBOS.launch()

    steps = DBOS.list_workflow_steps(workflow_id)

    for step in steps:
        if name_filter and name_filter not in step["function_name"]:
            continue
        print(f"--- step {step['function_id']}: {step['function_name']} ---")
        if step["error"] is not None:
            print(f"ERROR: {step['error']}")
        else:
            try:
                print(json.dumps(step["output"], indent=2, default=str))
            except TypeError:
                print(step["output"])
        print()


if __name__ == "__main__":
    main()