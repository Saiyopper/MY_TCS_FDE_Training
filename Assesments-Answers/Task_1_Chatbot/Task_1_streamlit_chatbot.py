"""
Day-10 Task: Streamlit chat app on the Claude / Anthropic SDK.

"""
# Import and setup libraries
import anthropic
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# Model catalogue
MODELS = {
    "Claude Opus 5.5": {
        "id": "claude-opus-5-5",
        "thinking_off": None,
        "default_effort": "medium",
        "fallback": True,
    },
    "Claude Sonnet 5.5": {
        "id": "claude-sonnet-5-5",
        "thinking_off": {"type": "between_tools"},
        "default_effort": "high",
        "fallback": True,
    },
    "Claude Haiku 5.5": {
        "id": "claude-haiku-5-5",
        "thinking_off": {"type": "disabled"},
        "default_effort": "medium",
        "fallback": False,
    },
}

EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
# Turning thinking off is only accepted at effort "high" or below.
THINKING_OFF_MAX_EFFORT = {"low", "medium", "high"}

FALLBACK_BETA = "server-side-fallback-2026-07-01"

#Caching the API Client
@st.cache_resource
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()

#Building the API request
def build_request(model_cfg, effort, thinking_on, system_prompt, history):
    """Combine the sidebar settings and chat history into one request."""
    params = {
        "model": model_cfg["id"],
        "max_tokens": 64000,
        "messages": history,
        "output_config": {"effort": effort},
    }

    if system_prompt.strip():
        params["system"] = system_prompt.strip()

    note = None
    if thinking_on:
        # Show a readable summary of the model's reasoning.
        params["thinking"] = {"type": "adaptive", "display": "summarized"}
    elif model_cfg["thinking_off"] is None:
        note = "This model can't turn thinking off; lower the effort to reduce it."
    elif effort not in THINKING_OFF_MAX_EFFORT:
        params["thinking"] = {"type": "adaptive"}
        note = f"Thinking can only be turned off at effort 'high' or below; kept on for '{effort}'."
    else:
        params["thinking"] = model_cfg["thinking_off"]

    if model_cfg["fallback"]:
        # If a safety classifier declines, retry on a suitable fallback model.
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"

    return params, note


# Pagelayout & Sidebar controls
st.set_page_config(page_title=" Welcome to the Intelligence Chat Bot", page_icon="💬")
st.title("💬 Welcome to the Intelligence Chat Bot")

with st.sidebar:
    st.header("Settings")

    model_name = st.selectbox("Model", list(MODELS.keys()))
    model_cfg = MODELS[model_name]

    effort = st.select_slider(
        "Effort",
        options=EFFORT_LEVELS,
        value=model_cfg["default_effort"],
        help="Higher effort = deeper reasoning, more tokens, slower replies.",
    )

    thinking_on = st.toggle("Thinking", value=True, help="Show Claude's reasoning summary.")

    system_prompt = st.text_area(
        "System instruction",
        value="You are a helpful assistant.",
        height=150,
    )

    if st.button("Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# Managing Chat History
if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg.get("thinking"):
            with st.expander("Thinking"):
                st.markdown(msg["thinking"])
        st.markdown(msg["text"])


# Chat turn and Streaming responses
if prompt := st.chat_input("Ask Claude anything..."):
    st.session_state.messages.append(
        {"role": "user", "text": prompt, "api_content": prompt}
    )
    with st.chat_message("user"):
        st.markdown(prompt)

    history = [
        {"role": m["role"], "content": m["api_content"]}
        for m in st.session_state.messages
    ]
    params, note = build_request(model_cfg, effort, thinking_on, system_prompt, history)
    if note:
        st.info(note)

# Errod Handling & Saving the Assistant's reply
    with st.chat_message("assistant"):
        thinking_box = st.empty()
        answer_box = st.empty()
        thinking_text, answer_text = "", ""

        try:
            client = get_client()
            stream_ctx = (
                client.beta.messages.stream(**params)
                if "betas" in params
                else client.messages.stream(**params)
            )
            with stream_ctx as stream:
                for event in stream:
                    if event.type != "content_block_delta":
                        continue
                    if event.delta.type == "thinking_delta":
                        thinking_text += event.delta.thinking
                        with thinking_box.container():
                            with st.expander("Thinking…", expanded=True):
                                st.markdown(thinking_text)
                    elif event.delta.type == "text_delta":
                        answer_text += event.delta.text
                        answer_box.markdown(answer_text + "▌")
                final = stream.get_final_message()
        except anthropic.AuthenticationError:
            st.error("Authentication failed - check ANTHROPIC_API_KEY.")
            st.stop()
        except anthropic.NotFoundError:
            st.error(f"Model '{model_cfg['id']}' is not available to this API key.")
            st.stop()
        except anthropic.RateLimitError:
            st.error("Rate limited - wait a moment and try again.")
            st.stop()
        except anthropic.BadRequestError as e:
            st.error(f"Request rejected: {e.message}")
            st.stop()
        except anthropic.APIStatusError as e:
            st.error(f"API error {e.status_code}: {e.message}")
            st.stop()
        except anthropic.APIConnectionError:
            st.error("Could not reach the Anthropic API - check your network.")
            st.stop()

        if final.stop_reason == "refusal":
            answer_text = answer_text or "_Claude declined to answer this request._"
        elif final.stop_reason == "max_tokens":
            answer_text += "\n\n_(Response truncated: max_tokens reached.)_"

        # Collapse the live thinking view into a closed expander.
        if thinking_text:
            with thinking_box.container():
                with st.expander("Thinking"):
                    st.markdown(thinking_text)
        else:
            thinking_box.empty()
        answer_box.markdown(answer_text)

        u = final.usage
        st.caption(
            f"{final.model} · effort {effort} · "
            f"{u.input_tokens} in / {u.output_tokens} out tokens"
        )

    if final.stop_reason == "refusal":
        # Drop the declined exchange so the history stays valid for the next turn.
        st.session_state.messages.pop()
        st.stop()

    st.session_state.messages.append(
        {
            "role": "assistant",
            "text": answer_text,
            "thinking": thinking_text,
            # Send the full content blocks back unchanged on the next turn.
            "api_content": final.content,
        }
    )
