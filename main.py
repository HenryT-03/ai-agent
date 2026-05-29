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
    import_file,
    install_dependency,
    list_files,
    read_file,
    run_rscript,
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
SCRIPT_EXTENSIONS = (".py", ".sh", ".bat", ".r")
RESULT_EXTENSIONS = (
    ".txt",
    ".tsv",
    ".csv",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".html",
    ".fasta",
    ".fa",
    ".aln",
    ".treefile",
)


# ---------------------------
# STATE MODEL
# ---------------------------

def init_state(
    query: str,
    run_name: str,
    script_quality: str = "internal",
    requested_language: Optional[str] = None,
    requested_extension: Optional[str] = None,
) -> Dict[str, Any]:
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
        "last_file_sizes": {},
        "last_file_count": 0,
        "last_total_bytes": 0,
        "no_output_progress_count": 0,
        "identical_action_count": 0,
        "same_error_count": 0,
        "replan_count": 0,
        "forced_replan_reason": None,
        "environment": {},
        "script_quality": script_quality,
        "requested_language": requested_language,
        "requested_extension": requested_extension,
        "last_action_name": None,
        "last_read_file": None,
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
        ("path_translation_failure", ["cannot open", "/usr/bin/.*: line", "awk: fatal", "bad interpreter", "cygpath", "wslpath"]),
        ("external_tool_path_failure", ["winerror 2", "no such file or directory", "not found in path", "not recognized", "command not found"]),
        ("r_package_missing", ["there is no package called", "package .* not found", "namespace .* not available"]),
        ("r_tool_invocation", ["cannot run program", "createprocess error", "system command failed", "non-zero exit status"]),
        ("r_working_dir", ["cannot open the connection", "cannot open file"]),
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
    sizes = {}
    if isinstance(output, str):
        for line in output.splitlines():
            m = re.match(r"^(.+?)\s+\((\d+) bytes", line.strip())
            if m:
                sizes[m.group(1)] = int(m.group(2))
    state["last_file_sizes"] = sizes

    if count > state["last_file_count"] or total_bytes > state["last_total_bytes"] or paths != state["last_file_snapshot"]:
        state["no_output_progress_count"] = 0
    else:
        state["no_output_progress_count"] += 1

    state["last_file_snapshot"] = paths
    state["last_file_count"] = count
    state["last_total_bytes"] = total_bytes


def completed_file_set(state: Dict[str, Any]) -> set:
    """Return known files with a few display-name variants accepted."""
    known = set(state.get("completed", []))
    variants = set(known)
    for path in known:
        normalized = path.replace("\\", "/")
        variants.add(normalized)
        variants.add(os.path.basename(normalized))
    return variants


def add_completed_file(state: Dict[str, Any], filename: str) -> None:
    if not isinstance(filename, str) or not filename.strip():
        return
    clean = filename.strip().replace("\\", "/")
    state["completed"] = sorted(set(state.get("completed", [])) | {clean})


def refresh_file_inventory(state: Dict[str, Any], label: str = "FILES") -> str:
    """Synchronize runtime state with the actual run directory."""
    listing = list_files()
    mark_completed_from_listing(state, listing)
    print(f"[{label}]\n{listing}")
    return listing


def update_completed_from_action_result(state: Dict[str, Any], action: Dict[str, Any], result: Any) -> None:
    """Record files proven by successful file-writing action responses."""
    func = action.get("function_name")
    args = action.get("function_parms", {}) or {}

    if func == "write_script" and isinstance(result, str):
        m = re.search(r"Script written to\s+(.+)$", result)
        if m:
            add_completed_file(state, m.group(1))
        elif isinstance(args.get("filename"), str):
            add_completed_file(state, args["filename"])

    if func == "import_file" and isinstance(result, dict) and result.get("returncode") == 0:
        stdout = result.get("stdout", "")
        m = re.search(r"\bto\s+(.+)$", stdout)
        if m:
            add_completed_file(state, m.group(1))


def is_file_like_arg(value: Any) -> bool:
    if not isinstance(value, str) or value.startswith("-"):
        return False
    return bool(os.path.splitext(value.replace("\\", "/"))[1])


