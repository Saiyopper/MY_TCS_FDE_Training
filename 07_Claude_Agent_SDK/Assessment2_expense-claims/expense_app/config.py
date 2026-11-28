"""Paths and settings. Everything is read from the project folder, so a clean checkout works."""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = Path(os.environ.get("EXPENSE_DATA_DIR", PROJECT_ROOT / "data"))
INBOX_DIR = PROJECT_ROOT / "inbox"          # one JSON file per claim (R1)
OUTBOX_DIR = PROJECT_ROOT / "outbox"        # one JSON file per decision (R2)
SESSIONS_DIR = PROJECT_ROOT / "sessions"    # claim id -> Claude session id (R3)
LOGS_DIR = PROJECT_ROOT / "logs"            # tool-call audit log and cost log (R5)
RESULTS_DIR = PROJECT_ROOT / "results"      # exported decisions for the sample claims

# Model used by the reviewer. Override with EXPENSE_MODEL in .env.
MODEL = os.environ.get("EXPENSE_MODEL", "claude-sonnet-5-5")

# Bounds on one agent run (R5). A single review or follow-up stops at either limit.
MAX_TURNS = int(os.environ.get("EXPENSE_MAX_TURNS", "25"))
MAX_BUDGET_USD = float(os.environ.get("EXPENSE_MAX_BUDGET_USD", "0.50"))
