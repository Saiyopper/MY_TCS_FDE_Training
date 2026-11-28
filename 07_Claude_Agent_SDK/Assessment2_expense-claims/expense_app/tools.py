"""In-process MCP server 'expense' (R4): the policy tools the review agent may call.

Each tool does one job that code should do rather than the model: reading policy articles,
looking up records, converting currency, summing amounts, checking rules, and validating and
saving decisions. Every tool returns a readable error instead of raising, so the agent can recover.
"""
from __future__ import annotations

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import config, guard, rules, store

SERVER_NAME = "expense"
ARTICLE_FILES = {
    "EX-200": config.DATA_DIR / "expense_policy.md",
    "EX-201": config.DATA_DIR / "kb" / "ex-201-meals-and-entertainment.md",
    "EX-202": config.DATA_DIR / "kb" / "ex-202-travel.md",
    "EX-203": config.DATA_DIR / "kb" / "ex-203-equipment-and-software.md",
    "EX-204": config.DATA_DIR / "kb" / "ex-204-documentation-and-timing.md",
    "EX-205": config.DATA_DIR / "kb" / "ex-205-prohibited-duplicates-splits-and-caps.md",
    "EX-206": config.DATA_DIR / "kb" / "ex-206-currency-preapprovals-and-payment.md",
}


def _ok(payload) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}]}


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _find_employee(name: str) -> tuple[str | None, dict | None]:
    directory = store.employees()
    if name in directory:
        return name, directory[name]
    lowered = " ".join(str(name or "").lower().split())
    for key, rec in directory.items():
        if key.lower() == lowered:
            return key, rec
    return None, None


@tool(
    "read_policy_article",
    "Read one expense policy article by id. Valid ids: EX-200 (decision framework, order of "
    "checks, hard stops), EX-201 (meals), EX-202 (travel), EX-203 (equipment and software), "
    "EX-204 (receipts and deadlines), EX-205 (prohibited items, duplicates, splits, caps), "
    "EX-206 (currency and pre-approvals).",
    {"type": "object", "properties": {"article_id": {"type": "string"}}, "required": ["article_id"]},
)
async def read_policy_article(args: dict) -> dict:
    article_id = str(args.get("article_id", "")).strip().upper()
    path = ARTICLE_FILES.get(article_id)
    if path is None:
        return _error(f"unknown article {article_id!r}. Valid ids: {', '.join(sorted(ARTICLE_FILES))}")
    return _ok(path.read_text(encoding="utf-8"))


@tool(
    "lookup_employee",
    "Get an employee's record from the directory by full name: employee_id, grade, manager, "
    "monthly_cap_usd, month_to_date_usd and preapprovals. Returns an error if the name is unknown.",
    {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
async def lookup_employee(args: dict) -> dict:
    key, rec = _find_employee(str(args.get("name", "")))
    if rec is None:
        return _error(f"no employee named {args.get('name')!r} in the directory. "
                      "Check the spelling against the claim; the reply should ask the employee to confirm.")
    return _ok({"name": key, **rec})


@tool(
    "find_earlier_claims",
    "List the already-processed claims for one employee (id, date, vendor, amount, currency, "
    "category). Use it to see the employee's own history when a rule depends on earlier claims.",
    {"type": "object", "properties": {"employee": {"type": "string"}}, "required": ["employee"]},
)
async def find_earlier_claims(args: dict) -> dict:
    name = str(args.get("employee", "")).strip()
    key, _ = _find_employee(name)
    if key is None:
        return _error(f"no employee named {name!r} in the directory")
    rows = [p for p in store.past_claims() if p.get("employee") == key]
    return _ok({"employee": key, "earlier_claims": rows, "count": len(rows)})


@tool(
    "convert_to_usd",
    "Convert an amount to USD with the fixed quarterly rate, rounded to the cent. Supported "
    "currencies come from fx_rates.json (USD, EUR, GBP, INR). Returns an error for other currencies.",
    {"type": "object", "properties": {"amount": {"type": "number"}, "currency": {"type": "string"}},
     "required": ["amount", "currency"]},
)
async def convert_to_usd(args: dict) -> dict:
    try:
        usd = rules.to_usd(args.get("amount"), args.get("currency"), store.fx_rates())
    except rules.RuleError as e:
        return _error(str(e))
    rate = rules.supported_rates(store.fx_rates())[str(args["currency"]).upper()]
    return _ok({"amount": args.get("amount"), "currency": str(args["currency"]).upper(),
                "rate_to_usd": str(rate), "amount_usd": str(usd)})


@tool(
    "calculate_reimbursement",
    "Add up the reimbursable amount in USD. Pass one object per part of the claim: "
    "{label, amount_usd, kind: 'reimbursable' or 'excluded', cap_usd (optional, per-line cap)}. "
    "Use 'excluded' for items the policy removes (alcohol, the per-diem-covered meal, a personal part). "
    "Use cap_usd for a per-night or per-day limit, and group_cap_usd for a limit on the whole claim "
    "(e.g. a meal at $75 x number of people). Returns the exact total to the cent.",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "object"}},
        "group_cap_usd": {"type": "number"}}, "required": ["lines"]},
)
async def calculate_reimbursement(args: dict) -> dict:
    try:
        result = rules.calculate_reimbursement(args.get("lines"), args.get("group_cap_usd"))
    except rules.RuleError as e:
        return _error(f"could not calculate: {e}")
    return _ok(result)