def expected_script_outputs(action: Dict[str, Any], before_files: set) -> List[str]:
    """Return explicit expected_outputs, or infer output args as a fallback."""
    if action.get("function_name") not in {"run_script", "run_rscript"}:
        return []

    parms = action.get("function_parms", {}) or {}
    explicit = parms.get("expected_outputs")
    if isinstance(explicit, list):
        return [item.replace("\\", "/") for item in explicit if isinstance(item, str) and item.strip()]
    if isinstance(explicit, str) and explicit.strip():
        return [explicit.replace("\\", "/")]

    args = parms.get("args", []) or []
    outputs = []
    known_before = set(before_files)
    for arg in args:
        if not is_file_like_arg(arg):
            continue
        normalized = arg.replace("\\", "/")
        if normalized not in known_before and os.path.basename(normalized) not in known_before:
            outputs.append(normalized)
    return outputs


def validate_expected_script_outputs(before_files: set, action: Dict[str, Any], result: Any, listing: Any) -> Optional[str]:
    """
    Treat missing/empty expected outputs after a zero-exit script as failure.

    Explicit expected_outputs is preferred. Inferring file-like args that were
    absent before execution remains a fallback for older model actions.
    """
    if action.get("function_name") not in {"run_script", "run_rscript"}:
        return None
    if not isinstance(result, dict) or result.get("returncode") != 0:
        return None

    expected = expected_script_outputs(action, before_files)
    if not expected:
        return None

    paths, _, _ = parse_file_listing(listing)
    sizes = {}
    if isinstance(listing, str):
        for line in listing.splitlines():
            m = re.match(r"^(.+?)\s+\((\d+) bytes", line.strip())
            if m:
                sizes[m.group(1)] = int(m.group(2))

    for output in expected:
        basename = os.path.basename(output)
        matched = output if output in paths else basename if basename in paths else None
        if not matched:
            return f"script exited 0 but expected output file was not created: {output}"
        if sizes.get(matched, 0) <= 0:
            return f"script exited 0 but expected output file is empty: {matched}"

    return None


def validate_action_against_state(state: Dict[str, Any], action: Dict[str, Any]) -> Optional[str]:
    """
    State validation: block consumers of run-local files unknown to runtime.

    This makes file verification a runtime invariant instead of a prompt-level
    suggestion. The model must list, import, or create files before using them.
    """
    func = action.get("function_name")
    args = action.get("function_parms", {}) or {}
    known = completed_file_set(state)

    if func == "write_script":
        code = args.get("code", "")
        if isinstance(code, str) and re.search(r"\bos\s*\.\s*system\s*\(", code):
            return "generated scripts may not use os.system(); use subprocess.run(..., check=False) with explicit returncode checks"
        filename = args.get("filename", "")
        ext = os.path.splitext(filename)[1]
        requested_ext = state.get("requested_extension")

        if state.get("script_quality") == "deliverable" and requested_ext and ext.lower() in {".py", ".r", ".sh", ".bat"}:
            helper_name = os.path.basename(filename).lower()
            is_helper = any(token in helper_name for token in ("helper", "test", "runner", "gen_"))
            if ext.lower() != requested_ext.lower() and not is_helper:
                return f"user requested a {state.get('requested_language')} script; final deliverable must use {requested_ext}, not {ext}"

        tool_call_pattern = (
            r"(subprocess\.(?:run|call|popen)|os\s*\.\s*system|system2\s*\(|system\s*\(|shell\s*\()"
            r"[\s\S]{0,200}?(mafft|iqtree)"
        )
        hardcoded_tool_path = r"(?i)[A-Z]:[\\/][^'\"\n]*(mafft|iqtree)[^'\"\n]*\.(exe|bat|cmd)"
        if isinstance(code, str):
            if ext.lower() == ".r" and re.search(r"\bsetwd\s*\(", code):
                return "R scripts must not use setwd(); pass paths as CLI args and avoid relying on mutable working directories"
            if state.get("script_quality") == "internal" and re.search(tool_call_pattern, code, re.IGNORECASE):
                return (
                    "internal scripts may not call MAFFT/IQ-TREE via subprocess/system; "
                    "use run_mafft/run_iqtree wrappers for bioinformatics execution and reserve scripts for parsing/transforms"
                )
            platform_specific_request = bool(re.search(r"\b(windows|platform-specific|absolute tool path|hardcoded path)\b", state.get("goal", ""), re.IGNORECASE))
            if re.search(hardcoded_tool_path, code) and not (state.get("script_quality") == "deliverable" and platform_specific_request):
                return "generated scripts may not hardcode absolute MAFFT/IQ-TREE tool paths; use PATH-aware invocation for deliverables or runtime wrappers internally"

    checks = {
        "run_script": ("filename",),
        # State validation: every file-dependent action must name a concrete
        # target that is already confirmed in STATE.completed_files. Accept the
        # filename alias for run_rscript because model outputs may drift, but
        # still validate the referenced file before execution.
        "run_rscript": ("script_path", "filename"),
        "read_file": ("filename",),
        "run_mafft": ("input_fasta",),
        "run_iqtree": ("input_fasta",),
    }

    target_arg_names = checks.get(func, ())
    if target_arg_names and not any(args.get(arg_name) for arg_name in target_arg_names):
        return (
            f"{func} requires a target file argument ({', '.join(target_arg_names)}). "
            "Call list_files if state may be stale, then use a file confirmed in STATE.completed_files; "
            "do not assume it exists."
        )

    for arg_name in target_arg_names:
        filename = args.get(arg_name)
        if filename and filename not in known:
            return (
                f"{filename} is not in STATE.completed_files. If state may be stale, call list_files; "
                "if it is absent, create it or import it from a real external absolute path; do not assume it exists."
            )

    if func == "run_script" and state.get("script_quality") == "deliverable":
        script_name = args.get("filename", "")
        if os.path.splitext(script_name)[1].lower() in {".py", ".r", ".sh", ".bat"} and args.get("args") and not args.get("expected_outputs"):
            return "deliverable test executions must include expected_outputs so runtime can verify created artifacts"

    if func == "run_script":
        script_name = args.get("filename", "")
        if isinstance(script_name, str) and script_name.lower().endswith(".r"):
            return "Use run_rscript for .R files, not run_script"

    if func == "run_rscript" and args.get("args") and not args.get("expected_outputs") and state.get("script_quality") == "deliverable":
        return "deliverable R test executions must include expected_outputs so runtime can verify created artifacts"

    return None


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
        "script_quality": state["script_quality"],
        "requested_language": state.get("requested_language"),
        "requested_extension": state.get("requested_extension"),
    }
    # Include script_format hint if available
    if "script_format" in state["environment"]:
        serializable["script_format_hint"] = f"Recommended for this environment: {state['environment']['script_format']}"
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
# INTENT CLASSIFICATION
# ---------------------------

