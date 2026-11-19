# Task 2 – IT Support Chat Bot (AI-Powered IT Helpdesk Assistant)

A Streamlit chatbot that answers employee IT questions using **RAG**, **tool calling** and an **MCP server**.

| Requirement | Implementation |
|---|---|
| UI | `streamlit.py`: chat UI, ticket cards, tool-call and knowledge-base source panels |
| RAG | `data/it_sector.txt` split by FAQ section → **Voyage AI** embeddings (`voyage-4-lite`) → **ChromaDB** (persistent, `data/chroma_db/`) |
| Server tool | `web_search`: Anthropic-hosted web search (`web_search_20260209`) |
| Client tool | `get_current_datetime`: runs locally in `app_implementation.py` |
| MCP server | `mcp_server.py`: **IT Helpdesk MCP Server** with `create_support_ticket` and `check_ticket_status`; tickets in an in-memory dict seeded from `data/mcp_tool_data.json` |
| Agent loop | `app_implementation.py`: Claude decides which tools to call; client/MCP tools are run and results sent back until the final answer |

## Files
```
requirements.txt        dependencies
streamlit.py            Streamlit UI
app_implementation.py   RAG + tools + MCP client + Claude agent loop
mcp_server.py           IT Helpdesk MCP Server (streamable HTTP, port 3000)
data/it_sector.txt      IT FAQ used for RAG
data/mcp_tool_data.json sample tickets (IT-101 .. IT-103)
```

## Setup
Python **3.10–3.14** (the `voyageai` SDK doesn't support 3.15 yet).

```bash
py -3.14 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then fill in ANTHROPIC_API_KEY and VOYAGE_API_KEY
```

## Run
Terminal 1 – MCP server:
```bash
python mcp_server.py        # http://localhost:3000/mcp
```
Terminal 2 – chatbot:
```bash
streamlit run streamlit.py
```

## Try
- *My laptop is running slow, what should I do?* → answered from the FAQ (RAG)
- *What's today's date?* → `get_current_datetime`
- *How do I fix Microsoft Teams error caa20002?* → `web_search`
- *I'm Rahul, raise a high priority ticket: Outlook crashes on startup* → `create_support_ticket` (MCP)
- *What's the status of IT-102?* → `check_ticket_status` (MCP)

## Notes
- Model: Claude Opus 5.5 (Sonnet 5.5 selectable), adaptive thinking, server-side refusal fallback enabled.
- The Voyage free tier (no payment method) allows 3 requests/min; the app retries, and if search still fails it answers without the knowledge base and shows a warning.
- Tickets created at runtime are lost when the MCP server restarts (in-memory store, as required).
