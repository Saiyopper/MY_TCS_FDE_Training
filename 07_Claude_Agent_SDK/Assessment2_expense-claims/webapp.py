"""Web front end for the expense claim reviewer. Standard library only; no extra installs.

    python webapp.py            then open http://127.0.0.1:8000

Pages: claim list with summary, submit a claim, claim detail with Review and follow-up questions.
Everything that comes from a claim (descriptions, vendors) is HTML-escaped before display.
"""
from __future__ import annotations

import asyncio
import html
import sys
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cli  # noqa: E402
from expense_app import agent, config, store  # noqa: E402

HOST = "127.0.0.1"
PORT = 8000

CSS = """
:root {
  --bg: #f1f4f9; --surface: #ffffff; --surface-2: #f7f9fc; --text: #172033; --muted: #64748b;
  --border: #e2e8f0; --accent: #4f46e5; --accent-2: #7c3aed; --shadow: 0 1px 3px rgba(15,23,42,.08),
  0 8px 24px rgba(15,23,42,.06);
  --approve: #059669; --reject: #dc2626; --needs_info: #d97706; --escalate: #7c3aed; --none: #64748b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0b1120; --surface: #131b2e; --surface-2: #18233b; --text: #e6ebf5; --muted: #93a0b8;
    --border: #243251; --shadow: 0 1px 3px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.25);
  }
}
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
       background: var(--bg); color: var(--text); line-height: 1.5; min-height: 100vh; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.top { background: linear-gradient(120deg, #1e1b4b, #4f46e5 60%, #7c3aed); color: #fff;
             padding: 18px 16px; box-shadow: var(--shadow); }
.top-inner { max-width: 1040px; margin: 0 auto; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.brand { display: flex; align-items: center; gap: 10px; color: #fff; font-weight: 700; font-size: 18px; }
.brand:hover { text-decoration: none; }
.brand-mark { width: 34px; height: 34px; border-radius: 10px; background: rgba(255,255,255,.18);
              display: grid; place-items: center; font-size: 18px; }
.tagline { margin-left: auto; font-size: 13px; opacity: .85; }
main { max-width: 1040px; margin: 24px auto 48px; padding: 0 16px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 0 0 14px; }
h3 { font-size: 15px; margin: 18px 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
        padding: 20px 22px; margin-bottom: 18px; box-shadow: var(--shadow); }
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
             margin-bottom: 14px; }
.card-head h2 { margin: 0; }
.count { font-size: 13px; color: var(--muted); background: var(--surface-2); border: 1px solid var(--border);
         border-radius: 999px; padding: 2px 10px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 18px; }
.stat { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px;
        box-shadow: var(--shadow); border-top: 3px solid var(--accent); }
.stat .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
.stat .value { font-size: 24px; font-weight: 700; margin-top: 2px; }
.stat.approve { border-top-color: var(--approve); } .stat.reject { border-top-color: var(--reject); }
.stat.needs_info { border-top-color: var(--needs_info); } .stat.escalate { border-top-color: var(--escalate); }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 11px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
thead th { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em;
           background: var(--surface-2); position: sticky; top: 0; }
tbody tr:hover { background: var(--surface-2); }
td.num { white-space: nowrap; font-variant-numeric: tabular-nums; }
.kv th { width: 150px; background: none; color: var(--muted); font-size: 13px; text-transform: none;
         letter-spacing: 0; padding: 8px 10px; position: static; }
.kv td { padding: 8px 10px; }
.badge { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px;
         font-size: 12.5px; font-weight: 600; color: #fff; background: var(--none); white-space: nowrap; }
.badge::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: rgba(255,255,255,.85); }
.badge.approve { background: var(--approve); } .badge.reject { background: var(--reject); }
.badge.needs_info { background: var(--needs_info); } .badge.escalate { background: var(--escalate); }
.badge.none { background: var(--none); }
.amount-approved { font-weight: 700; color: var(--approve); }
.grid-2 { display: grid; grid-template-columns: 1.1fr 1fr; gap: 18px; align-items: start; }
@media (max-width: 820px) { .grid-2 { grid-template-columns: 1fr; } }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 16px; }
.form-grid .full { grid-column: 1 / -1; }
@media (max-width: 640px) { .form-grid { grid-template-columns: 1fr; } }
label { display: block; font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 5px; }
input, select, textarea { width: 100%; padding: 10px 12px; font: inherit; color: var(--text);
                          background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px;
                          transition: border-color .15s, box-shadow .15s; }
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent);
                                            box-shadow: 0 0 0 3px rgba(79,70,229,.18); }
textarea { min-height: 96px; resize: vertical; }
.btn { display: inline-flex; align-items: center; gap: 8px; border: 0; border-radius: 10px; padding: 10px 18px;
       font: inherit; font-weight: 600; color: #fff; cursor: pointer; margin-top: 14px;
       background: linear-gradient(120deg, var(--accent), var(--accent-2)); box-shadow: 0 4px 12px rgba(79,70,229,.25);
       transition: transform .08s, filter .15s; }
.btn:hover { filter: brightness(1.06); }
.btn:active { transform: translateY(1px); }
.btn.secondary { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); box-shadow: none; }
.btn.muted-btn { background: #475569; }
.hint { font-size: 13px; color: var(--muted); margin-left: 10px; }
.notice, .error { border-radius: 12px; padding: 12px 16px; margin-bottom: 16px; display: flex; gap: 10px; align-items: flex-start; }
.notice { background: color-mix(in srgb, var(--approve) 12%, var(--surface)); border: 1px solid color-mix(in srgb, var(--approve) 35%, transparent); }
.error { background: color-mix(in srgb, var(--reject) 10%, var(--surface)); border: 1px solid color-mix(in srgb, var(--reject) 35%, transparent); }
.reply { background: var(--surface-2); border: 1px solid var(--border); border-left: 4px solid var(--accent);
         border-radius: 10px; padding: 14px 16px; white-space: pre-wrap; font-family: inherit; margin: 0; }
.muted { color: var(--muted); font-size: 14px; }
.empty { text-align: center; padding: 28px 12px; color: var(--muted); }
.empty .big { font-size: 34px; margin-bottom: 6px; }
.chat { display: flex; flex-direction: column; gap: 10px; margin-top: 14px; }
.bubble { max-width: 92%; padding: 10px 14px; border-radius: 14px; white-space: pre-wrap; }
.bubble.q { align-self: flex-end; background: var(--accent); color: #fff; border-bottom-right-radius: 4px; }
.bubble.a { align-self: flex-start; background: var(--surface-2); border: 1px solid var(--border); border-bottom-left-radius: 4px; }
.bubble .who { display: block; font-size: 11px; opacity: .75; margin-bottom: 2px; text-transform: uppercase; letter-spacing: .05em; }
.breadcrumb { font-size: 14px; margin-bottom: 12px; }
.claim-title { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
.claim-title h1 { margin: 0; font-variant-numeric: tabular-nums; }
.money-big { font-size: 26px; font-weight: 700; }
@media (max-width: 600px) {
  .tagline { display: none; }
  .btn { width: 100%; justify-content: center; }
  .hint { display: block; margin: 8px 0 0; }
  thead { display: none; }
  table, tbody, tr, td { display: block; width: 100%; }
  tr { border: 1px solid var(--border); border-radius: 12px; padding: 8px 10px; margin-bottom: 10px; background: var(--surface); }
  td { border: none; padding: 3px 0; }
  td[data-label]::before { content: attr(data-label); display: block; font-size: 11px; color: var(--muted);
                           text-transform: uppercase; letter-spacing: .05em; }
  .kv th { display: block; width: auto; }
  .kv td { padding: 2px 0 8px; }
}
"""

