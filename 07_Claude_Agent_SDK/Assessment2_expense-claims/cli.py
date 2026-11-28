"""Command-line front end for the expense claim review assistant.

  python cli.py submit                 enter a claim (saved to inbox/)
  python cli.py list                   claims in the inbox and their decision status
  python cli.py review CLM-3001        agent reviews a claim (tool calls shown)
  python cli.py ask CLM-3001 "why?"    one follow-up question on a reviewed claim
  python cli.py chat CLM-3001          interactive follow-ups on a reviewed claim
  python cli.py show CLM-3001          print the saved decision and reply
  python cli.py ingest-samples         copy data/claims.json into the inbox
  python cli.py batch [--force]        review every inbox claim that has no decision yet
  python cli.py export                 write results/ for every reviewed claim
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from expense_app import config, rules, store  # noqa: E402

CATEGORIES = ["meals", "travel", "equipment", "software", "other"]


# ---- intake (R1) -----------------------------------------------------------------------------

def _ask(prompt: str, required: bool = True, default: str = "") -> str:
    while True:
        value = input(f"{prompt}{f' [{default}]' if default else ''}: ").lstrip("﻿").strip() or default
        if value or not required:
            return value
        print("  (required)")


def _validate_claim(claim: dict) -> list[str]:
    problems = []
    try:
        date.fromisoformat(claim["expense_date"])
    except ValueError:
        problems.append("expense date must be YYYY-MM-DD")
    if claim["category"] not in CATEGORIES:
        problems.append(f"category must be one of: {', '.join(CATEGORIES)}")
    if claim["receipt"] not in ("attached", "missing"):
        problems.append("receipt must be 'attached' or 'missing'")
    try:
        rules.to_usd(claim["amount"], claim["currency"], store.fx_rates())
    except rules.RuleError as e:
        problems.append(str(e))
    if not claim["description"].strip():
        problems.append("description must not be empty")
    return problems


def submit_claim(claim_fields: dict) -> dict:
    """Validate, give the claim a unique id and save it to inbox/<id>.json."""
    claim = {
        "id": "CLM-" + uuid.uuid4().hex[:8].upper(),
        "employee": claim_fields["employee"].strip(),
        "submitted_at": store.now_iso(),
        "expense_date": claim_fields["expense_date"].strip(),
        "category": claim_fields["category"].strip().lower(),
        "vendor": claim_fields["vendor"].strip(),
        "amount": float(claim_fields["amount"]),
        "currency": claim_fields["currency"].strip().upper(),
        "receipt": claim_fields["receipt"].strip().lower(),
        "pre_approval": (claim_fields.get("pre_approval") or "").strip() or None,
        "description": claim_fields["description"].strip(),
    }
    problems = _validate_claim(claim)
    if problems:
        raise ValueError("; ".join(problems))
    if claim["employee"] not in store.employees():
        print(f"  note: {claim['employee']!r} is not in the employee directory; the review will ask "
              "the employee to confirm their name.")
    store.save_new_claim(claim)
    return claim


def cmd_submit(args) -> int:
    print("Enter a claim. Amount and currency are converted to USD at the fixed rate.")
    fields = {
        "employee": _ask("Employee name"),
        "expense_date": _ask("Expense date (YYYY-MM-DD)"),
        "category": _ask(f"Category ({'/'.join(CATEGORIES)})", default="meals"),
        "vendor": _ask("Vendor"),
        "amount": _ask("Amount"),
        "currency": _ask("Currency (USD, EUR, GBP, INR)", default="USD"),
        "receipt": _ask("Receipt (attached/missing)", default="attached"),
        "pre_approval": _ask("Pre-approval number (blank if none)", required=False),
        "description": _ask("Description"),
    }
    try:
        claim = submit_claim(fields)
    except ValueError as e:
        print(f"Not saved: {e}")
        return 1
    print(f"Saved {claim['id']} to {store.inbox_path(claim['id'])}")
    if args.review:
        return cmd_review(argparse.Namespace(claim_id=claim["id"]))
    return 0


# ---- listing and viewing ---------------------------------------------------------------------

def cmd_list(args) -> int:
    ids = store.list_claim_ids()
    if not ids:
        print("The inbox is empty. Use 'submit' or 'ingest-samples'.")
        return 0
    print(f"{'claim':<11} {'employee':<16} {'expense date':<12} {'amount':>10}  status")
    for cid in ids:
        try:
            c = store.load_claim(cid)
        except (store.StoreError, json.JSONDecodeError) as e:
            print(f"{cid:<11} could not read: {e}")
            continue
        dec = store.load_decision(cid)
        status = (f"{dec['decision']['decision']} {dec['decision']['approved_amount_usd']} "
                  f"(v{dec['version']})" if dec else "not reviewed")
        money = f"{c['amount']} {c['currency']}"
        print(f"{cid:<11} {c['employee']:<16} {c['expense_date']:<12} {money:>10}  {status}")
    return 0


def cmd_show(args) -> int:
    try:
        record = store.load_decision(args.claim_id)
    except store.StoreError as e:
        print(e)
        return 1
    if record is None:
        print(f"No decision saved for {args.claim_id} yet.")
        return 1
    d = record["decision"]
    print(f"Claim {d['claim_id']}  (saved version {record['version']} at {record['updated_at']})")
    print(f"  decision:        {d['decision']}")
    print(f"  approved (USD):  {d['approved_amount_usd']}")
    print(f"  escalate to:     {d['escalate_to']}" + (f"  ({d.get('escalation_reason', '')})"
                                                     if d['escalate_to'] != 'none' else ""))
    print(f"  policy refs:     {', '.join(d['policy_refs'])}")
    print(f"  flags:           {', '.join(d['flags']) or 'none'}")
    print("  reply to employee:")
    for line in d["reply_to_employee"].splitlines():
        print(f"    {line}")
    return 0


# ---- agent commands --------------------------------------------------------------------------

def _run_async(coro):
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        print("\nstopped.")
        return None


def _print_outcome(summary: dict) -> None:
    print(f"  turns={summary['turns']}  cost=${summary['cost_usd'] or 0:.4f}  "
          f"session={summary['session_id']}  status={summary['subtype']}")


def cmd_review(args) -> int:
    from expense_app import agent
    print(f"Reviewing {args.claim_id} with {config.MODEL} ...")
    try:
        summary = _run_async(agent.review_claim(args.claim_id))
    except store.StoreError as e:
        print(e)
        return 1
    if summary is None:
        return 1
    _print_outcome(summary)
    if store.load_decision(args.claim_id) is None:
        print("The agent did not save a decision. Agent output:\n" + summary["text"])
        return 1
    print()
    cmd_show(argparse.Namespace(claim_id=args.claim_id))
    return 0


def cmd_ask(args) -> int:
    from expense_app import agent
    try:
        summary = _run_async(agent.follow_up(args.claim_id, args.question))
    except store.StoreError as e:
        print(e)
        return 1
    if summary is None:
        return 1
    print("\n" + summary["text"])
    _print_outcome(summary)
    return 0


def cmd_chat(args) -> int:
    from expense_app import agent
    try:
        store.load_claim(args.claim_id)
    except store.StoreError as e:
        print(e)
        return 1
    if store.load_session_id(args.claim_id) is None:
        print(f"{args.claim_id} has not been reviewed yet. Run: python cli.py review {args.claim_id}")
        return 1
    print(f"Follow-up questions about {args.claim_id}. Type 'exit' to stop.")
    while True:
        try:
            question = input("you> ").strip()
        except EOFError:
            break
        if question.lower() in ("exit", "quit", ""):
            if question == "":
                continue
            break
        try:
            summary = _run_async(agent.follow_up(args.claim_id, question))
        except store.StoreError as e:
            print(e)
            return 1
        if summary is None:
            break
        print("\nagent> " + summary["text"] + "\n")
        _print_outcome(summary)
    return 0


def cmd_ingest(args) -> int:
    samples = json.loads((config.DATA_DIR / "claims.json").read_text(encoding="utf-8"))
    added = skipped = 0
    for claim in samples:
        if store.inbox_path(claim["id"]).exists():
            skipped += 1
            continue
        store.save_new_claim(claim)
        added += 1
    print(f"Ingested {added} sample claims into inbox/ ({skipped} already there).")
    return 0


def cmd_batch(args) -> int:
    from expense_app import agent
    ids = store.list_claim_ids()
    todo = [c for c in ids if args.force or store.load_decision(c) is None]
    if not todo:
        print("Nothing to review. Use --force to review everything again.")
        return 0
    total = 0.0
    for cid in todo:
        print(f"\n== {cid}")
        try:
            summary = _run_async(agent.review_claim(cid))
        except store.StoreError as e:
            print(e)
            continue
        if summary is None:
            break
        total += summary["cost_usd"] or 0
        _print_outcome(summary)
        if store.load_decision(cid) is None:
            print("  (no decision saved)")
    print(f"\nBatch finished. Total cost: ${total:.4f}")
    return 0


def cmd_export(args) -> int:
    """Copy every saved decision into results/ as <id>.decision.json and <id>.reply.txt."""
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for cid in store.list_claim_ids():
        record = store.load_decision(cid)
        if record is None:
            continue
        d = record["decision"]
        (config.RESULTS_DIR / f"{cid}.decision.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (config.RESULTS_DIR / f"{cid}.reply.txt").write_text(
            d["reply_to_employee"] + "\n", encoding="utf-8")
        rows.append(f"| {cid} | {d['decision']} | {d['approved_amount_usd']} | {d['escalate_to']} | "
                    f"{', '.join(d['flags']) or '-'} | {', '.join(d['policy_refs'])} |")
    summary = ["# Results", "", "| Claim | Decision | Approved (USD) | Escalate to | Flags | Policy refs |",
               "| --- | --- | ---: | --- | --- | --- |", *rows, ""]
    (config.RESULTS_DIR / "SUMMARY.md").write_text("\n".join(summary), encoding="utf-8")
    print(f"Exported {len(rows)} decisions to {config.RESULTS_DIR}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Expense claim review assistant (recommends only).")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit", help="enter a claim")
    s.add_argument("--review", action="store_true", help="review the claim straight after saving")
    sub.add_parser("list", help="list inbox claims")
    r = sub.add_parser("review", help="review a claim with the agent")
    r.add_argument("claim_id")
    a = sub.add_parser("ask", help="one follow-up question")
    a.add_argument("claim_id")
    a.add_argument("question")
    c = sub.add_parser("chat", help="interactive follow-ups")
    c.add_argument("claim_id")
    sh = sub.add_parser("show", help="show the saved decision")
    sh.add_argument("claim_id")
    sub.add_parser("ingest-samples", help="copy data/claims.json into the inbox")
    b = sub.add_parser("batch", help="review all unreviewed inbox claims")
    b.add_argument("--force", action="store_true", help="review every claim again")
    sub.add_parser("export", help="write results/ from the outbox")
    return p


DISPATCH = {"submit": cmd_submit, "list": cmd_list, "review": cmd_review, "ask": cmd_ask,
            "chat": cmd_chat, "show": cmd_show, "ingest-samples": cmd_ingest,
            "batch": cmd_batch, "export": cmd_export}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return DISPATCH[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
