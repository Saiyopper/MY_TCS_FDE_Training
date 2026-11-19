#IT Helpdesk MCP Server.

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

DATA_FILE = Path(__file__).parent / "data" / "mcp_tool_data.json"
VALID_PRIORITIES = {"Low", "Medium", "High", "Critical"}

# In-memory ticket store: ticket_id -> ticket dict
TICKETS: dict[str, dict] = {}


def load_seed_tickets() -> None:
    """Load the sample tickets from mcp_tool_data.json into the dictionary."""
    with open(DATA_FILE, encoding="utf-8") as f:
        for ticket in json.load(f):
            TICKETS[ticket["ticket_id"].upper()] = ticket


def next_ticket_id() -> str:
    """IT-101, IT-102, ... -> next free number."""
    numbers = [int(tid.split("-")[1]) for tid in TICKETS if tid.split("-")[-1].isdigit()]
    return f"IT-{max(numbers, default=100) + 1}"


load_seed_tickets()

mcp_server = MCPServer(name="IT Helpdesk MCP Server", version="1.0.0")


@mcp_server.tool(
    name="create_support_ticket",
    description=(
        "Create a new IT support ticket for an employee. Use when the employee asks to "
        "raise/log a ticket, or when their issue could not be resolved through troubleshooting. "
        "Returns the created ticket, including its new ticket_id."
    ),
)
def create_support_ticket(
    employee: str,
    issue: str,
    description: str,
    priority: str = "Medium",
) -> str:
    """
    Args:
        employee: Name of the employee reporting the issue.
        issue: Short title of the problem, e.g. "VPN not connecting".
        description: Details of the problem and anything already tried.
        priority: One of Low, Medium, High, Critical.
    """
    if not employee.strip() or not issue.strip():
        raise ToolError("employee and issue are required")

    priority = priority.strip().capitalize()
    if priority not in VALID_PRIORITIES:
        raise ToolError(f"priority must be one of {sorted(VALID_PRIORITIES)}")

    ticket = {
        "ticket_id": next_ticket_id(),
        "employee": employee.strip(),
        "issue": issue.strip(),
        "description": description.strip(),
        "priority": priority,
        "status": "Open",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    TICKETS[ticket["ticket_id"]] = ticket
    return json.dumps(ticket)


@mcp_server.tool(
    name="check_ticket_status",
    description=(
        "Look up an existing IT support ticket by its ID (format IT-<number>, e.g. IT-102) "
        "and return its details and current status."
    ),
)
def check_ticket_status(ticket_id: str) -> str:
    """
    Args:
        ticket_id: The ticket ID, e.g. "IT-102".
    """
    ticket = TICKETS.get(ticket_id.strip().upper())
    if ticket is None:
        raise ToolError(f"No ticket found with ID '{ticket_id}'")
    return json.dumps(ticket)


mcp_app = mcp_server.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # FastAPI doesn't run a mounted sub-app's lifespan, so start the MCP
    # session manager explicitly.
    async with mcp_app.router.lifespan_context(mcp_app):
        yield


app = FastAPI(lifespan=lifespan)
app.mount("/", mcp_app)


if __name__ == "__main__":
    import asyncio
    import sys

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=3000)
    server = uvicorn.Server(config)

    # SelectorEventLoop avoids noisy ConnectionResetError logs on Windows when
    # short-lived MCP client connections close.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None

    print(f"IT Helpdesk MCP Server running at http://localhost:3000/mcp "
          f"({len(TICKETS)} tickets loaded)")
    asyncio.run(server.serve(), loop_factory=loop_factory)