ICON_OK = "&#10003;"      # check mark
ICON_WARN = "&#9888;"     # warning sign


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(title)}</title><style>{CSS}</style></head><body>
<header class="top"><div class="top-inner">
  <a class="brand" href="/"><span class="brand-mark">&#128179;</span>Expense claim reviewer</a>
  <span class="tagline">The assistant recommends. A finance reviewer decides and pays.</span>
</div></header>
<main>{body}</main></body></html>"""


def badge(decision: str | None) -> str:
    if not decision:
        return '<span class="badge none">not reviewed</span>'
    label = decision.replace("_", " ")
    return f'<span class="badge {esc(decision)}">{esc(label)}</span>'


def employee_options(selected: str = "") -> str:
    opts = ['<option value="">Choose an employee</option>']
    for n in sorted(store.employees()):
        sel = " selected" if n == selected else ""
        opts.append(f'<option value="{esc(n)}"{sel}>{esc(n)}</option>')
    return "".join(opts)


def _summary_stats() -> str:
    counts = {"approve": 0, "needs_info": 0, "escalate": 0, "reject": 0}
    approved_total = Decimal("0")
    total = 0
    unreviewed = 0
    for cid in store.list_claim_ids():
        total += 1
        rec = store.load_decision(cid)
        if rec is None:
            unreviewed += 1
            continue
        d = rec["decision"]
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1
        approved_total += Decimal(d["approved_amount_usd"])
    tiles = [
        ("", "Claims", total),
        ("", "Not reviewed", unreviewed),
        ("approve", "Approve", counts["approve"]),
        ("needs_info", "Needs info", counts["needs_info"]),
        ("escalate", "Escalate", counts["escalate"]),
        ("reject", "Reject", counts["reject"]),
        ("approve", "Recommended USD", f"{approved_total:,.2f}"),
    ]
    return '<div class="stats">' + "".join(
        f'<div class="stat {cls}"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div></div>'
        for cls, label, value in tiles) + "</div>"


def home_page(msg: str = "", error: str = "", form: dict | None = None) -> str:
    form = form or {}
    ids = store.list_claim_ids()
    rows = []
    for cid in ids:
        try:
            c = store.load_claim(cid)
        except (store.StoreError, ValueError) as e:
            rows.append(f'<tr><td data-label="Claim">{esc(cid)}</td><td colspan="6">could not read: {esc(e)}</td></tr>')
            continue
        dec = store.load_decision(cid)
        d = dec["decision"] if dec else None
        status = badge(d["decision"]) if d else badge(None)
        approved = (f'<span class="amount-approved">{esc(d["approved_amount_usd"])}</span>' if d else
                    '<span class="muted">&mdash;</span>')
        rows.append(
            f'<tr><td data-label="Claim"><a href="/claim/{esc(cid)}"><strong>{esc(cid)}</strong></a></td>'
            f'<td data-label="Employee">{esc(c["employee"])}</td>'
            f'<td data-label="Date">{esc(c["expense_date"])}</td>'
            f'<td data-label="Amount" class="num">{esc(c["amount"])} {esc(c["currency"])}</td>'
            f'<td data-label="Vendor">{esc(c["vendor"])}</td>'
            f'<td data-label="Status">{status}</td>'
            f'<td data-label="Approved USD" class="num">{approved}</td></tr>')

    if rows:
        table = ('<div class="table-wrap"><table><thead><tr><th>Claim</th><th>Employee</th><th>Date</th>'
                 '<th>Amount</th><th>Vendor</th><th>Decision</th><th>Approved USD</th></tr></thead>'
                 '<tbody>' + "".join(rows) + '</tbody></table></div>')
    else:
        table = ('<div class="empty"><div class="big">&#128237;</div><div>The inbox is empty.</div>'
                 '<div class="muted">Submit a claim below, or run <code>python cli.py ingest-samples</code> '
                 'to load the ten sample claims.</div></div>')

    cats = "".join(f'<option value="{c}"{" selected" if form.get("category") == c else ""}>{c}</option>'
                   for c in cli.CATEGORIES)
    currencies = "".join(f'<option value="{c}">{c}</option>' for c in sorted(store.fx_rates())
                         if not c.startswith("_"))
    notice = f'<div class="notice">{ICON_OK}&nbsp; {esc(msg)}</div>' if msg else ""
    err = f'<div class="error">{ICON_WARN}&nbsp; {esc(error)}</div>' if error else ""
    body = f"""{notice}{err}
