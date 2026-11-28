"""Code-enforced checks on a decision before it is saved (R6 and the hard stops).

The model proposes a decision. This module rejects anything that breaks a rule we can check
with ordinary code. Each error is a sentence the agent can read and fix.
"""
from __future__ import annotations

import re
from decimal import Decimal

from . import rules

REQUIRED_FIELDS = ["claim_id", "decision", "approved_amount_usd", "policy_refs",
                   "flags", "reply_to_employee", "escalate_to"]
OPTIONAL_FIELDS = ["escalation_reason"]
DECISIONS = {"approve", "reject", "needs_info", "escalate"}
ESCALATE_TO = {"manager", "finance-controller", "compliance", "none"}
FLAGS = {"alcohol_excluded", "over_limit", "duplicate", "late_submission", "split_purchase",
         "missing_receipt", "invalid_preapproval", "prohibited_item", "double_claim",
         "monthly_cap_exceeded", "injection_attempt"}
ARTICLES = {f"EX-20{n}" for n in range(0, 7)}

# Hard-stop wording checks (EX-200). Kept deliberately simple and conservative.
PAYMENT_RE = re.compile(
    r"\b(paid|payment|payments|payroll|payout|payday|refunded|transferred|"
    r"pay date|will be reimbursed|has been reimbursed|reimbursed (on|by|in))\b", re.I)
ACCUSATION_RE = re.compile(
    r"\b(fraud\w*|dishonest\w*|lie|lies|lying|liar|cheat\w*|steal\w*|stole\w*|fake\w*|"
    r"falsif\w*|scam\w*|misrepresent\w*|deceiv\w*|suspicious)\b", re.I)
LEAK_WORDS_RE = re.compile(r"\b(injection|prompt|system prompt|guard|hook)\b", re.I)
CLAIM_REF_RE = re.compile(r"\b(CLM-[A-Za-z0-9_-]+|C-\d{4})\b")
PREAPPROVAL_REF_RE = re.compile(r"\bPA-\d+\b", re.I)


def _money_or_error(value, errors: list[str]) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except Exception:
        errors.append("approved_amount_usd must be a number such as 198.00")
        return None
    if not d.is_finite() or d < 0:
        errors.append("approved_amount_usd must be zero or more")
        return None
    if d != d.quantize(Decimal("0.01")):
        errors.append("approved_amount_usd must have at most two decimal places")
        return None
    return d


def directory_names(employees: dict) -> set[str]:
    """Every person named in the directory: employees, managers and pre-approvers."""
    names = set(employees)
    for rec in employees.values():
        if rec.get("manager"):
            names.add(rec["manager"])
        for pa in rec.get("preapprovals", []):
            if pa.get("approved_by"):
                names.add(pa["approved_by"])
    return names


