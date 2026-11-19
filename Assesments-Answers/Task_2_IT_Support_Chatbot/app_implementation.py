"""IT Helpdesk Assistant - core implementation (no UI code here).
"""

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic
import chromadb
import voyageai
from dotenv import find_dotenv, load_dotenv
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE_DIR = Path(__file__).parent

# .env next to this file first, then any .env found from the working directory up.
load_dotenv(BASE_DIR / ".env")
load_dotenv(find_dotenv(usecwd=True))
FAQ_FILE = BASE_DIR / "data" / "it_sector.txt"
CHROMA_DIR = BASE_DIR / "data" / "chroma_db"
COLLECTION_NAME = "it_support_faq"

EMBEDDING_MODEL = "voyage-4-lite"
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:3000/mcp")

MODELS = {
    "Claude Opus 5.5": "claude-opus-5-5",
    "Claude Sonnet 5.5": "claude-sonnet-5-5",
}
DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_LOOP_STEPS = 10

SYSTEM_PROMPT = """You are the IT Helpdesk Assistant for our company's employees.

How to answer IT questions - the knowledge base comes first:
- Each user message includes <knowledge_base> sections retrieved from the company IT \
support FAQ (it_sector.txt). This is your primary and authoritative source.
- EVERY IT-related question must be answered from the knowledge base: laptops, desktops, \
chargers and batteries, screens, keyboards, printers and other hardware, software, \
network, email, security, backups, VPN, phones and tablets. Do NOT call web_search for \
IT questions.
- Each FAQ section describes a general approach that applies beyond its example question. \
For example, "Hardware Issues Diagnosis" covers ANY hardware problem (laptop not \
charging, battery, screen, keyboard, mouse, monitor, docking station), not only \
printers. Pick the closest section(s) and apply their steps to the employee's specific \
device and problem, but do not add steps that are not in the FAQ.
- If the FAQ steps may not fully solve it, offer to raise a support ticket.
- Use web_search only for questions that are NOT about IT support.
- End EVERY answer with exactly one final line naming where it came from, in one of \
these forms (the app turns this line into a label under the answer):
  Source: FAQ - <section title>[; <another section title>]
  Source: Web search
  Source: None
- Use get_current_datetime whenever the answer depends on the current date or time.
- Give troubleshooting as short numbered steps. Keep answers concise and practical.

Support tickets (via the IT Helpdesk MCP Server):
- Use create_support_ticket when the employee asks to raise a ticket, or offer one when \
troubleshooting hasn't fixed the issue. You need the employee's name, a short issue title, \
a description, and a priority (Low, Medium, High, Critical). Ask for the name if you \
don't have it; infer the rest from the conversation when you reasonably can.
- Use check_ticket_status when the employee asks about an existing ticket ID (e.g. IT-102).
- After a ticket tool runs, report the ticket ID, issue, priority and status clearly.

Questions outside IT support (e.g. product recommendations, news, general knowledge):
- Do not refuse or redirect them. Use web_search to find current, reliable information \
and answer helpfully and concisely, ending with "Source: Web search"."""


# --------------------------------------------------------------------------------------
# 1. RAG: Voyage AI embeddings + ChromaDB
# --------------------------------------------------------------------------------------
def chunk_by_section(text: str) -> list[str]:
    """Split the FAQ on '## ' headings, so each chunk is one Q&A topic."""
    sections = re.split(r"\n(?=## )", text.strip())
    return [s.strip() for s in sections if s.strip()]


