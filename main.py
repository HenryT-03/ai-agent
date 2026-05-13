import argparse
import json
import os
import re
import threading
import time
from collections import Counter, deque
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from google import genai

from actions import (
    init_run,
    install_dependency,
    list_files,
    read_file,
    run_script,
    write_script,
    run_mafft,
    run_iqtree,
    probe_environment,
)
from prompts import few_shot_messages, system_prompt

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL_ID = "gemini-3.1-flash-lite"

RATE_LIMIT_RPM = 5
MIN_INTERVAL = 60.0 / RATE_LIMIT_RPM
_last_call_time = 0.0
_rate_lock = threading.Lock()

MAX_TURNS = 24
MAX_REPLAN_COUNT = 4
RECENT_ACTION_WINDOW = 6


# ---------------------------
# STATE MODEL
# ---------------------------

def init_state(query: str, run_name: str) -> Dict[str, Any]:
    return {
        "goal": query,
        "run_name": run_name,
        "subgoals": [],
        "completed": [],
        "current_focus": None,
        "failures": [],
        "last_error_type": None,
        "recent_actions": deque(maxlen=RECENT_ACTION_WINDOW),
        "recent_errors": deque(maxlen=RECENT_ACTION_WINDOW),
        "last_file_snapshot": set(),
        "last_file_count": 0,
        "last_total_bytes": 0,
        "no_output_progress_count": 0,
        "identical_action_count": 0,
        "same_error_count": 0,
        "replan_count": 0,
        "forced_replan_reason": None,
        "environment": {},
    }


