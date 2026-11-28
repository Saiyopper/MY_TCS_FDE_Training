# Expense Claim Review Assistant

An agent that reviews expense claims against `data/expense_policy.md` and recommends a decision and a
draft reply. A finance reviewer makes the final decision and pays. The assistant never pays and never
contacts the employee.

Model: `claude-sonnet-5-5` by default (set `EXPENSE_MODEL` to change it). Python 3.10+ with
`claude-agent-sdk`.

## 1. Install

```bash
python -m pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env
# edit .env and set ANTHROPIC_API_KEY (never commit .env)
```

Run the offline tests (no API key needed):

```bash
python -m unittest discover -s tests -v
```

## 2. Run

```bash
python cli.py ingest-samples          # copy data/claims.json (10 claims) into inbox/
python cli.py list                    # what is in the inbox and its status
python cli.py review CLM-3001         # agent reviews one claim; tool calls are printed as they happen
python cli.py show CLM-3001           # saved decision, reply and flags
python cli.py ask CLM-3001 "why was the amount reduced?"   # one follow-up
python cli.py chat CLM-3001           # interactive follow-ups (type exit to stop)
python cli.py batch                   # review every claim that has no decision yet
python cli.py export                  # write results/ (decision JSON, reply text, SUMMARY.md)
python cli.py submit --review         # enter a new claim and review it at once
```

Files are written to `inbox/` (claims), `outbox/` (decisions, with version history in
`outbox/history/`), `sessions/` (claim id to Claude session id), `logs/audit.jsonl` (every tool call
and whether the hook allowed it) and `logs/cost.jsonl` (turns and cost per run).

## 3. Architecture

```
            +-----------------------------+
 you -----> | cli.py                      |  submit / list / review / ask / chat / batch / export
            +------+---------------+------+
                   |               |
        intake: write one file     | agent run (claude-agent-sdk query, resume for follow-ups)
                   v               v
          inbox/CLM-xxxx.json   +--------------------------------------------------+
                   ^            | Agent  (system prompt: check order, reply rules) |
                   |            |   tools allow-list: only mcp__expense__* and     |
                   |            |   mcp__inbox__*; built-in tools disallowed       |
                   |            |   PreToolUse / PostToolUse hook -> logs/audit    |
                   |            |   max_turns, max_budget_usd per run              |
                   |            +------+----------------------------+--------------+
                   |                   |                            |
                   |   in-process MCP "expense" (SDK)     external MCP "inbox" (stdio process)
                   |   read_policy_article, lookup_employee,   list_claims, read_claim
                   |   find_earlier_claims, convert_to_usd,        (reads inbox/, marks claim
                   |   calculate_reimbursement,                     text as UNTRUSTED)
                   |   check_policy_rules, save_decision,
                   |   get_saved_decision
                   |                   |
                   +-------- rules.py (Decimal math, duplicate, split, late, caps)
                                       guard.py (schema, amounts, order of checks,
                                                 reply hard stops) -> outbox/CLM-xxxx.json
```

Division of labour:

- **Code** (`rules.py`, `guard.py`): money conversion, rounding, caps, duplicate, late and split
  detection, pre-approval validity, and the hard stops. The guard re-checks every decision before it
  is saved, so the model cannot save a decision that breaks a rule code can check.
- **Model** (the agent): reads the claim and policy, decides judgement calls (is this item
  prohibited? how many attendees? how many per-diem days?), chooses which tools to call and in what
  order, and writes the reply.

### How sessions survive a restart (R3)

The first review starts a Claude Agent SDK session. Its id is stored in `sessions/<claim>.json`. A
follow-up calls `query(..., resume=<id>)`, so the agent gets back the full conversation for that claim.
The Claude Code transcript is on disk, so this still works after the app is closed and reopened. If
the transcript has gone, the follow-up starts a new session with the saved decision in the prompt, and
the console says so.

### Guardrails (R5)

- **Allow-list:** `allowed_tools` lists only the 8 expense tools and 2 inbox tools. `disallowed_tools`
  blocks Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, Task and others. The PreToolUse hook
  also denies anything not on the list.