<h1>Claims</h1><p class="muted" style="margin-top:0">Every claim the assistant has seen, and what it recommends.</p>
{_summary_stats() if ids else ""}
<div class="card"><div class="card-head"><h2>Inbox</h2><span class="count">{len(ids)} claim{"s" if len(ids) != 1 else ""}</span></div>
{table}</div>

<div class="card"><div class="card-head"><h2>Submit a claim</h2><span class="muted">Amount is converted to USD at the fixed quarterly rate.</span></div>
<form method="post" action="/submit" class="form-grid">
  <div><label for="employee">Employee</label><select id="employee" name="employee" required>{employee_options(form.get("employee", ""))}</select></div>
  <div><label for="expense_date">Expense date</label><input id="expense_date" name="expense_date" type="date" required value="{esc(form.get("expense_date", ""))}"></div>
  <div><label for="category">Category</label><select id="category" name="category">{cats}</select></div>
  <div><label for="vendor">Vendor</label><input id="vendor" name="vendor" required value="{esc(form.get("vendor", ""))}"></div>
  <div><label for="amount">Amount</label><input id="amount" name="amount" type="number" step="0.01" min="0.01" required value="{esc(form.get("amount", ""))}"></div>
  <div><label for="currency">Currency</label><select id="currency" name="currency">{currencies}</select></div>
  <div><label for="receipt">Receipt</label><select id="receipt" name="receipt"><option value="attached">attached</option><option value="missing">missing</option></select></div>
  <div><label for="pre_approval">Pre-approval number (optional)</label><input id="pre_approval" name="pre_approval" value="{esc(form.get("pre_approval", ""))}"></div>
  <div class="full"><label for="description">Description</label><textarea id="description" name="description" required>{esc(form.get("description", ""))}</textarea></div>
  <div class="full"><button class="btn" type="submit">Save claim</button></div>