def action_signature(action: Dict[str, Any]) -> str:
    """Stable signature for detecting repeated identical actions."""
    try:
        return json.dumps(action, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(action)


def classify_failure(stderr: str, returncode: int) -> Optional[str]:
    if returncode == 0:
        return None

    s = (stderr or "").lower()
    patterns = [
        ("dependency", ["module not found", "modulenotfounderror", "there is no package", "package .* is not installed", "library.*not found"]),
        ("missing_file", ["no such file", "cannot open file", "file not found", "does not exist"]),
        ("permission", ["permission denied", "access is denied"]),
        ("timeout", ["timed out", "timeout"]),
        ("syntax", ["syntaxerror", "parse error", "unexpected token", "unexpected end"]),
        ("command_not_found", ["command not found", "not recognized", "not found in path"]),
    ]

    for label, needles in patterns:
        for needle in needles:
            if re.search(needle, s):
                return label
    return "unknown"


def parse_file_listing(output: Any) -> Tuple[set, int, int]:
    """Return (paths, file_count, total_bytes) from list_files text."""
    if not isinstance(output, str) or output.startswith("No files") or output.startswith("Error"):
        return set(), 0, 0

    paths = set()
    total_bytes = 0
    for line in output.splitlines():
        m = re.match(r"^(.+?)\s+\((\d+) bytes", line.strip())
        if not m:
            continue
        paths.add(m.group(1))
        total_bytes += int(m.group(2))
    return paths, len(paths), total_bytes


def update_plan(state: Dict[str, Any], subgoals: List[str], focus: Optional[str] = None) -> str:
    clean_subgoals = [str(s).strip() for s in subgoals if str(s).strip()]
    state["subgoals"] = clean_subgoals
    state["current_focus"] = focus.strip() if isinstance(focus, str) and focus.strip() else (clean_subgoals[0] if clean_subgoals else None)
    state["forced_replan_reason"] = None
    return "Plan updated."


def mark_completed_from_listing(state: Dict[str, Any], output: Any) -> None:
    paths, count, total_bytes = parse_file_listing(output)
    state["completed"] = sorted(paths)

    if count > state["last_file_count"] or total_bytes > state["last_total_bytes"] or paths != state["last_file_snapshot"]:
        state["no_output_progress_count"] = 0
    else:
        state["no_output_progress_count"] += 1

    state["last_file_snapshot"] = paths
    state["last_file_count"] = count
    state["last_total_bytes"] = total_bytes


def update_entropy_metrics(state: Dict[str, Any], action: Dict[str, Any], result: Any) -> Optional[str]:
    """
    Detect rising state entropy: repeated identical actions, same unresolved errors,
    or repeated output checks with no new files/bytes.
    """
    sig = action_signature(action)
    recent_actions = state["recent_actions"]
    recent_actions.append(sig)
    state["identical_action_count"] = Counter(recent_actions).get(sig, 0)

    err_type = None
    if isinstance(result, dict):
        err_type = classify_failure(result.get("stderr", ""), result.get("returncode", 0))
        state["last_error_type"] = err_type
        if err_type:
            state["failures"].append(err_type)
            state["recent_errors"].append(err_type)
            state["same_error_count"] = Counter(state["recent_errors"]).get(err_type, 0)
        else:
            state["same_error_count"] = 0
    elif isinstance(result, str) and "not found in PATH" in result:
        err_type = "command_not_found"
        state["last_error_type"] = err_type
        state["failures"].append(err_type)
        state["recent_errors"].append(err_type)
        state["same_error_count"] = Counter(state["recent_errors"]).get(err_type, 0)

    func = action.get("function_name")
    if func == "list_files":
        mark_completed_from_listing(state, result)

    if state["identical_action_count"] >= 3 and err_type:
        return "same action was selected multiple times after repeated failures"

    if err_type and state["same_error_count"] >= 2:
        return f"same error class repeated: {err_type}"

    return None


def build_state_block(state: Dict[str, Any]) -> str:
    serializable = {
        "goal": state["goal"],
        "run_name": state["run_name"],
        "environment": state["environment"],
        "subgoals": state["subgoals"],
        "completed_files": state["completed"],
        "focus": state["current_focus"],
        "failures": state["failures"][-6:],
        "last_error": state["last_error_type"],
    }
    return "STATE\n" + json.dumps(serializable, indent=2, ensure_ascii=False)


# ---------------------------
# MODEL CALL
# ---------------------------

def call_model(messages, system_instruction, retries=3, delay=10):
    global _last_call_time
    errors = []

    for _attempt in range(retries):
        try:
            with _rate_lock:
                now = time.time()
                wait = MIN_INTERVAL - (now - _last_call_time)
                if wait > 0:
                    time.sleep(wait)
                _last_call_time = time.time()

            return client.models.generate_content(
                model=MODEL_ID,
                contents=messages,
                config={"system_instruction": system_instruction},
            )
        except Exception as e:
            errors.append(str(e))
            time.sleep(delay)
            delay *= 2

    raise RuntimeError(f"Model unavailable after {retries} retries. Errors: {'; '.join(errors)}")


# ---------------------------
# ACTION PARSING
# ---------------------------

def extract_action(text: str) -> Optional[Dict[str, Any]]:
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    decoder = json.JSONDecoder()
    i = 0
    while i < len(text):
        try:
            obj, j = decoder.raw_decode(text, i)
            if isinstance(obj, dict) and "function_name" in obj:
                return obj
            i = max(j, i + 1)
        except json.JSONDecodeError:
            i += 1
    return None


def build_prompt(state: Dict[str, Any]) -> str:
    return system_prompt + "\n\n" + build_state_block(state)


# ---------------------------
# ACTION DISPATCH
# ---------------------------

def dispatch_action(state: Dict[str, Any], action: Dict[str, Any]) -> Any:
    func = action.get("function_name")
    args = action.get("function_parms", {}) or {}

    available_actions = {
        "write_script": write_script,
        "run_script": run_script,
        "read_file": read_file,
        "install_dependency": install_dependency,
        "list_files": list_files,
        "run_mafft": run_mafft,
        "run_iqtree": run_iqtree,
        "probe_environment": probe_environment,
    }

    if func == "plan":
        return update_plan(state, args.get("subgoals", []), args.get("focus"))

    if func not in available_actions:
        state["failures"].append("invalid_action")
        state["last_error_type"] = "invalid_action"
        return {"stdout": "", "stderr": f"Invalid action: {func}", "returncode": 1}

    return available_actions[func](**args)


def deterministic_retry(state: Dict[str, Any], action: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Apply deterministic fixes for common, solvable failures.
    Return a dict with instructions if a retry is recommended, or None.
    
    Handles:
    - Missing Python dependencies → auto-install and signal retry
    - Encoding issues → log suggestion
    """
    # Only process dict results (subprocess calls); skip string results (state updates)
    if not isinstance(result, dict):
        return None
    
    if result.get("returncode") == 0:
        return None  # Success, no retry needed
    
    stderr = (result.get("stderr") or "").lower()
    func = action.get("function_name")
    
    # Pattern: ModuleNotFoundError or "no module named"
    if func == "run_script" and ("modulenotfound" in stderr or "no module named" in stderr):
        # Extract package name if possible
        match_patterns = [
            r"no module named ['\"]?(\w+)['\"]?",
            r"modulenotfounderror[^:]*:\s*no module named ['\"]?(\w+)['\"]?",
        ]
        package = None
        for pattern in match_patterns:
            m = re.search(pattern, stderr)
            if m:
                package = m.group(1)
                break
        
        if package:
            # Auto-install and return signal
            print(f"[RETRY] Auto-installing missing Python dependency: {package}")
            install_result = install_dependency(package, "python")
            if install_result.get("returncode") == 0:
                return {"retry_action": action, "note": f"Installed {package}; please re-execute the script"}
    
    # Pattern: encoding errors
    if "decode" in stderr or "encoding" in stderr or "codec" in stderr:
        print("[SUGGESTION] Detected encoding issue; try error='ignore' in file operations")
    
    return None


def force_replan(state: Dict[str, Any], reason: str, messages: List[Dict[str, Any]]) -> None:
    state["replan_count"] += 1
    state["forced_replan_reason"] = reason
    state["current_focus"] = None
    messages.append({
        "role": "user",
        "parts": [{"text": f"Replan required before further execution. Reason: {reason}"}],
    })


# ---------------------------
# MAIN LOOP
# ---------------------------

def run_agent(query: str):
    run_name = init_run()
    state = init_state(query, run_name)

    state["environment"] = probe_environment(
        tools=["mafft", "iqtree"],
        python_packages=["Bio"],
    )
    print(f"Run directory: workspace/runs/{run_name}/")

    messages = few_shot_messages + [
        {"role": "user", "parts": [{"text": query}]}
    ]

    for turn in range(1, MAX_TURNS + 1):
        print(f"\n--- Turn {turn} ---")

        try:
            response = call_model(messages, build_prompt(state))
            text = response.text or ""
        except RuntimeError as e:
            return f"Answer: Model call failed: {e}"

        print(text)

        answer_match = re.search(r"^\s*Answer:\s*(.+)", text, re.MULTILINE | re.DOTALL)
        if answer_match:
            return answer_match.group(0).strip()

        action = extract_action(text)
        if not action:
            state["failures"].append("parse_failure")
            messages.append({"role": "model", "parts": [{"text": text}]})
            force_replan(state, "model output did not contain a parseable action", messages)
            continue

        result = dispatch_action(state, action)
        print(f"Action_Response: {result}")

        retry_signal = deterministic_retry(state, action, result)
        if retry_signal:
            print(f"[RETRY] {retry_signal.get('note', 'Retrying...')}")

        messages.append({"role": "model", "parts": [{"text": text}]})
        messages.append({"role": "user", "parts": [{"text": f"Action_Response: {result}"}]})

        reason = update_entropy_metrics(state, action, result)
        if reason:
            if state["replan_count"] >= MAX_REPLAN_COUNT:
                return (
                    "Answer: Stopped because replanning was triggered repeatedly. "
                    f"Latest reason: {reason}. Failures: {state['failures'][-6:]}"
                )
            force_replan(state, reason, messages)

    return "Answer: Max turns reached without completion."


# ---------------------------
# ENTRY
# ---------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?")
    args = parser.parse_args()

    print("Validating environment... OK")
    query = args.query or input("Query: ")
    print(run_agent(query))