class KnowledgeBase:
    """Vector store for the IT FAQ. Embeds with Voyage AI, stores in ChromaDB on disk.

    The collection is rebuilt only when it_sector.txt changes (tracked by a hash),
    so restarting the app doesn't re-embed the documents.
    """

    def __init__(self, faq_file: Path = FAQ_FILE, db_dir: Path = CHROMA_DIR):
        # reads VOYAGE_API_KEY; retries with backoff on rate limits (free tier = 3 RPM)
        self.voyage = voyageai.Client(max_retries=3)
        self.last_error: str | None = None
        self.db = chromadb.PersistentClient(path=str(db_dir))
        self.faq_file = faq_file
        self.collection = self.ingest()

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        result = self.voyage.embed(texts, model=EMBEDDING_MODEL, input_type=input_type)
        return result.embeddings

    def ingest(self):
        text = self.faq_file.read_text(encoding="utf-8")
        source_hash = hashlib.sha256(text.encode()).hexdigest()
        meta = {"hnsw:space": "cosine", "source_hash": source_hash}

        # Reuse the stored collection if it was built from this exact file.
        # embedding_function=None: we always pass Voyage embeddings ourselves.
        try:
            existing = self.db.get_collection(COLLECTION_NAME, embedding_function=None)
            if (existing.metadata or {}).get("source_hash") == source_hash and existing.count():
                return existing
            self.db.delete_collection(COLLECTION_NAME)  # FAQ changed: rebuild
        except Exception:
            pass  # collection doesn't exist yet

        collection = self.db.create_collection(
            COLLECTION_NAME, embedding_function=None, metadata=meta
        )
        chunks = chunk_by_section(text)
        collection.add(
            ids=[f"faq-{i}" for i in range(len(chunks))],
            documents=chunks,
            embeddings=self.embed(chunks, input_type="document"),
            metadatas=[{"title": c.splitlines()[0].lstrip("# ").strip()} for c in chunks],
        )
        return collection

    def search(self, query: str, k: int = 2) -> list[dict]:
        """Return the k most relevant FAQ sections with their cosine distance.

        If embedding fails (e.g. Voyage rate limit), return ALL FAQ sections instead
        (the FAQ is small), so answers still come from the IT FAQs.
        """
        try:
            query_vec = self.embed([query], input_type="query")[0]
            self.last_error = None
        except voyageai.error.VoyageError as e:
            self.last_error = (
                f"Voyage search unavailable ({type(e).__name__}) - "
                "using all IT FAQ sections instead."
            )
            stored = self.collection.get(include=["documents", "metadatas"])
            return [
                {"title": meta["title"], "content": doc, "distance": None}
                for doc, meta in zip(stored["documents"], stored["metadatas"])
            ]
        res = self.collection.query(query_embeddings=[query_vec], n_results=k)
        return [
            {"title": meta["title"], "content": doc, "distance": round(dist, 4)}
            for doc, meta, dist in zip(
                res["documents"][0], res["metadatas"][0], res["distances"][0]
            )
        ]


def build_user_content(question: str, hits: list[dict]) -> str:
    """Put the retrieved FAQ sections in front of the employee's question."""
    if not hits:
        return question
    context = "\n\n---\n\n".join(
        f'<section title="{h["title"]}">\n{h["content"]}\n</section>' for h in hits
    )
    return (
        f"<knowledge_base>\n{context}\n</knowledge_base>\n\n"
        f"Employee question: {question}"
    )


# --------------------------------------------------------------------------------------
# 2. Tools: web_search (server) + get_current_datetime (client)
# --------------------------------------------------------------------------------------
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 3,
}

DATETIME_TOOL = {
    "name": "get_current_datetime",
    "description": (
        "Returns the current local date and time. Use whenever the answer depends on "
        "today's date or the current time."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date_format": {
                "type": "string",
                "description": "Python strftime format, e.g. '%Y-%m-%d %H:%M:%S' or '%A, %d %B %Y'.",
            }
        },
        "required": [],
    },
}


def get_current_datetime(date_format: str = "%Y-%m-%d %H:%M:%S") -> str:
    if not date_format:
        raise ValueError("date_format cannot be empty")
    return datetime.now().strftime(date_format)


# --------------------------------------------------------------------------------------
# 3. MCP client: talk to the IT Helpdesk MCP Server
# --------------------------------------------------------------------------------------
async def _mcp_call(fn, url: str = MCP_SERVER_URL):
    """Open a fresh MCP connection, run fn(session), then close it."""
    async with streamable_http_client(url) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await fn(session)


