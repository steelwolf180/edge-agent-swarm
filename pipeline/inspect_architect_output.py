"""
One-off inspection script: pull the checkpointed Architect step output for a
specific (already-completed) workflow, without re-running the pipeline.

Usage:
    python inspect_architect_output.py 99085c84-d178-4bd7-8543-3affaa9aea57

DBOS checkpoints each @DBOS.step() output as it completes, so even though
this workflow later failed at validate_diagram_renders_step, Architect's
output (including the full raw context_diagram) is already sitting in the
system database. This just reads it back -- no llama-server, no thermal
guard, no ~11 min re-run.
"""
import os
import sys

from dbos import DBOS, DBOSConfig
from dotenv import load_dotenv

load_dotenv()


def main(workflow_id: str) -> None:
    config: DBOSConfig = {
        "name": "edge-agent-swarm",
        "system_database_url": os.environ.get("DBOS_SYSTEM_DATABASE_URL"),
        # Read-only inspection script -- don't start an admin server at all,
        # so it can't collide with mermaid-ink on :3001 (pipeline/run.py
        # works around the same collision with admin_port=3010, but this
        # script has no need for an admin server in the first place).
        "run_admin_server": False,
    }
    DBOS(config=config)
    DBOS.launch()

    # load_output isn't a valid kwarg on this installed DBOS version
    # (v2.26.0 per the run.py log) -- step output is loaded by default.
    steps = DBOS.list_workflow_steps(workflow_id)

    if not steps:
        print(f"No steps found for workflow_id={workflow_id}. Check the id.")
        return

    print(f"Found {len(steps)} step(s) for workflow {workflow_id}:\n")
    for step in steps:
        print(f"  [{step['function_id']}] {step['function_name']}"
              f"{'  <-- ERRORED' if step.get('error') else ''}")

    # Architect's step function is architect_step (per pipeline/run.py) --
    # adjust the match below if your actual function name differs.
    architect_step = next(
        (s for s in steps if "architect" in s["function_name"].lower()
         and "validate" not in s["function_name"].lower()),
        None,
    )

    if architect_step is None:
        print("\nCould not find an architect_step entry by name -- "
              "check the step list above and adjust the filter.")
        return

    output = architect_step.get("output")
    if output is None:
        print("\nArchitect step has no recorded output (unexpected -- "
              "it should have completed before the render step failed).")
        return

    print("\n" + "=" * 70)
    print("RAW context_diagram FROM THE FAILED RUN:")
    print("=" * 70)
    # output shape mirrors ArchitectOutput.model_dump(mode="json") per
    # run.py's step-boundary serialization convention.
    diagram = output.get("context_diagram") if isinstance(output, dict) else None
    print(diagram if diagram else output)
    print("=" * 70)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python inspect_architect_output.py <workflow_id>")
        sys.exit(1)
    main(sys.argv[1])