@tool(
    "check_policy_rules",
    "Run the code checks for one claim (EX-200 order steps 2 to 5 and 7): duplicate, late "
    "submission (over 60 days), split purchase, receipt requirement, equipment band, monthly cap "
    "and, if needed_preapproval_type is given, pre-approval validity. Pass amount_usd to test the "
    "monthly cap against a reimbursable amount you calculated; otherwise the claimed USD amount is used. "
    "The field settled_by_code shows an outcome that code alone decides, if any.",
    {"type": "object", "properties": {
        "claim_id": {"type": "string"},
        "amount_usd": {"type": "number"},
        "needed_preapproval_type": {"type": "string",
                                    "enum": ["equipment", "client_entertainment", "travel_business_class"]}},
     "required": ["claim_id"]},
)
async def check_policy_rules(args: dict) -> dict:
    try:
        claim = store.load_claim(args.get("claim_id"))
    except store.StoreError as e:
        return _error(str(e))
    try:
        amount = None
        if args.get("amount_usd") is not None:
            amount = rules.parse_amount(args["amount_usd"])
        facts = rules.deterministic_checks(
            claim, store.employees(), store.past_claims(), store.fx_rates(),
            amount_usd=amount, needed_preapproval_type=args.get("needed_preapproval_type"))
    except rules.RuleError as e:
        return _error(f"could not check the claim: {e}")
    return _ok(facts)


@tool(
    "save_decision",
    "Validate and save the decision for one claim to outbox/<claim_id>.json. Pass decision_json as a "
    "JSON string with the fields: claim_id, decision (approve|reject|needs_info|escalate), "
    "approved_amount_usd (string with 2 decimals), policy_refs (list such as [\"EX-201\"]), flags "
    "(list), reply_to_employee (text), escalate_to (manager|finance-controller|compliance|none), and "
    "escalation_reason (one line, required when escalate_to is not none). If the save is refused, the "
    "error lists what to fix; correct the decision and call this tool again. Calling it again on a "
    "follow-up updates the saved decision.",
    {"type": "object", "properties": {"decision_json": {"type": "string"}}, "required": ["decision_json"]},
)
async def save_decision(args: dict) -> dict:
    raw = args.get("decision_json")
    try:
        decision = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        return _error(f"decision_json is not valid JSON ({e}). Send one JSON object.")
    if not isinstance(decision, dict) or not decision.get("claim_id"):
        return _error("the decision must be a JSON object with a claim_id")
    try:
        claim = store.load_claim(decision["claim_id"])
    except store.StoreError as e:
        return _error(str(e))
    errors, normalised = guard.validate_decision(
        decision, claim, store.employees(), store.past_claims(), store.fx_rates())
    if errors:
        bullet = "\n".join(f"- {e}" for e in errors)
        return _error(f"Decision NOT saved. Fix these problems and call save_decision again:\n{bullet}")
    record = store.save_decision(claim["id"], normalised, session_id=None)
    return _ok(f"Saved decision version {record['version']} for {claim['id']} "
               f"({normalised['decision']}, {normalised['approved_amount_usd']} USD).")


@tool(
    "get_saved_decision",
    "Return the saved decision for a claim, with its version number. Use it on follow-up questions "
    "to see exactly what was decided and saved.",
    {"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]},
)
async def get_saved_decision(args: dict) -> dict:
    try:
        record = store.load_decision(args.get("claim_id"))
    except store.StoreError as e:
        return _error(str(e))
    if record is None:
        return _error(f"no decision has been saved for {args.get('claim_id')} yet")
    return _ok(record)


def build_server():
    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[read_policy_article, lookup_employee, find_earlier_claims, convert_to_usd,
               calculate_reimbursement, check_policy_rules, save_decision, get_saved_decision],
    )


# Tool names as the model and the allow-list see them.
EXPENSE_TOOL_NAMES = [f"mcp__{SERVER_NAME}__{n}" for n in [
    "read_policy_article", "lookup_employee", "find_earlier_claims", "convert_to_usd",
    "calculate_reimbursement", "check_policy_rules", "save_decision", "get_saved_decision"]]