def list_mcp_tools() -> list[dict]:
    """Fetch the MCP server's tools and convert them to Claude tool definitions."""

    async def _list(session):
        return await session.list_tools()

    result = asyncio.run(_mcp_call(_list))
    return [
        {
            "name": tool.name,
            "description": tool.description or tool.name,
            "input_schema": tool.input_schema,
        }
        for tool in result.tools
    ]


def call_mcp_tool(name: str, arguments: dict) -> tuple[str, bool]:
    """Run one MCP tool. Returns (text_output, is_error)."""

    async def _call(session):
        return await session.call_tool(name, arguments)

    result = asyncio.run(_mcp_call(_call))
    text = "\n".join(block.text for block in result.content if block.type == "text")
    return text, bool(getattr(result, "is_error", False))


# --------------------------------------------------------------------------------------
# 4. The assistant: agent loop over Claude + tools
# --------------------------------------------------------------------------------------
@dataclass
class TurnResult:
    """What one user turn produced, for the UI to display."""

    answer: str
    rag_hits: list[dict] = field(default_factory=list)
    tool_events: list[dict] = field(default_factory=list)  # {"kind", "name", "input", "output", ...}
    model: str = ""
    usage: dict = field(default_factory=dict)
    refused: bool = False
    source: str = ""  # label shown under the answer, e.g. "📚 Referring to IT FAQs ..."


FAQ_LABEL = "📚 Referring to IT FAQs (it_sector.txt)"
SOURCE_LINE = re.compile(r"(?:^|\n)[ \t*_]*Source:[ \t]*(.*?)[ \t*_]*$", re.IGNORECASE)


def split_source(answer: str, result: TurnResult) -> tuple[str, str]:
    """Remove Claude's trailing 'Source: ...' line and build the label for the UI."""
    cited = ""
    match = SOURCE_LINE.search(answer)
    if match:
        cited = match.group(1).strip()
        answer = answer[: match.start()].rstrip() or answer

    used_web = any(ev["name"] == "web_search" for ev in result.tool_events)
    faq_titles = re.sub(r"^(FAQ|IT Knowledge Base)\s*[-:]\s*", "", cited, flags=re.I)

    if cited.lower().startswith(("faq", "it knowledge base")) and faq_titles:
        label = f"{FAQ_LABEL} → {faq_titles}"
    elif used_web or cited.lower().startswith("web"):
        label = "🌐 Not covered by the IT FAQs - answer from web search"
    elif result.rag_hits:
        label = f"{FAQ_LABEL} - no matching FAQ section for this question"
    else:
        label = "⚠️ IT FAQs unavailable for this question"
    return answer, label