</form></div>"""
    return page("Expense claims", body)


def claim_page(cid: str, msg: str = "", error: str = "", question: str = "", answer: str = "") -> str:
    claim = store.load_claim(cid)
    record = store.load_decision(cid)

    facts = [("Employee", claim["employee"]), ("Expense date", claim["expense_date"]),
             ("Submitted", claim["submitted_at"]), ("Category", claim["category"]),
             ("Vendor", claim["vendor"]), ("Amount", f'{claim["amount"]} {claim["currency"]}'),
             ("Receipt", claim["receipt"]), ("Pre-approval", claim.get("pre_approval") or "none"),
             ("Description", claim["description"])]
    fields = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in facts)

    if record:
        d = record["decision"]
        to = d["escalate_to"]
        reason = (f'<div class="muted">{esc(d.get("escalation_reason", ""))}</div>'
                  if to != "none" else "")
        decision_html = f"""
<div class="claim-title" style="margin-bottom:14px">{badge(d["decision"])}
  <span class="money-big">{esc(d["approved_amount_usd"])} <span class="muted" style="font-size:15px;font-weight:500">USD recommended</span></span></div>
<table class="kv">
  <tr><th>Escalate to</th><td>{esc(to)}{reason}</td></tr>
  <tr><th>Policy refs</th><td>{esc(", ".join(d["policy_refs"]))}</td></tr>
  <tr><th>Flags</th><td>{esc(", ".join(d["flags"]) or "none")}</td></tr>
  <tr><th>Saved</th><td>version {record["version"]} &middot; {esc(record["updated_at"])}</td></tr>
</table>
<h3>Draft reply to the employee</h3>
<pre class="reply">{esc(d["reply_to_employee"])}</pre>"""
        button_label = "Review again"
    else:
        decision_html = ('<div class="empty"><div class="big">&#128269;</div>'
                         '<div>Not reviewed yet.</div></div>')
        button_label = "Review this claim"

    review_btn = f"""<form method="post" action="/claim/{esc(cid)}/review">
<button class="btn" type="submit">{button_label}</button>
<span class="hint">The agent takes about a minute and shows nothing until it finishes.</span></form>"""

    ask_block = ""
    if record:
        bubbles = ""
        if question:
            bubbles += f'<div class="bubble q"><span class="who">You</span>{esc(question)}</div>'
        if answer:
            bubbles += f'<div class="bubble a"><span class="who">Assistant</span>{esc(answer)}</div>'
        ask_block = f"""<div class="card"><div class="card-head"><h2>Ask about this claim</h2>
