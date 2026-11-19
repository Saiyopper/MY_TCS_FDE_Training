#IT Support Chat Bot - Streamlit UI.

import anthropic
import streamlit as st

from app_implementation import (
    MCP_SERVER_URL,
    MODELS,
    HelpdeskAssistant,
    call_mcp_tool,
    parse_ticket,
)

st.set_page_config(page_title="Welcome to TCS IT Helpdesk", page_icon="💻")


@st.cache_resource(show_spinner="Loading IT knowledge base...")
def get_assistant() -> HelpdeskAssistant:
    return HelpdeskAssistant()


def render_ticket(ticket: dict) -> None:
    st.markdown(f"**🎫 {ticket['ticket_id']} — {ticket['issue']}**")
    cols = st.columns(3)
    cols[0].metric("Status", ticket.get("status", "-"))
    cols[1].metric("Priority", ticket.get("priority", "-"))
    cols[2].metric("Employee", ticket.get("employee", "-"))
    if ticket.get("description"):
        st.caption(ticket["description"])


def show_mcp_result(tool_name: str, arguments: dict) -> None:
    """Call an MCP tool directly and show the ticket (or the error) in the dialog."""
    try:
        output, is_error = call_mcp_tool(tool_name, arguments)
    except Exception as e:
        st.error(f"Could not reach the MCP server at {MCP_SERVER_URL}: {e}")
        return
    ticket = None if is_error else parse_ticket(output)
    if ticket:
        st.success("Done")
        with st.container(border=True):
            render_ticket(ticket)
    else:
        st.error(output or "The MCP server returned no result.")


@st.dialog("🎫 Create support ticket")
def create_ticket_dialog() -> None:
    with st.form("create_ticket"):
        employee = st.text_input("Employee name")
        issue = st.text_input("Issue", placeholder="e.g. VPN not connecting")
        description = st.text_area("Description", placeholder="What happened, what you tried")
        priority = st.selectbox("Priority", ["Low", "Medium", "High", "Critical"], index=1)
        submitted = st.form_submit_button("Create ticket", type="primary")
    if submitted:
        if not employee.strip() or not issue.strip():
            st.warning("Employee name and issue are required.")
        else:
            show_mcp_result("create_support_ticket", {
                "employee": employee, "issue": issue,
                "description": description, "priority": priority,
            })


@st.dialog("🔍 Check ticket status")
def check_status_dialog() -> None:
    with st.form("check_status"):
        ticket_id = st.text_input("Ticket ID", placeholder="e.g. IT-102")
        submitted = st.form_submit_button("Check status", type="primary")
    if submitted:
        if not ticket_id.strip():
            st.warning("Enter a ticket ID.")
        else:
            show_mcp_result("check_ticket_status", {"ticket_id": ticket_id})


def render_details(turn: dict) -> None:
    """Show tickets, tool calls and RAG sources under an assistant answer."""
    if turn.get("source"):
        st.info(turn["source"])

    for ev in turn.get("tool_events", []):
        ticket = parse_ticket(ev["output"]) if isinstance(ev["output"], str) else None
        if ticket:
            with st.container(border=True):
                render_ticket(ticket)

    if turn.get("tool_events"):
        with st.expander(f"🔧 Tools used ({len(turn['tool_events'])})"):
            for ev in turn["tool_events"]:
                label = {"server": "Server tool", "client": "Client tool", "mcp": "MCP tool"}[ev["kind"]]
                icon = "❌" if ev["is_error"] else "✅"
                st.markdown(f"{icon} **{ev['name']}** · _{label}_")
                st.json(ev["input"], expanded=False)
                if ev["kind"] == "server" and isinstance(ev["output"], list):
                    for src in ev["output"]:
                        st.markdown(f"- [{src['title']}]({src['url']})")
                else:
                    st.code(str(ev["output"]), language="json")

    if turn.get("rag_hits"):
        with st.expander("📚 Knowledge base sources"):
            for hit in turn["rag_hits"]:
                dist = hit["distance"]
                st.markdown(f"**{hit['title']}**" + (f" · distance {dist}" if dist is not None else ""))
                st.caption(hit["content"])

    if turn.get("meta"):
        st.caption(turn["meta"])


