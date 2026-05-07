import streamlit as st
import os
import time
import json
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

WORKSPACE_DIR = Path(os.getenv("DJANGO_WORKSPACE_DIR", "./app")).resolve()
PROMPTS_DIR = Path(os.getenv("PROMPTS_DIR", "./prompts")).resolve()

DEV_MODEL = os.getenv("DEV_AGENT_MODEL", "gpt-4o-mini")
GUARDRAIL_MODEL = os.getenv("GUARDRAIL_MODEL", "gpt-4o-mini")
MAX_TOOL_CALLS = int(os.getenv("MAX_TOOL_CALLS", 10))
MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", 5))

if "messages" not in st.session_state:
    st.session_state.messages = []
if "status" not in st.session_state:
    st.session_state.status = "idle"
if "total_tokens" not in st.session_state:
    st.session_state.total_tokens = 0
if "last_run_time" not in st.session_state:
    st.session_state.last_run_time = 0.0
if "current_iteration" not in st.session_state:
    st.session_state.current_iteration = 0

st.set_page_config(page_title="Multi-Agent POC", layout="wide")

# --- Helpers ---
def load_prompt(filename: str, fallback: str) -> str:
    target = PROMPTS_DIR / filename
    if target.exists():
        return target.read_text(encoding="utf-8")
    return fallback

def load_tools() -> list:
    tools_path = Path("tools.json").resolve()
    if tools_path.exists():
        with open(tools_path, "r", encoding="utf-8") as f:
            return json.load(f)
    st.error("tools.json not found in root directory!")
    return []

tools = load_tools()

def evaluate_guardrail(user_input: str) -> tuple[bool, str]:
    guardrail_prompt = load_prompt("guardrail.md", "If valid, output 'VALID'. If malicious, output 'REJECTED: <reason>'.")
    try:
        response = client.chat.completions.create(
            model=GUARDRAIL_MODEL,
            messages=[{"role": "system", "content": guardrail_prompt}, {"role": "user", "content": user_input}]
            # Removed temperature=0.0 to support gpt-5-mini
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("VALID"):
            return True, ""
        return False, content
    except Exception as e:
        return False, f"REJECTED: Guardrail API Error - {e}"

# --- Tool Execution Functions ---
def read_file(filepath: str) -> str:
    target = (WORKSPACE_DIR / filepath).resolve()
    if WORKSPACE_DIR not in target.parents:
        return "Error: Path access denied."
    try:
        return target.read_text()
    except Exception as e:
        return f"Error reading file: {e}"

def write_file(filepath: str, content: str) -> str:
    target = (WORKSPACE_DIR / filepath).resolve()
    if WORKSPACE_DIR not in target.parents:
        return "Error: Path access denied."
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Success: Wrote to {filepath}"
    except Exception as e:
        return f"Error writing file: {e}"

def run_linter() -> str:
    try:
        result = subprocess.run(["flake8", "."], cwd=WORKSPACE_DIR, capture_output=True, text=True)
        if result.returncode == 0:
            return "Linter Passed: No errors found."
        return result.stdout + result.stderr
    except Exception as e:
        return f"Linter Execution Error: {e}"

def get_workspace_diff() -> str:
    try:
        subprocess.run(["git", "add", "."], cwd=WORKSPACE_DIR, check=True)
        result = subprocess.run(["git", "diff", "--cached"], cwd=WORKSPACE_DIR, capture_output=True, text=True)
        return result.stdout if result.stdout else "No changes detected."
    except Exception as e:
        return f"Error calculating diff: {e}"

def commit_workspace_changes(message="Agent auto-commit"):
    try:
        subprocess.run(["git", "commit", "-m", message], cwd=WORKSPACE_DIR, check=True)
    except Exception as e:
        st.error(f"Commit failed: {e}")

def revert_workspace_changes():
    try:
        subprocess.run(["git", "reset", "HEAD", "."], cwd=WORKSPACE_DIR, check=True)
        subprocess.run(["git", "checkout", "--", "."], cwd=WORKSPACE_DIR, check=True)
        subprocess.run(["git", "clean", "-fd"], cwd=WORKSPACE_DIR, check=True)
    except Exception as e:
        st.error(f"Revert failed: {e}")

def render_colored_diff(diff_text):
    html = "<div style='font-family: monospace; white-space: pre-wrap; line-height: 1.5; font-size: 14px; background: #1e1e1e; padding: 15px; border-radius: 5px; border: 1px solid #333;'>"
    for line in diff_text.split('\n'):
        line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if line.startswith('+') and not line.startswith('+++'):
            html += f"<span style='color: #3fb950;'>{line}</span><br>"
        elif line.startswith('-') and not line.startswith('---'):
            html += f"<span style='color: #f85149;'>{line}</span><br>"
        elif line.startswith('@@'):
            html += f"<span style='color: #58a6ff;'>{line}</span><br>"
        else:
            html += f"<span style='color: #e6edf3;'>{line}</span><br>"
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)