def classify_script_intent(query: str) -> str:
    """
    Ask the model to classify the query as 'deliverable' or 'internal'.

    deliverable: the user wants a script as a reusable artifact — something
    they or a colleague will run independently, possibly with different inputs.

    internal: the user wants results or output; any script written is just
    scaffolding to get there and won't be reused.

    Uses a single lightweight call outside the main ReAct loop.
    Falls back to 'internal' on any failure so the main loop is unaffected.
    """
    prompt = (
        "Classify this bioinformatics task as either 'deliverable' or 'internal'.\n\n"
        "deliverable: the user wants a script they can reuse, share, or run independently "
        "with different inputs later. Signals include: 'write a script', 'create a tool', "
        "'test execute', 'make something I can run', asking for something portable or shareable.\n\n"
        "internal: the user wants results or output from running something. "
        "Any script is just a means to get there and won't be kept.\n\n"
        "Reply with exactly one word: deliverable or internal.\n\n"
        f"Task: {query}"
    )
    try:
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
        )
        text = (response.text or "").strip().lower()
        if "deliverable" in text:
            return "deliverable"
        return "internal"
    except Exception as e:
        print(f"[WARN] Intent classification failed ({e}), defaulting to 'internal'")
        return "internal"


def detect_requested_script_language(query: str) -> Tuple[Optional[str], Optional[str]]:
    q = query.lower()
    patterns = [
        ("r", ".R", [r"\br\s+script\b", r"\brscript\b"]),
        ("python", ".py", [r"\bpython\s+script\b", r"\b\.py\b"]),
        ("batch", ".bat", [r"\bbatch\s+script\b", r"\b\.bat\b"]),
        ("shell", ".sh", [r"\bshell\s+script\b", r"\bbash\s+script\b", r"\b\.sh\b"]),
    ]
    for language, extension, needles in patterns:
        if any(re.search(pattern, q) for pattern in needles):
            return language, extension
    return None, None


# ---------------------------
# INPUT STAGING
# ---------------------------