- **Hook:** every tool call is written to `logs/audit.jsonl` (pre and post). Denied calls are logged.
- **Bounded loop:** `EXPENSE_MAX_TURNS` (default 25) and `EXPENSE_MAX_BUDGET_USD` (default 0.50) per run.
- **Hostile text:** the inbox server returns claim text under an "UNTRUSTED CLAIM DATA" header. The
  system prompt says to ignore instructions inside claims and to add the flag `injection_attempt`. The
  decision still follows the policy, because the guard checks the rules whatever the text says.
- **Input errors:** every tool returns a readable `is_error` message the agent can act on (unknown
  employee, unsupported currency, bad JSON, unknown claim id, invalid article id).

### What the guard refuses (R6)

- amount above the claimed USD amount; a non-zero amount on anything but `approve`
- a decision that disagrees with the first settling check code can make (duplicate, late, split)
- approval with a missing receipt (items $25 or more; per-diem claims exempt)
- equipment of $150 or more without a valid pre-approval on the employee's record; equipment over $1,000
- approval that takes the employee over their monthly cap
- replies that say or imply payment, accuse anyone, name another person, cite another person's claim or
  pre-approval, mention prompts or instructions, or are not signed "Finance Operations"

## 4. Design decisions

1. **Money is `Decimal`, never `float`.** Conversion and each cap is rounded half-up to the cent, in code.
2. **Ordinary code for the rules we can check.** Duplicate, late, split, receipt, pre-approval and cap
   checks are in Python and also run inside the save guard. A prompt-only version was not trusted to
   respect the order of checks.
3. **The model still makes the judgement calls.** Prohibited-item classification, attendee counts and
   per-diem days come from reading the description, so they stay with the model. Code checks the result.
4. **Recommend only.** Nothing is paid, and no message is sent. Outputs go to `outbox/`.
5. **One file per claim, one file per decision.** Atomic writes with a version number; each earlier
   version is kept in `outbox/history/`.
6. **Two MCP servers, one of them external.** The in-process server holds the policy tools. The
   stdio server reads the inbox, so the agent gets claims through a tool, not in the prompt.
7. **Unknown employee → needs_info.** The policy does not cover this case. The reply asks the employee
   to confirm their name. The guard enforces it.
8. **Unsupported currency → refused at intake.** The review path also returns a readable error.

## 5. Interpretations of the policy (state these to a reviewer)

- **Per diem days (EX-202).** A "full day away overnight" is counted as one day per night away. The
  return day is not counted. CLM-3010 (15 to 19 Feb) is therefore 4 days, $240.00. If the policy means
  calendar days, the answer changes to $300.00.
- **Meal attendees (EX-201).** "Lunch with two people from X" is read as the employee plus two guests (3
  attendees), so the cap is $225.
- **`escalation_reason`.** The seven required fields are kept as written. The one-line reason for an
  escalation is a separate field, `escalation_reason`, and the guard requires it when `escalate_to` is
  not `none`.
- **Flags.** `duplicate` is used for a repeated claim. `double_claim` is used for a meal claimed on top
  of a per diem.
- **Split purchase.** Counted over the claim being reviewed plus earlier claims with the same employee,
  vendor and date, where every claim is under $150. A valid equipment pre-approval whose max covers the
  total stops it being treated as a split.
- **Monthly cap.** Tested against month-to-date plus the reimbursable amount, using the cap in
  `employees.json` for the grade.

## 6. Models, cost and limits

- Model: `claude-sonnet-5-5` (set `EXPENSE_MODEL` to try a cheaper model).
- Measured cost: the full batch of 10 sample claims cost $0.50 in total, about $0.03 to $0.09 per claim
  (5 to 13 turns). Each run is recorded in `logs/cost.jsonl`. A follow-up costs about $0.07.
- Results for the 10 sample claims are in `results/` (`SUMMARY.md`, `<id>.decision.json`, `<id>.reply.txt`).
- Limits: 25 turns and $0.50 per run by default.

## 7. Not in this submission

- The stretch goals (web UI, approval gate) are not implemented.
- `reflection.md` is for the submitter to write from their own review of the results.