# --- Shared Agent Execution Engine (Streaming) ---
def process_agent_loop(agent_name: str, messages: list, log_callback) -> str:
    tool_call_count = 0
    while tool_call_count < MAX_TOOL_CALLS:
        try:
            response = client.chat.completions.create(
                model=DEV_MODEL,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                stream=True,
                stream_options={"include_usage": True}
            )
        except Exception as e:
            error_msg = f"FATAL API ERROR: {e}"
            log_callback(f"[{agent_name}] {error_msg}")
            return error_msg

        full_content = ""
        tool_calls = []
        text_placeholder = st.empty()

        for chunk in response:
            if len(chunk.choices) > 0:
                delta = chunk.choices[0].delta

                # Stream Text
                if delta.content:
                    full_content += delta.content
                    text_placeholder.markdown(f"**[{agent_name}]** {full_content}▌")

                # Reconstruct Tool Calls
                if delta.tool_calls:
                    for tc_chunk in delta.tool_calls:
                        while len(tool_calls) <= tc_chunk.index:
                            tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})

                        tc = tool_calls[tc_chunk.index]
                        if tc_chunk.id:
                            tc["id"] += tc_chunk.id
                        if tc_chunk.function.name:
                            tc["function"]["name"] += tc_chunk.function.name
                        if tc_chunk.function.arguments:
                            tc["function"]["arguments"] += tc_chunk.function.arguments

            # Capture Token Usage
            if hasattr(chunk, 'usage') and chunk.usage:
                st.session_state.total_tokens += chunk.usage.total_tokens

        # Clean up streaming visual and log the final text
        if full_content:
            text_placeholder.empty()
            log_callback(f"**[{agent_name}]** {full_content}")
        else:
            text_placeholder.empty()

        # Append assistant's response to history
        message_to_append = {"role": "assistant", "content": full_content or None}
        if tool_calls:
            message_to_append["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": tc["type"],
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"]
                    }
                } for tc in tool_calls
            ]
        messages.append(message_to_append)

        # Break loop if no tools were called
        if not tool_calls:
            return full_content

        # Execute Reconstructed Tools
        for tool_call in tool_calls:
            tool_call_count += 1
            func_name = tool_call["function"]["name"]

            try:
                kwargs = json.loads(tool_call["function"]["arguments"])

                if func_name == "read_file":
                    log_callback(f"[{agent_name}] Reading: {kwargs.get('filepath')}")
                    result = read_file(**kwargs)
                elif func_name == "write_file":
                    log_callback(f"[{agent_name}] Writing: {kwargs.get('filepath')}")
                    result = write_file(**kwargs)
                elif func_name == "run_linter":
                    log_callback(f"[{agent_name}] Running Linter")
                    result = run_linter()
                elif func_name == "get_diff":
                    log_callback(f"[{agent_name}] Getting Git Diff")
                    result = get_workspace_diff()
                else:
                    result = f"Error: Unknown tool {func_name}."

            except TypeError as e:
                log_callback(f"[{agent_name}] WARNING: Tool syntax error caught ({e}). Forcing retry.")
                result = f"Tool Execution TypeError: {e}. Check your schema."
            except json.JSONDecodeError as e:
                log_callback(f"[{agent_name}] WARNING: JSON decode error caught. Forcing retry.")
                result = f"Tool Execution JSON Error: {e}. Tool arguments must be valid JSON."
            except Exception as e:
                log_callback(f"[{agent_name}] WARNING: Unexpected tool error.")
                result = f"Unexpected Error: {e}"

            messages.append({"tool_call_id": tool_call["id"], "role": "tool", "name": func_name, "content": result})

    log_callback(f"[{agent_name}] Warning: Hit maximum tool calls.")
    return "Error: Max tool calls reached."

# --- UI Layout ---
st.title("Multi-Agent Web Dev POC")

