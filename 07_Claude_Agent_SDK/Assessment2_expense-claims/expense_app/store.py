"""File storage: the inbox (claims), the outbox (decisions), sessions and the data files."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config

CLAIM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


class StoreError(Exception):
    """A readable error for the user or the agent (unknown claim, bad id, etc.)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_claim_id(claim_id) -> str:
    if not isinstance(claim_id, str) or not CLAIM_ID_RE.match(claim_id):
        raise StoreError(f"invalid claim id {claim_id!r}; ids look like CLM-1A2B3C4D")
    return claim_id


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---- reference data (read-only) -------------------------------------------------------------

def employees() -> dict:
    return _read_json(config.DATA_DIR / "employees.json")


def past_claims() -> list[dict]:
    return _read_json(config.DATA_DIR / "past_claims.json")


def fx_rates() -> dict:
    return _read_json(config.DATA_DIR / "fx_rates.json")


# ---- inbox: one file per claim ----------------------------------------------------------------

def inbox_path(claim_id: str) -> Path:
    return config.INBOX_DIR / f"{check_claim_id(claim_id)}.json"


def save_new_claim(claim: dict) -> Path:
    """Write a claim to inbox/<id>.json. Refuses to overwrite an existing claim."""
    path = inbox_path(claim["id"])
    if path.exists():
        raise StoreError(f"claim {claim['id']} already exists")
    _atomic_write(path, json.dumps(claim, indent=2, ensure_ascii=False) + "\n")
    return path


def load_claim(claim_id: str) -> dict:
    path = inbox_path(claim_id)
    if not path.exists():
        raise StoreError(f"no claim {claim_id} in the inbox")
    return _read_json(path)


def list_claim_ids() -> list[str]:
    if not config.INBOX_DIR.exists():
        return []
    return sorted(p.stem for p in config.INBOX_DIR.glob("*.json"))


# ---- outbox: one decision per claim, with version history -----------------------------------

def outbox_path(claim_id: str) -> Path:
    return config.OUTBOX_DIR / f"{check_claim_id(claim_id)}.json"


def load_decision(claim_id: str) -> dict | None:
    path = outbox_path(claim_id)
    return _read_json(path) if path.exists() else None


def save_decision(claim_id: str, decision: dict, session_id: str | None) -> dict:
    """Save the decision. Each update keeps the previous version in outbox/history/<id>.jsonl."""
    previous = load_decision(claim_id)
    version = (previous or {}).get("version", 0) + 1
    record = {
        "version": version,
        "updated_at": now_iso(),
        "session_id": session_id,
        "decision": decision,
    }
    if previous is not None:
        hist = config.OUTBOX_DIR / "history" / f"{claim_id}.jsonl"
        hist.parent.mkdir(parents=True, exist_ok=True)
        with hist.open("a", encoding="utf-8") as f:
            f.write(json.dumps(previous, ensure_ascii=False) + "\n")
    _atomic_write(outbox_path(claim_id), json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return record


# ---- sessions: which Claude session belongs to which claim (survives restarts) -------------

def _session_path(claim_id: str) -> Path:
    return config.SESSIONS_DIR / f"{check_claim_id(claim_id)}.json"


def load_session_id(claim_id: str) -> str | None:
    path = _session_path(claim_id)
    if not path.exists():
        return None
    return _read_json(path).get("session_id")


def save_session_id(claim_id: str, session_id: str) -> None:
    _atomic_write(_session_path(claim_id),
                  json.dumps({"claim_id": claim_id, "session_id": session_id,
                              "updated_at": now_iso()}, indent=2) + "\n")


# ---- logs -----------------------------------------------------------------------------------

def append_log(filename: str, record: dict) -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with (config.LOGS_DIR / filename).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