# ---------------- Sidebar ----------------
assistant = get_assistant()

with st.sidebar:
    st.header("⚙️ Settings")
    model_name = st.selectbox("Model", list(MODELS))
    effort = st.select_slider("Effort", ["low", "medium", "high"], value="medium")

    st.divider()
    st.subheader("MCP server")
    if assistant.mcp_error:
        st.error("Not connected. Start it with `python mcp_server.py`.")
        st.caption(assistant.mcp_error)
    else:
        st.success(f"Connected · {MCP_SERVER_URL}")
        tool_names = {t["name"] for t in assistant.mcp_tools}
        if "create_support_ticket" in tool_names and st.button(
            "🎫 create_support_ticket", use_container_width=True
        ):
            create_ticket_dialog()
        if "check_ticket_status" in tool_names and st.button(
            "🔍 check_ticket_status", use_container_width=True
        ):
            check_status_dialog()
        st.caption("Click a tool to call it directly, or just ask in the chat.")
    if st.button("Reconnect MCP", use_container_width=True):
        assistant.refresh_mcp_tools()
        st.rerun()

    st.divider()
    st.markdown("**Try asking**")
    st.caption(
        "• My laptop is running slow, what should I do?\n\n"
        "• What's today's date?\n\n"
        "• Latest fix for Outlook stuck on 'Updating inbox'?\n\n"
        "• What's the status of ticket IT-102?\n\n"
        "• Raise a ticket: my VPN keeps dropping (I'm Rahul)."
    )

    if st.button("Clear chat", use_container_width=True):
        st.session_state.turns = []
        st.session_state.api_history = []
        st.rerun()


# ---------------- Chat ----------------
st.title("💻 Welcome to TCS IT Helpdesk")
st.caption("Answers from the IT knowledge base, web search for anything new, and support tickets via MCP.")

if "turns" not in st.session_state:
    st.session_state.turns = []        # what we display
    st.session_state.api_history = []  # full Claude transcript, replayed each turn

for turn in st.session_state.turns:
    with st.chat_message(turn["role"]):
        st.markdown(turn["text"])
        if turn["role"] == "assistant":
            render_details(turn)

if question := st.chat_input("Describe your IT issue or ask about a ticket..."):
    st.session_state.turns.append({"role": "user", "text": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        try:
            with st.spinner("Looking into it..."):
                result = assistant.chat(
                    question,
                    st.session_state.api_history,
                    model=MODELS[model_name],
                    effort=effort,
                )
        except anthropic.AuthenticationError:
            st.error("Authentication failed - check ANTHROPIC_API_KEY in your .env.")
            st.stop()
        except anthropic.RateLimitError:
            st.error("Rate limited - wait a moment and try again.")
            st.stop()
        except anthropic.APIStatusError as e:
            st.error(f"API error {e.status_code}: {e.message}")
            st.stop()
        except anthropic.APIConnectionError:
            st.error("Could not reach the Anthropic API - check your network.")
            st.stop()

        if assistant.kb.last_error:
            st.warning(assistant.kb.last_error)

        turn = {
            "role": "assistant",
            "text": result.answer,
            "source": getattr(result, "source", ""),
            "rag_hits": result.rag_hits,
            "tool_events": result.tool_events,
            "meta": (
                f"{result.model} · effort {effort} · "
                f"{result.usage.get('input_tokens', 0)} in ({result.usage.get('cached_tokens', 0)} cached) / "
                f"{result.usage.get('output_tokens', 0)} out tokens"
                if result.model else ""
            ),
        }
        st.markdown(turn["text"])
        render_details(turn)

    st.session_state.turns.append(turn)