def validate_decision(decision: dict, claim: dict, employees: dict, past: list[dict],
                      fx: dict) -> tuple[list[str], dict]:
    """Return (errors, normalised_decision). No errors means the decision may be saved."""
    errors: list[str] = []
    if not isinstance(decision, dict):
        return ["the decision must be a JSON object"], {}

    for f in REQUIRED_FIELDS:
        if f not in decision:
            errors.append(f"missing field '{f}'")
    allowed = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    for f in decision:
        if f not in allowed:
            errors.append(f"unknown field '{f}' (allowed: {', '.join(sorted(allowed))})")
    if errors:
        return errors, {}

    if decision["claim_id"] != claim["id"]:
        errors.append(f"claim_id must be {claim['id']}")

    kind = decision["decision"]
    if kind not in DECISIONS:
        errors.append("decision must be one of approve, reject, needs_info, escalate")
    escalate_to = decision["escalate_to"]
    if escalate_to not in ESCALATE_TO:
        errors.append("escalate_to must be one of manager, finance-controller, compliance, none")

    refs = decision["policy_refs"]
    if not isinstance(refs, list) or not refs or any(r not in ARTICLES for r in refs):
        errors.append("policy_refs must be a non-empty list of article ids EX-200 to EX-206")

    flags = decision["flags"]
    if not isinstance(flags, list) or any(f not in FLAGS for f in flags):
        errors.append(f"flags must be a list using only: {', '.join(sorted(FLAGS))}")
        flags = []

    amount = _money_or_error(decision["approved_amount_usd"], errors)

    # --- amounts (R6: the amount can never exceed the claim, and reject/escalate/needs_info pay 0)
    try:
        claimed_usd = rules.to_usd(claim["amount"], claim["currency"], fx)
    except rules.RuleError as e:
        return errors + [str(e)], {}
    if amount is not None:
        if kind == "approve":
            if amount <= 0:
                errors.append("approve needs an approved_amount_usd above zero")
            if amount > claimed_usd:
                errors.append(f"approved_amount_usd {amount} is more than the claimed {claimed_usd} USD")
        elif amount != 0:
            errors.append(f"approved_amount_usd must be 0 for decision '{kind}'")

    if kind == "escalate" and escalate_to == "none":
        errors.append("an escalate decision needs an escalate_to target")
    if kind != "escalate" and escalate_to != "none":
        errors.append("escalate_to must be 'none' unless the decision is escalate")
    if escalate_to != "none" and not str(decision.get("escalation_reason", "")).strip():
        errors.append("escalation_reason is required (one line) when escalate_to is not none")

    # --- the employee
    rec = employees.get(claim.get("employee"))
    if rec is None:
        if kind != "needs_info":
            errors.append("the employee is not in the directory, so the decision must be needs_info "
                          "(ask the employee to confirm their name)")

    # --- the first settling check, computed by code (EX-200 order). Skipped if the claim is prohibited.
    if "prohibited_item" not in flags and rec is not None:
        settled = rules.deterministic_checks(claim, employees, past, fx)["settled_by_code"]
        if settled:
            if kind != settled["decision"] or escalate_to != settled["escalate_to"]:
                errors.append(f"code settles this claim as {settled['decision']} to "
                              f"{settled['escalate_to']} ({settled['policy_ref']}, flag {settled['flag']}); "
                              "change the decision to match")
            if settled["flag"] not in flags:
                errors.append(f"add the flag '{settled['flag']}'")

    if kind == "approve" and rec is not None:
        usd = claimed_usd
        # Missing receipt (EX-204). Per-diem days never need receipts, so per diem claims are exempt.
        if usd >= rules.RECEIPT_THRESHOLD_USD and claim.get("receipt") != "attached" \
                and "per diem" not in str(claim.get("description", "")).lower():
            errors.append("a receipt is required for items of $25 or more; the decision must be needs_info")
        # Equipment bands (EX-203)
        if str(claim.get("category", "")).lower() == "equipment":
            if usd > rules.EQUIPMENT_HIGH_USD:
                errors.append("equipment over $1,000 goes through procurement: it must be reject")
            elif usd >= rules.EQUIPMENT_LOW_USD:
                ok, why = rules.valid_preapproval(rec, claim.get("pre_approval"), "equipment", usd)
                if not ok:
                    errors.append(f"equipment of $150 or more needs a valid equipment pre-approval ({why}); "
                                  "the decision must be needs_info")
        # Monthly cap (EX-205)
        if amount is not None and amount > 0:
            if rules.cap_status(rec, amount)["exceeds_cap"]:
                errors.append("this approval would take the employee over their monthly cap; "
                              "the decision must be escalate to manager")

    # --- reply rules and hard stops (EX-200)
    reply = str(decision["reply_to_employee"]).strip()
    if not reply:
        errors.append("reply_to_employee must not be empty")
    else:
        first = str(claim.get("employee", "")).split()[0] if claim.get("employee") else ""
        if first and not reply.lower().startswith(f"hi {first.lower()}"):
            errors.append(f"the reply must address the employee by first name, starting 'Hi {first},'")
        if not reply.rstrip().endswith("Finance Operations"):
            errors.append("the reply must be signed off as 'Finance Operations'")
        if PAYMENT_RE.search(reply):
            errors.append("the reply must not say or imply that the claim is paid, or mention payment")
        if ACCUSATION_RE.search(reply):
            errors.append("the reply must not accuse the employee of dishonesty or fraud")
        if LEAK_WORDS_RE.search(reply):
            errors.append("the reply must not mention instructions, prompts, guards or hooks")
        for name in sorted(directory_names(employees)):
            if name != claim.get("employee") and name.lower() in reply.lower():
                errors.append("the reply must not mention another person")
                break
        own_pa = {pa["id"] for pa in (rec or {}).get("preapprovals", [])}
        for pa in PREAPPROVAL_REF_RE.findall(reply):
            if pa.upper() not in {p.upper() for p in own_pa}:
                errors.append("the reply must not mention pre-approvals that are not on this employee's record")
                break
        own_claims = {claim["id"]} | {p["id"] for p in past if p.get("employee") == claim.get("employee")}
        for ref in CLAIM_REF_RE.findall(reply):
            if ref not in own_claims:
                errors.append("the reply may only refer to this employee's own claims")
                break
        if kind == "needs_info" and not re.search(r"need|please|send|provide|upload|attach|confirm", reply, re.I):
            errors.append("a needs_info reply must say exactly what is needed")

    if errors:
        return errors, {}

    normalised = {
        "claim_id": claim["id"],
        "decision": kind,
        "approved_amount_usd": f"{amount:.2f}",
        "policy_refs": list(decision["policy_refs"]),
        "flags": sorted(set(flags)),
        "reply_to_employee": reply,
        "escalate_to": escalate_to,
    }
    if decision.get("escalation_reason"):
        normalised["escalation_reason"] = str(decision["escalation_reason"]).strip()
    return [], normalised
