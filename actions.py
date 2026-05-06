import os
import subprocess
from datetime import datetime
from typing import List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKING_DIR = os.path.join(BASE_DIR, "workspace")
os.makedirs(WORKING_DIR, exist_ok=True)
os.makedirs(os.path.join(WORKING_DIR, "data"), exist_ok=True)
os.makedirs(os.path.join(WORKING_DIR, "runs"), exist_ok=True)

# Extension-driven execution keeps the action surface small.
# Default support is intentionally narrow:
#   .py -> Python for parsing, analysis, orchestration
#   .sh -> shell for CLI pipelines
#   .R  -> R only when requested or when R-specific bioinformatics packages are useful
# Other languages can still be run with run_script(..., interpreter="...") if explicitly requested
# and installed, but they are not advertised as default choices.
INTERPRETERS = {
    ".py": ["python"],
    ".sh": ["bash"],
    ".R": ["Rscript"],
}

_RUN_DIR: Optional[str] = None


def init_run() -> str:
    """Create a per-query run directory and route new scripts/logs/results there."""
    global _RUN_DIR
    run_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    _RUN_DIR = os.path.join(WORKING_DIR, "runs", run_name)
    for subdir in ("logs", "results"):
        os.makedirs(os.path.join(_RUN_DIR, subdir), exist_ok=True)
    return run_name


def current_run_dir() -> Optional[str]:
    return _RUN_DIR


def resolve_path(filename: str) -> str:
    """
    Resolve a model-provided filename into the workspace sandbox.

    Routing:
      scripts/... -> current run directory root
      logs/... -> current run directory logs/
      results/... -> current run directory results/
      data/... -> shared workspace/data
      everything else -> current run directory root

    The model should not include workspace/, runs/, run ids, absolute paths, or ...
    This function still strips common accidental prefixes and blocks escapes.
    """
    if not filename or not isinstance(filename, str):
        raise ValueError("filename must be a non-empty string")

    filename = filename.replace("\\", os.sep)

    if filename.startswith(WORKING_DIR + os.sep):
        filename = os.path.relpath(filename, WORKING_DIR)
    elif filename == "workspace":
        filename = ""
    elif filename.startswith("workspace" + os.sep):
        filename = filename[len("workspace") + 1:]

    if os.path.isabs(filename):
        raise ValueError(f"Absolute paths are not allowed: {filename!r}")

    parts = filename.split(os.sep)
    if ".." in parts:
        raise ValueError(f"Path escape blocked: {filename!r}")

    if filename.startswith("scripts" + os.sep):
        filename = filename[len("scripts") + 1:]
        base = _RUN_DIR or WORKING_DIR
    elif filename.startswith("logs" + os.sep):
        filename = filename[len("logs") + 1:]
        base = os.path.join(_RUN_DIR or WORKING_DIR, "logs")
    elif filename.startswith("results" + os.sep):
        filename = filename[len("results") + 1:]
        base = os.path.join(_RUN_DIR or WORKING_DIR, "results")
    elif filename.startswith("data" + os.sep):
        base = os.path.join(WORKING_DIR, "data")
    else:
        base = _RUN_DIR or WORKING_DIR

    path = os.path.abspath(os.path.join(base, filename))

    if not path.startswith(WORKING_DIR + os.sep) and path != WORKING_DIR:
        raise ValueError(f"Path escape blocked: {filename!r}")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def write_script(filename: str, code: str):
    path = resolve_path(filename)
    with open(path, "w", newline="\n", encoding="utf-8") as f:
        f.write(code)
    return f"Script written to {os.path.relpath(path, WORKING_DIR)}"


def run_script(filename: str, interpreter: Optional[str] = None, timeout: int = 300, args: Optional[List[str]] = None):
    """
    Run a script from the sandbox. Returns stdout, stderr, and returncode.
    Relative paths inside scripts are anchored to workspace/.
    """
    try:
        path = resolve_path(filename)
        ext = os.path.splitext(path)[1]
        rel_path = os.path.relpath(path, WORKING_DIR)

        if interpreter:
            cmd = [interpreter, rel_path]
        else:
            base_cmd = INTERPRETERS.get(ext)
            if not base_cmd:
                return {
                    "stdout": "",
                    "stderr": (
                        f"No default interpreter for extension {ext!r}. "
                        f"Default supported extensions: {sorted(INTERPRETERS)}. "
                        "If the user explicitly requested this language and its runtime is installed, "
                        "rerun with the interpreter override."
                    ),
                    "returncode": 1,
                }
            cmd = base_cmd + [rel_path]

        if args:
            cmd.extend(args)

        if ext == ".sh":
            os.chmod(path, 0o755)

        result = subprocess.run(
            cmd,
            cwd=WORKING_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = result.stdout or ""
        if not stdout and result.returncode == 0:
            stdout = "(no stdout)"
        return {"stdout": stdout, "stderr": result.stderr or "", "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Script timed out after {timeout} seconds", "returncode": 1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": 1}


def read_file(filename: str):
    try:
        path = resolve_path(filename)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"


def install_dependency(package: str, language: str = "python"):
    """
    Install a Python or R dependency only after a missing-dependency failure.

    language: "python" | "r"
    """
    language = language.lower().strip()
    try:
        if language in ("python", "py", "pip"):
            result = subprocess.run(
                ["python", "-m", "pip", "install", package],
                capture_output=True,
                text=True,
                timeout=180,
            )
        elif language in ("r", "cran"):
            script = f'install.packages("{package}", repos="https://cran.r-project.org")'
            result = subprocess.run(
                ["Rscript", "-e", script],
                capture_output=True,
                text=True,
                timeout=180,
            )
        else:
            return {"stdout": "", "stderr": f"Unsupported dependency language: {language}", "returncode": 1}

        return {"stdout": result.stdout or "", "stderr": result.stderr or "", "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Dependency install timed out after 180 seconds", "returncode": 1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": 1}


def check_tool(tool: str):
    import shutil

    path = shutil.which(tool)
    if path:
        return f"{tool} found at {path}"
    return f"{tool} not found in PATH. Install it or add its executable directory to PATH."


def list_files(directory: str = ""):
    """
    List files under the current run directory, or a routed workspace subdirectory.
    Returns relative path, byte size, and empty-file marker.
    """
    try:
        if directory:
            base = resolve_path(directory)
        else:
            base = _RUN_DIR or WORKING_DIR

        if not os.path.exists(base):
            return f"Directory not found: {directory!r}"

        entries = []
        for root, _dirs, files in os.walk(base):
            for fname in sorted(files):
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, WORKING_DIR)
                size = os.path.getsize(full)
                entries.append(f"{rel}  ({size} bytes{'  [EMPTY]' if size == 0 else ''})")

        return "\n".join(entries) if entries else "No files found."
    except Exception as e:
        return f"Error listing files: {e}"