def extract_and_stage_inputs(query: str) -> List[str]:
    """
    Scan the query for absolute file paths that exist on disk and copy them
    into the current run directory before the agent loop starts.

    This prevents the agent from discovering staging failures mid-run and
    means turn 1 can assume named inputs are already present.
    """
    pattern = r'(?:[A-Za-z]:[\\\/]|\/)[^\s\'"<>|*?]+'
    candidates = re.findall(pattern, query)

    staged = []
    for path in candidates:
        path = path.rstrip(".,;)")
        if not os.path.isfile(path):
            continue
        result = import_file(path)
        if isinstance(result, dict) and result.get("returncode") == 0:
            basename = os.path.basename(path)
            staged.append(basename)
            print(f"[STAGED] {path} -> {basename}")
        else:
            err = result.get("stderr", str(result)) if isinstance(result, dict) else str(result)
            print(f"[WARN] Could not stage {path}: {err}")

    return staged


# ---------------------------
# PROTOCOL VALIDATION / ACTION PARSING
# ---------------------------

def text_after_pause(text: str) -> bool:
    """Protocol validation: PAUSE must be the last non-whitespace token."""
    pause_match = re.search(r"\bPAUSE\b", text)
    return bool(pause_match and text[pause_match.end():].strip())


def has_model_authored_action_response(text: str) -> bool:
    """Reject only protocol-looking Action_Response lines authored by model."""
    return bool(re.search(r"(?m)^\s*Action_Response\s*:", text))

def extract_actions(text: str) -> List[Dict[str, Any]]:
    """Parse Action JSON objects only from explicit Action: protocol blocks."""
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    decoder = json.JSONDecoder()
    actions = []
    for match in re.finditer(r"(?m)^\s*Action\s*:\s*", text):
        try:
            obj, _ = decoder.raw_decode(text, match.end())
            if isinstance(obj, dict) and "function_name" in obj:
                actions.append(obj)
        except json.JSONDecodeError:
            continue
    return actions


def extract_action(text: str) -> Optional[Dict[str, Any]]:
    actions = extract_actions(text)
    return actions[0] if actions else None


def build_prompt(state: Dict[str, Any]) -> str:
    return system_prompt + "\n\n" + build_state_block(state)


def extract_final_answer(text: str) -> Optional[str]:
    """Return a normalized final answer if the model used any known final marker."""
    answer_match = re.search(r"^\s*Answer:\s*(.+)", text, re.MULTILINE | re.DOTALL)
    if answer_match:
        return answer_match.group(0).strip()

    marker_match = re.search(r"<\s*final answer\s*>", text, re.IGNORECASE)
    if not marker_match:
        return None

    before = text[:marker_match.start()].strip()
    after = text[marker_match.end():].strip()

    if after:
        return f"Answer: {after}"

    # Some models append the marker after the actual final prose. Drop any
    # leading Thought line and use the remaining prose as the final answer.
    before = re.sub(r"^\s*Thought\s*:\s*.*(?:\r?\n)+", "", before, count=1, flags=re.IGNORECASE).strip()
    if before:
        return f"Answer: {before}"

    return "Answer: Task complete."


def final_answer_replan_reason(state: Dict[str, Any], answer: str) -> Optional[str]:
    """
    Refuse to end result-oriented runs with only source code as the artifact.

    The prompt carries the policy; this guard catches the common failure mode
    where the model says a script is ready even though no concrete result has
    been produced or reported.
    """
    if state.get("script_quality") == "deliverable":
        requested_ext = state.get("requested_extension")
        if requested_ext and not any(str(path).lower().endswith(requested_ext.lower()) for path in state.get("completed", [])):
            return f"user requested a {state.get('requested_language')} script, but no {requested_ext} deliverable is confirmed in STATE.completed_files"
        return None

    lowered = answer.lower()
    if state.get("last_action_name") not in {"list_files", "read_file"}:
        return "before final Answer, call list_files or read_file on the final artifact so completion is grounded in current runtime state"

    mentions_script = any(ext in lowered for ext in SCRIPT_EXTENSIONS) or "script" in lowered
    mentions_result_artifact = any(ext in lowered for ext in RESULT_EXTENSIONS)
    mentions_printed_result = any(
        phrase in lowered
        for phrase in (
            "printed result",
            "printed output",
            "stdout",
            "result:",
            "results:",
            "matrix:",
            "homology",
        )
    )

    if mentions_script and not mentions_result_artifact and not mentions_printed_result:
        return (
            "internal/result task ended with a script instead of a concrete result; "
            "execute the workflow and write a named result artifact such as result.txt, result.tsv, result.csv, or result.png"
        )

    if mentions_result_artifact:
        known_nonempty = any(size > 0 and path.lower() in lowered for path, size in state.get("last_file_sizes", {}).items())
        if not known_nonempty and not mentions_printed_result:
            return "final Answer mentions a result artifact, but no matching non-empty artifact was verified by list_files/read_file"

    goal = state.get("goal", "").lower()
    if any(term in goal for term in ("nearest neighbor", "nearest-neighbor", "closest neighbor", "sister")):
        if not any(term in lowered for term in ("nearest", "neighbor", "sister", "closest")):
            return "nearest-neighbor tasks must compute and include the specific neighbor/result in the final Answer, not only artifact names"

    return None


