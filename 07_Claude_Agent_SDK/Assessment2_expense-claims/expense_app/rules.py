"""Deterministic policy arithmetic and checks (EX-200 to EX-206).

Plain Python with Decimal, no model involved. The agent calls these through tools, and the
guard re-runs the same checks before a decision is saved.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
MAX_DAYS_TO_SUBMIT = 60            # EX-204
RECEIPT_THRESHOLD_USD = Decimal("25")  # EX-204
EQUIPMENT_LOW_USD = Decimal("150")     # EX-203
EQUIPMENT_HIGH_USD = Decimal("1000")   # EX-203
PER_NIGHT_LIMIT = Decimal("220")       # EX-202
HIGH_COST_NIGHT_LIMIT = Decimal("320") # EX-202
HIGH_COST_CITIES = {"london", "new york", "san francisco", "singapore", "zurich"}


class RuleError(ValueError):
    """Bad input to a rule. The message is written for the agent to read and recover from."""


def parse_amount(value) -> Decimal:
    """A positive, finite amount. Rejects strings, booleans and zero with a helpful message."""
    if isinstance(value, bool):
        raise RuleError(f"amount must be a number, got {value!r}")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise RuleError(f"amount must be a number, got {value!r}") from None
    if not d.is_finite() or d <= 0:
        raise RuleError(f"amount must be greater than zero, got {value!r}")
    return d


def money(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def supported_rates(fx: dict) -> dict[str, Decimal]:
    return {k: Decimal(str(v)) for k, v in fx.items() if not k.startswith("_")}


def to_usd(amount, currency: str, fx: dict) -> Decimal:
    """Convert to USD with the fixed quarterly rate, rounded half-up to the cent (EX-206)."""
    rates = supported_rates(fx)
    cur = str(currency or "").strip().upper()
    if cur not in rates:
        raise RuleError(f"unsupported currency {currency!r}; supported: {', '.join(sorted(rates))}")
    return money(parse_amount(amount) * rates[cur])


def days_between(expense_date: str, submitted_at: str) -> int:
    """Whole days from the expense date to the submission date (EX-204)."""
    try:
        e = date.fromisoformat(str(expense_date)[:10])
        s = date.fromisoformat(str(submitted_at)[:10])
    except (TypeError, ValueError):
        raise RuleError("dates must be ISO format YYYY-MM-DD") from None
    return (s - e).days


def _norm(text) -> str:
    return " ".join(str(text or "").lower().split())


def same_purchase_key(a: dict, b: dict) -> bool:
    """Same employee, vendor and expense date (the grouping used for duplicates and splits)."""
    return (
        _norm(a.get("employee")) == _norm(b.get("employee"))
        and _norm(a.get("vendor")) == _norm(b.get("vendor"))
        and str(a.get("expense_date")) == str(b.get("expense_date"))
    )


def find_duplicates(claim: dict, past: list[dict]) -> list[dict]:
    """EX-205: same employee, vendor, expense date, amount and currency as an earlier claim."""
    out = []
    for p in past:
        if (
            same_purchase_key(claim, p)
            and str(p.get("currency", "")).upper() == str(claim.get("currency", "")).upper()
            and Decimal(str(p["amount"])) == parse_amount(claim["amount"])
        ):
            out.append(p)
    return out


def valid_preapproval(rec: dict | None, pa_id, needed_type: str, usd: Decimal) -> tuple[bool, str]:
    """EX-206: valid only on this employee's record, right type, and max_usd at least the amount."""
    if not pa_id:
        return False, "no pre-approval number was given"
    if rec is None:
        return False, "the employee is not in the directory"
    for pa in rec.get("preapprovals", []):
        if pa.get("id") == pa_id:
            if pa.get("type") != needed_type:
                return False, f"pre-approval {pa_id} is not of type {needed_type}"
            if Decimal(str(pa["max_usd"])) < usd:
                return False, f"pre-approval {pa_id} covers less than the claimed USD amount"
            return True, f"pre-approval {pa_id} is valid for this claim"
    return False, f"{pa_id} is not listed on this employee's record"


def cap_status(rec: dict, usd: Decimal) -> dict:
    """EX-205: month-to-date spend plus this amount against the grade's monthly cap."""
    mtd = Decimal(str(rec["month_to_date_usd"]))
    cap = Decimal(str(rec["monthly_cap_usd"]))
    total = mtd + usd
    return {
        "month_to_date_usd": str(money(mtd)),
        "monthly_cap_usd": str(money(cap)),
        "total_if_approved_usd": str(money(total)),
        "exceeds_cap": total > cap,
    }