<span class="muted">The agent remembers this claim and its saved decision.</span></div>
<form method="post" action="/claim/{esc(cid)}/ask">
<label for="question">Your question</label>
<textarea id="question" name="question" required placeholder="e.g. why was the amount reduced?"></textarea>
<button class="btn secondary" type="submit">Ask the agent</button></form>
{f'<div class="chat">{bubbles}</div>' if bubbles else ''}</div>"""

    notice = f'<div class="notice">{ICON_OK}&nbsp; {esc(msg)}</div>' if msg else ""
    err = f'<div class="error">{ICON_WARN}&nbsp; {esc(error)}</div>' if error else ""
    body = f"""<div class="breadcrumb"><a href="/">&larr; All claims</a></div>
{notice}{err}
<div class="claim-title"><h1>{esc(cid)}</h1>{badge(record["decision"]["decision"] if record else None)}</div>
<div class="grid-2">
  <div class="card"><div class="card-head"><h2>Claim</h2></div>
    <table class="kv">{fields}</table></div>
  <div class="card"><div class="card-head"><h2>Recommendation</h2></div>
    {decision_html}
    {review_btn}</div>
</div>
{ask_block}"""
    return page(f"Claim {cid}", body)


class Handler(BaseHTTPRequestHandler):
    server_version = "ExpenseReviewer/1.0"

    # ---- helpers ----
    def _send(self, status: int, body: str, location: str | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self._send(303, "", location=location)

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 200_000)).decode("utf-8", errors="replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _claim_id_from_path(self, path: str) -> str | None:
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "claim":
            return None
        try:
            return store.check_claim_id(parts[1])
        except store.StoreError:
            return None

    def log_message(self, fmt, *args):  # keep the console for agent output
        pass

    # ---- routes ----
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        msg = query.get("msg", [""])[0]
        error = query.get("error", [""])[0]
        if url.path == "/":
            return self._send(200, home_page(msg=msg, error=error))
        cid = self._claim_id_from_path(url.path)
        if cid and url.path == f"/claim/{cid}":
            try:
                return self._send(200, claim_page(cid, msg=msg, error=error))
            except store.StoreError as e:
                return self._send(404, page("Not found", f'<div class="error">{ICON_WARN}&nbsp; {esc(e)}</div>'))
        return self._send(404, page("Not found", f'<div class="error">{ICON_WARN}&nbsp; Page not found.</div>'))

    def do_POST(self):
        url = urlparse(self.path)
        form = self._form()
        if url.path == "/submit":
            try:
                claim = cli.submit_claim({k: form.get(k, "") for k in
                                          ["employee", "expense_date", "category", "vendor", "amount",
                                           "currency", "receipt", "pre_approval", "description"]})
            except (ValueError, KeyError, TypeError, store.StoreError) as e:
                return self._send(200, home_page(error=f"Not saved: {e}", form=form))
            return self._redirect(f"/claim/{claim['id']}?msg={quote('Claim saved. Review it when ready.')}")

        cid = self._claim_id_from_path(url.path)
        if cid is None:
            return self._send(404, page("Not found", f'<div class="error">{ICON_WARN}&nbsp; Page not found.</div>'))
        try:
            store.load_claim(cid)
        except store.StoreError as e:
            return self._send(404, page("Not found", f'<div class="error">{ICON_WARN}&nbsp; {esc(e)}</div>'))

        if url.path.endswith("/review"):
            try:
                summary = asyncio.run(agent.review_claim(cid, show_tools=False))
            except Exception as e:  # show the problem on the page rather than a blank error
                return self._redirect(f"/claim/{cid}?error={quote(f'Review failed: {type(e).__name__}: {e}')}")
            if store.load_decision(cid) is None:
                return self._redirect(f"/claim/{cid}?error={quote('The agent did not save a decision. Try again.')}")
            cost = summary["cost_usd"] or 0
            return self._redirect(f"/claim/{cid}?msg={quote(f'Review finished. Cost ${cost:.3f}.')}")

        if url.path.endswith("/ask"):
            question = form.get("question", "").strip()
            if not question:
                return self._redirect(f"/claim/{cid}?error={quote('Type a question first.')}")
            try:
                summary = asyncio.run(agent.follow_up(cid, question, show_tools=False))
            except Exception as e:
                return self._send(200, claim_page(cid, question=question,
                                                  error=f"Follow-up failed: {type(e).__name__}: {e}"))
            return self._send(200, claim_page(cid, question=question,
                                              answer=summary["text"] or "(no answer text)"))

        return self._send(404, page("Not found", f'<div class="error">{ICON_WARN}&nbsp; Page not found.</div>'))


def main() -> None:
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Expense claim reviewer running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