# ---------------------------
# ACTION DISPATCH
# ---------------------------

def dispatch_action(state: Dict[str, Any], action: Dict[str, Any]) -> Any:
    func = action.get("function_name")
    args = action.get("function_parms", {}) or {}

    if func == "run_rscript" and "script_path" not in args and "filename" in args:
        args = dict(args)
        args["script_path"] = args.pop("filename")
        action["function_parms"] = args

    available_actions = {
        "write_script": write_script,
        "run_script": run_script,
        "run_rscript": run_rscript,
        "read_file": read_file,
        "import_file": import_file,
        "install_dependency": install_dependency,
        "list_files": list_files,
        "run_mafft": run_mafft,
        "run_iqtree": run_iqtree,
        "probe_environment": probe_environment,
    }

    if func == "plan":
        return update_plan(state, args.get("subgoals", []), args.get("focus"))

    if func == "run_script":
        filename = args.get("filename", "")
        if isinstance(filename, str) and filename.lower().endswith(".r"):
            return {"stdout": "", "stderr": "Use run_rscript for .R files, not run_script", "returncode": 1}

    if func not in available_actions:
        state["failures"].append("invalid_action")
        state["last_error_type"] = "invalid_action"
        return {"stdout": "", "stderr": f"Invalid action: {func}", "returncode": 1}

    return available_actions[func](**args)


