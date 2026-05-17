"""Allow `python -m worker_control ...` as an alias for the workerctl CLI."""
from worker_control.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