def split_group(claim: dict, past: list[dict], fx: dict, rec: dict | None) -> dict | None:
    """EX-205 split purchase. Same employee, vendor and day; every claim under $150; total over $150.

    The claim being reviewed must itself be under $150. A valid equipment pre-approval whose
    max covers the total means it is not a split.
    """
    usd = to_usd(claim["amount"], claim["currency"], fx)
    if usd >= EQUIPMENT_LOW_USD:
        return None
    group = [claim]
    for p in past:
        if same_purchase_key(claim, p) and to_usd(p["amount"], p["currency"], fx) < EQUIPMENT_LOW_USD:
            group.append(p)
    if len(group) < 2:
        return None
    total = sum((to_usd(g["amount"], g["currency"], fx) for g in group), Decimal("0"))
    if total <= EQUIPMENT_LOW_USD:
        return None
    if rec is not None:
        for pa in rec.get("preapprovals", []):
            if pa.get("type") == "equipment" and Decimal(str(pa["max_usd"])) >= total:
                return None
    return {
        "claim_ids": [g.get("id") for g in group if g is not claim] + [claim.get("id")],
        "total_usd": str(money(total)),
    }


def deterministic_checks(claim: dict, employees: dict, past: list[dict], fx: dict,
                         amount_usd: Decimal | None = None,
                         needed_preapproval_type: str | None = None) -> dict:
    """Facts for steps 2 to 5 and 7, plus the outcome when code alone settles the claim.

    `settled_by_code` is set only for duplicate (reject), late (escalate to finance-controller)
    and split (escalate to compliance), in that order. The first one that applies wins.
    """
    rec = employees.get(claim.get("employee"))
    usd = to_usd(claim["amount"], claim["currency"], fx)
    usd_for_caps = amount_usd if amount_usd is not None else usd

    duplicates = find_duplicates(claim, past)
    days = days_between(claim["expense_date"], claim["submitted_at"])
    late = days > MAX_DAYS_TO_SUBMIT
    split = split_group(claim, past, fx, rec)

    settled = None
    if duplicates:
        settled = {"decision": "reject", "escalate_to": "none", "flag": "duplicate", "policy_ref": "EX-205"}
    elif late:
        settled = {"decision": "escalate", "escalate_to": "finance-controller",
                   "flag": "late_submission", "policy_ref": "EX-204"}
    elif split:
        settled = {"decision": "escalate", "escalate_to": "compliance",
                   "flag": "split_purchase", "policy_ref": "EX-205"}

    facts = {
        "claim_id": claim.get("id"),
        "employee_found": rec is not None,
        "claimed_amount_usd": str(money(usd)),
        "fx_rate_used": str(supported_rates(fx)[claim["currency"].upper()]),
        "days_from_expense_to_submission": days,
        "late_submission": late,
        "duplicates_of": [d.get("id") for d in duplicates],
        "split_group": split,
        "receipt_attached": claim.get("receipt") == "attached",
        "receipt_required": usd >= RECEIPT_THRESHOLD_USD,
        "missing_required_receipt": usd >= RECEIPT_THRESHOLD_USD and claim.get("receipt") != "attached",
        "settled_by_code": settled,
    }
    if str(claim.get("category", "")).lower() == "equipment":
        if usd < EQUIPMENT_LOW_USD:
            band = "under_150"
        elif usd <= EQUIPMENT_HIGH_USD:
            band = "150_to_1000"
        else:
            band = "over_1000"
        facts["equipment_band"] = band
    if needed_preapproval_type:
        ok, why = valid_preapproval(rec, claim.get("pre_approval"), needed_preapproval_type, usd)
        facts["preapproval_check"] = {"type_needed": needed_preapproval_type, "valid": ok, "reason": why}
    if rec is not None:
        facts["monthly_cap"] = cap_status(rec, usd_for_caps)
        facts["monthly_cap"]["amount_checked_usd"] = str(money(usd_for_caps))
    return facts


def calculate_reimbursement(lines: list[dict], group_cap_usd=None) -> dict:
    """Sum USD lines. A line is 'reimbursable' or 'excluded' (contributes 0 but is reported).

    Each reimbursable line may carry its own cap (e.g. a hotel night at min(rate, $320)).
    `group_cap_usd` caps the total (e.g. a meal at $75 x attendees).
    """
    if not isinstance(lines, list) or not lines:
        raise RuleError("lines must be a non-empty list of {label, amount_usd, kind, cap_usd?}")
    out = []
    subtotal = Decimal("0")
    for i, line in enumerate(lines):
        if not isinstance(line, dict):
            raise RuleError(f"line {i + 1} must be an object")
        label = str(line.get("label", f"line {i + 1}"))
        kind = str(line.get("kind", "reimbursable")).lower()
        if kind not in ("reimbursable", "excluded"):
            raise RuleError(f"line '{label}': kind must be 'reimbursable' or 'excluded'")
        amount = parse_amount(line.get("amount_usd"))
        value = Decimal("0")
        capped = False
        if kind == "reimbursable":
            value = amount
            if line.get("cap_usd") is not None:
                cap = parse_amount(line["cap_usd"])
                if value > cap:
                    value = cap
                    capped = True
            value = money(value)
            subtotal += value
        out.append({"label": label, "kind": kind, "amount_usd": str(money(amount)),
                    "counted_usd": str(value), "capped": capped})
    before_group = money(subtotal)
    final = before_group
    group_capped = False
    if group_cap_usd is not None:
        g = parse_amount(group_cap_usd)
        if final > g:
            final = money(g)
            group_capped = True
    return {"lines": out, "subtotal_usd": str(before_group),
            "group_cap_applied": group_capped, "total_usd": str(final)}