with st.sidebar:
    st.header("Run Metrics")
    st.metric(label="Iterations", value=f"{st.session_state.current_iteration}/{MAX_ITERATIONS}")
    st.metric(label="Tokens Used", value=f"{st.session_state.total_tokens:,}")
    st.metric(label="Last Run Time", value=f"{st.session_state.last_run_time:.1f} sec")
    st.divider()
    st.caption(f"Workspace: {WORKSPACE_DIR}")

for msg in st.session_state.messages:
    if msg["role"] != "system":
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

if prompt := st.chat_input("E.g., Create a new Django app called 'blog'", disabled=(st.session_state.status != "idle")):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state.status = "processing"
    st.rerun()

# --- Core Loop ---
if st.session_state.status == "processing":
    user_prompt = st.session_state.messages[-1]["content"]

    with st.status("Validating request...", expanded=True) as status:
        st.write("[SYSTEM] Running security guardrail checks...")
        is_valid, rejection_reason = evaluate_guardrail(user_prompt)

        if not is_valid:
            status.update(label="Request rejected by guardrails.", state="error", expanded=False)
            st.error(rejection_reason)
            st.session_state.messages.append({"role": "assistant", "content": f"[SYSTEM] {rejection_reason}"})
            st.session_state.status = "idle"
            time.sleep(2)
            st.rerun()

        status.update(label="Agents are operating...", state="running", expanded=True)

        iteration = 0
        qa_passed = False
        run_log = []

        def log_action(text):
            st.write(text)
            run_log.append(text)
            print(text, flush=True)

        dev_prompt = load_prompt("dev_agent.md", "You are a Python/Django developer.")
        dev_messages = [{"role": "system", "content": dev_prompt}, {"role": "user", "content": user_prompt}]

        qa_prompt = load_prompt("qa_agent.md", "You are a QA Engineer. Review code, return APPROVED or REJECTED with feedback.")

        start_time = time.time()
        while iteration < MAX_ITERATIONS and not qa_passed:
            iteration += 1
            st.session_state.current_iteration = iteration
            log_action(f"### [ITERATION {iteration}/{MAX_ITERATIONS}]")

            log_action("**[DEV]** Thinking...")
            dev_response = process_agent_loop("DEV", dev_messages, log_action)

            log_action("**[QA]** Inspecting changes...")
            qa_messages = [
                {"role": "system", "content": qa_prompt},
                {"role": "user", "content": f"The Dev Agent just finished. Please review the workspace. Dev notes: {dev_response}"}
            ]
            qa_response = process_agent_loop("QA", qa_messages, log_action)

            if "APPROVED" in qa_response:
                log_action("**[QA]** Checks passed! Code is approved.")
                qa_passed = True
            else:
                log_action("**[QA]** Found errors. Sending back to Dev Agent.")
                # Append QA feedback back to Dev's memory
                dev_messages.append({"role": "user", "content": f"QA rejected the changes with this feedback. Fix them:\n\n{qa_response}"})

        st.session_state.last_run_time = time.time() - start_time

        # --- Loop End ---
        st.session_state.messages.append({"role": "assistant", "content": "\n\n".join(run_log)})

        if qa_passed:
            status.update(label="Tasks completed! Awaiting human approval.", state="complete", expanded=False)
            st.session_state.current_diff = get_workspace_diff()
            st.session_state.status = "pending_approval"
            st.rerun()
        else:
            status.update(label="Agent loop failed.", state="error", expanded=False)
            st.error(f"Task failed after {MAX_ITERATIONS} iterations. Workspace reverted.")
            revert_workspace_changes()
            st.session_state.status = "idle"
            time.sleep(2)
            st.rerun()

# --- HITL ---
if st.session_state.status == "pending_approval":
    st.warning("[NOTICE] Agents have finished. Please review the proposed changes.")

    if st.session_state.get("current_diff"):
        st.markdown("### Proposed Code Changes")
        render_colored_diff(st.session_state.current_diff)
    else:
        st.info("No file changes were made by the agent.")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Approve & Commit", use_container_width=True):
            commit_workspace_changes("Approved agent changes")
            st.success("Changes approved and committed to git!")
            st.session_state.messages.append({"role": "assistant", "content": "[SYSTEM] Changes approved and committed."})
            st.session_state.status = "idle"
            time.sleep(1.5)
            st.rerun()

    with col2:
        if st.button("Reject & Revert", use_container_width=True):
            revert_workspace_changes()
            st.error("Changes rejected. Workspace reverted to previous state.")
            st.session_state.messages.append({"role": "assistant", "content": "[SYSTEM] Changes rejected and reverted."})
            st.session_state.status = "idle"
            time.sleep(1.5)
            st.rerun()