class HelpdeskAssistant:
    def __init__(self):
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.kb = KnowledgeBase()
        self.mcp_tools: list[dict] = []
        self.mcp_error: str | None = None
        self.refresh_mcp_tools()

    def refresh_mcp_tools(self) -> None:
        """(Re)connect to the MCP server. The chat still works without it."""
        try:
            self.mcp_tools = list_mcp_tools()
            self.mcp_error = None
        except Exception as e:  # server not running, wrong URL, ...
            self.mcp_tools = []
            self.mcp_error = f"Could not reach MCP server at {MCP_SERVER_URL}: {e}"

    @property
    def tools(self) -> list[dict]:
        return [WEB_SEARCH_TOOL, DATETIME_TOOL, *self.mcp_tools]

    def _run_client_tool(self, block) -> dict:
        """Execute one tool_use block locally / via MCP and build its tool_result."""
        mcp_names = {t["name"] for t in self.mcp_tools}
        try:
            if block.name == "get_current_datetime":
                output, is_error = get_current_datetime(**block.input), False
            elif block.name in mcp_names:
                output, is_error = call_mcp_tool(block.name, block.input)
            else:
                output, is_error = f"Unknown tool: {block.name}", True
        except Exception as e:
            output, is_error = f"Error: {e}", True

        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
            "is_error": is_error,
        }

    def chat(self, question: str, history: list, model: str = DEFAULT_MODEL,
             effort: str = "medium") -> TurnResult:
        """Answer one employee question.

        `history` is the full API transcript (list of message dicts). It is extended
        in place with this turn's messages, unchanged, so the next turn replays it.
        """
        hits = self.kb.search(question, k=4)
        start = len(history)
        history.append({"role": "user", "content": build_user_content(question, hits)})
        try:
            return self._run_loop(history, start, TurnResult(answer="", rag_hits=hits),
                                  model, effort)
        except Exception:
            del history[start:]  # don't leave a half-finished turn in the transcript
            raise

    def _run_loop(self, history: list, start: int, result: TurnResult,
                  model: str, effort: str) -> TurnResult:
        response = None
        for _ in range(MAX_LOOP_STEPS):
            response = self.client.beta.messages.create(
                model=model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=history,
                tools=self.tools,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},
                # If a safety classifier declines, retry on a suitable fallback model.
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )

            if response.stop_reason == "refusal":
                del history[start:]  # drop the declined exchange; keep history valid
                result.refused = True
                result.answer = "_Sorry, I can't help with that request._"
                return result

            history.append({"role": "assistant", "content": response.content})
            self._record_server_tool_events(response.content, result)

            if response.stop_reason == "pause_turn":
                continue  # server-side web search loop paused; resend to resume

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    tr = self._run_client_tool(block)
                    tool_results.append(tr)
                    result.tool_events.append({
                        "kind": "mcp" if block.name != "get_current_datetime" else "client",
                        "name": block.name,
                        "input": block.input,
                        "output": tr["content"],
                        "is_error": tr["is_error"],
                    })
                # All results for this step go back in ONE user message.
                history.append({"role": "user", "content": tool_results})
                continue

            break  # end_turn / max_tokens

        # Web-search answers arrive as several cited text blocks that form one
        # continuous answer, so join them without separators.
        result.answer = "".join(
            b.text for b in response.content if b.type == "text"
        ).strip() or "_(No answer returned.)_"
        result.answer, result.source = split_source(result.answer, result)
        if response.stop_reason == "max_tokens":
            result.answer += "\n\n_(Response truncated.)_"
        elif response.stop_reason in ("tool_use", "pause_turn"):
            result.answer += "\n\n_(Stopped after too many tool steps.)_"

        result.model = response.model
        u = response.usage
        result.usage = {
            # total prompt size = uncached + cache writes + cache reads
            "input_tokens": u.input_tokens + (u.cache_creation_input_tokens or 0)
            + (u.cache_read_input_tokens or 0),
            "cached_tokens": u.cache_read_input_tokens or 0,
            "output_tokens": u.output_tokens,
        }
        return result

    @staticmethod
    def _record_server_tool_events(content, result: TurnResult) -> None:
        """Pull web_search queries and result links out of the response for display."""
        queries = {}
        for block in content:
            if block.type == "server_tool_use" and block.name == "web_search":
                queries[block.id] = (block.input or {}).get("query", "")
            elif block.type == "web_search_tool_result":
                if isinstance(block.content, list):
                    sources = [{"title": r.title, "url": r.url} for r in block.content]
                    output, is_error = sources, False
                else:  # error object, e.g. max_uses_exceeded
                    output, is_error = getattr(block.content, "error_code", "error"), True
                result.tool_events.append({
                    "kind": "server",
                    "name": "web_search",
                    "input": {"query": queries.get(block.tool_use_id, "")},
                    "output": output,
                    "is_error": is_error,
                })


def parse_ticket(output: str) -> dict | None:
    """Ticket tools return JSON; parse it for a nicer display."""
    try:
        data = json.loads(output)
        return data if isinstance(data, dict) and "ticket_id" in data else None
    except (json.JSONDecodeError, TypeError):
        return None