def deterministic_retry(state: Dict[str, Any], action: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Deterministic recovery: classify common execution failures and suggest the
    next safe strategy. This runs after dispatch, unlike state validation.

    Apply deterministic fixes for common, solvable failures.
    Return a dict with instructions if a retry is recommended, or None.

    Handles:
    - Missing Python dependencies -> auto-install and signal retry
    - External tool/path translation failures -> script-quality-aware replan
    - Encoding issues -> log suggestion
    """
    if not isinstance(result, dict):
        return None

    if result.get("returncode") == 0:
        return None

    stderr = (result.get("stderr") or "").lower()
    func = action.get("function_name")

    failure_class = classify_failure(result.get("stderr", ""), result.get("returncode", 0))

    if func == "run_rscript":
        if failure_class == "r_package_missing":
            m = re.search(r"there is no package called ['\"]([^'\"]+)['\"]", stderr)
            if m:
                package = m.group(1)
                print(f"[RETRY] Auto-installing missing R package: {package}")
                install_result = install_dependency(package, "r")
                if install_result.get("returncode") == 0:
                    return {"retry_action": action, "note": f"Installed R package {package}; re-execute the R script"}

        if failure_class == "r_tool_invocation":
            return {
                "replan_reason": (
                    "R system/system2 tool invocation failed; check tool availability with probe_environment, "
                    "use system2(command, args=c(...)) with an explicit args vector, and preserve the rest of the script"
                ),
                "failure_class": failure_class,
            }

        if failure_class == "r_working_dir":
            return {
                "replan_reason": (
                    "R file path error; remove setwd(), pass input/output paths as CLI args, "
                    "and use explicit paths rather than relying on working directory"
                ),
                "failure_class": failure_class,
            }

    # Generic because many tools fail similarly when Windows, POSIX shells, and
    # bundled CLI wrappers disagree about path syntax. Do not overfit to one
    # MAFFT/awk message; pick recovery based on whether the script is a
    # deliverable artifact or just internal execution scaffolding.
    if func == "run_script" and failure_class in {"path_translation_failure", "external_tool_path_failure"}:
        if state.get("script_quality") == "deliverable":
            reason = (
                f"{failure_class}: regenerate the deliverable using "
                "STATE.environment[\"script_format\"] and avoid POSIX shell/path assumptions on Windows; "
                "normalize/quote paths and keep explicit returncode checks; do not retry the same script without changing execution layer"
            )
        else:
            reason = (
                f"{failure_class}: for internal execution prefer runtime wrappers such as run_mafft/run_iqtree, "
                "or normalize paths and avoid POSIX shell assumptions on Windows; do not retry the same script without changing execution layer"
            )
        return {"replan_reason": reason, "failure_class": failure_class}

    if func == "run_script" and ("modulenotfound" in stderr or "no module named" in stderr):
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
            print(f"[RETRY] Auto-installing missing Python dependency: {package}")
            install_result = install_dependency(package, "python")
            if install_result.get("returncode") == 0:
                return {"retry_action": action, "note": f"Installed {package}; please re-execute the script"}

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

    print("[CLASSIFY] Determining script intent...")
    script_quality = classify_script_intent(query)
    requested_language, requested_extension = detect_requested_script_language(query)
    if requested_extension:
        script_quality = "deliverable"
    print(f"[CLASSIFY] script_quality={script_quality}")

    state = init_state(
        query,
        run_name,
        script_quality=script_quality,
        requested_language=requested_language,
        requested_extension=requested_extension,
    )

    state["environment"] = probe_environment(
        tools=["mafft", "iqtree", "Rscript"],
        python_packages=["Bio"],
    )

    staged = extract_and_stage_inputs(query)
    if staged:
        state["staged_inputs"] = staged
    refresh_file_inventory(state)

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

        if not extract_final_answer(text):
            print(text)

        # Protocol validation catches malformed model turns without treating
        # harmless mentions inside prose/code as runtime facts.
        if text_after_pause(text):
            state["failures"].append("text_after_pause")
            messages.append({"role": "model", "parts": [{"text": text}]})
            force_replan(state, "model wrote non-whitespace text after PAUSE", messages)
            continue

        if has_model_authored_action_response(text):
            state["failures"].append("model_invented_action_response")
            messages.append({"role": "model", "parts": [{"text": text}]})
            force_replan(state, "model authored an Action_Response protocol line; only the runtime may provide Action_Response", messages)
            continue

        final_answer = extract_final_answer(text)
        if final_answer:
            reason = final_answer_replan_reason(state, final_answer)
            if reason:
                state["failures"].append("script_only_final_answer")
                messages.append({"role": "model", "parts": [{"text": text}]})
                force_replan(state, reason, messages)
                continue
            return final_answer

        actions = extract_actions(text)
        if len(actions) > 1:
            state["failures"].append("multiple_actions")
            messages.append({"role": "model", "parts": [{"text": text}]})
            force_replan(state, f"model output contained {len(actions)} actions in one turn; output exactly one action before PAUSE", messages)
            continue

        action = actions[0] if actions else None
        if not action:
            state["failures"].append("parse_failure")
            messages.append({"role": "model", "parts": [{"text": text}]})
            force_replan(state, "model output did not contain a parseable action", messages)
            continue

        validation_error = validate_action_against_state(state, action)
        if validation_error:
            state["failures"].append("state_validation")
            state["last_error_type"] = "state_validation"
            messages.append({"role": "model", "parts": [{"text": text}]})
            force_replan(state, validation_error, messages)
            continue

        before_files = completed_file_set(state)
        result = dispatch_action(state, action)
        state["last_action_name"] = action.get("function_name")
        if action.get("function_name") == "read_file":
            state["last_read_file"] = (action.get("function_parms", {}) or {}).get("filename")
        print(f"Action_Response: {result}")
        update_completed_from_action_result(state, action, result)

        listing_after_action = None
        if action.get("function_name") in {"write_script", "import_file", "run_script", "run_rscript", "run_mafft", "run_iqtree"}:
            listing_after_action = refresh_file_inventory(state, label="FILES_AFTER_ACTION")

        output_validation_error = validate_expected_script_outputs(before_files, action, result, listing_after_action)
        if output_validation_error:
            state["failures"].append("script_output_validation")
            state["last_error_type"] = "script_output_validation"
            messages.append({"role": "model", "parts": [{"text": text}]})
            messages.append({"role": "user", "parts": [{"text": f"Action_Response: {result}"}]})
            force_replan(state, output_validation_error, messages)
            continue

        retry_signal = deterministic_retry(state, action, result)
        if retry_signal:
            if retry_signal.get("replan_reason"):
                print(f"[RECOVERY] {retry_signal.get('failure_class', 'execution_failure')}: {retry_signal['replan_reason']}")
                messages.append({"role": "model", "parts": [{"text": text}]})
                messages.append({"role": "user", "parts": [{"text": f"Action_Response: {result}"}]})
                force_replan(state, retry_signal["replan_reason"], messages)
                continue
            else:
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

