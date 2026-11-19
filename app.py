import json
import anthropic
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration & Styling
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Claude Intelligence Studio",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for polished look
st.markdown(
    """
    <style>
    .stChatInput { bottom: 20px; }
    .stAlert { border-radius: 8px; }
    </style>
    """,
    unsafe_allow_html=True,
)

MODELS = {
    "Claude Sonnet 3.5": {
        "id": "claude-3-5-sonnet-20241022",
        "pricing": {"input": 3.00, "output": 15.00}, # per million tokens
    },
    "Claude Haiku 3.5": {
        "id": "claude-3-5-haiku-20241022",
        "pricing": {"input": 0.80, "output": 4.00},
    },
    "Claude Opus 3": {
        "id": "claude-3-opus-20240229",
        "pricing": {"input": 15.00, "output": 75.00},
    },
}

PERSONAS = {
    "Default Assistant": "You are a helpful, direct, and precise AI assistant.",
    "Python Code Auditor": "You are an elite Python architect. Focus on clean code, PEP 8, performance, and robust error handling.",
    "Data Scientist": "You are an expert data scientist specializing in Pandas, NumPy, scikit-learn, and statistical modeling.",
    "Creative Writer": "You are a vivid, engaging writer with a flair for narrative pacing and rich vocabulary.",
}

@st.cache_resource
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()

# Initialize Session State
if "messages" not in st.session_state:
    st.session_state.messages = []
if "total_input_tokens" not in st.session_state:
    st.session_state.total_input_tokens = 0
if "total_output_tokens" not in st.session_state:
    st.session_state.total_output_tokens = 0

# ---------------------------------------------------------------------------
# Sidebar Controls
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("⚡ Claude Studio")
    st.markdown("---")

    selected_model_name = st.selectbox("Select Model", list(MODELS.keys()))
    model_cfg = MODELS[selected_model_name]

    st.markdown("### Persona Presets")
    selected_persona = st.selectbox("Choose Persona", list(PERSONAS.keys()))
    
    system_prompt = st.text_area(
        "System Instruction",
        value=PERSONAS[selected_persona],
        height=120,
    )

    max_tokens = st.slider("Max Response Tokens", 512, 8192, 4096, 256)
    temperature = st.slider("Temperature", 0.0, 1.0, 0.7, 0.1)

    st.markdown("---")
    st.markdown("### Session Statistics")
    in_tok = st.session_state.total_input_tokens
    out_tok = st.session_state.total_output_tokens
    
    # Calculate approximate cost
    cost = (in_tok * model_cfg["pricing"]["input"] + out_tok * model_cfg["pricing"]["output"]) / 1_000_000
    
    st.metric("Input Tokens", f"{in_tok:,}")
    st.metric("Output Tokens", f"{out_tok:,}")
    st.metric("Estimated Cost", f"${cost:.4f}")

    st.markdown("---")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Clear Chat", use_container_width=True):
            st.session_state.messages = []
            st.session_state.total_input_tokens = 0
            st.session_state.total_output_tokens = 0
            st.rerun()
            
    with col2:
        if st.session_state.messages:
            chat_json = json.dumps(st.session_state.messages, indent=2)
            st.download_button(
                "Export",
                data=chat_json,
                file_name="claude_chat_export.json",
                mime="application/json",
                use_container_width=True,
            )

# ---------------------------------------------------------------------------
# Main Chat Interface
# ---------------------------------------------------------------------------
st.title("💬 Interactive Workspace")

# Render existing message history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["text"])

# Handle user input
if prompt := st.chat_input("Message Claude..."):
    st.session_state.messages.append(
        {"role": "user", "text": prompt, "api_content": prompt}
    )
    with st.chat_message("user"):
        st.markdown(prompt)

    # Prepare message history for API
    history = [
        {"role": m["role"], "content": m["api_content"]}
        for m in st.session_state.messages
    ]

    api_params = {
        "model": model_cfg["id"],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": history,
    }

    if system_prompt.strip():
        api_params["system"] = system_prompt.strip()

    with st.chat_message("assistant"):
        answer_box = st.empty()
        answer_text = ""

        try:
            client = get_client()
            with client.messages.stream(**api_params) as stream:
                for text in stream.text_stream:
                    answer_text += text
                    answer_box.markdown(answer_text + "▌")
            
            final_message = stream.get_final_message()
            
            # Update token usage stats
            st.session_state.total_input_tokens += final_message.usage.input_tokens
            st.session_state.total_output_tokens += final_message.usage.output_tokens

        except anthropic.AuthenticationError:
            st.error("Authentication failed: Check your ANTHROPIC_API_KEY environment variable.")
            st.stop()
        except anthropic.RateLimitError:
            st.error("Rate limit reached. Please wait a moment before sending another message.")
            st.stop()
        except Exception as e:
            st.error(f"An error occurred: {str(e)}")
            st.stop()

        answer_box.markdown(answer_text)
        
        # Display turn metadata footer
        u = final_message.usage
        st.caption(f"Model: {final_message.model} | 📥 {u.input_tokens} tokens | 📤 {u.output_tokens} tokens")

    # Save assistant response to session state
    st.session_state.messages.append(
        {
            "role": "assistant",
            "text": answer_text,
            "api_content": final_message.content,
        }
    )