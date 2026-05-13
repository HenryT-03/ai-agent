import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKING_DIR = os.path.join(BASE_DIR, "workspace")
os.makedirs(WORKING_DIR, exist_ok=True)
os.makedirs(os.path.join(WORKING_DIR, "runs"), exist_ok=True)

INTERPRETERS = {
    ".py": ["python"],
    ".sh": ["bash"],
    ".R": ["Rscript"],
}

_RUN_DIR: Optional[str] = None


# ---------------------------
# UTILITIES: Path Normalization, Validation, Output Checks
# ---------------------------

def normalize_path_for_subprocess(path: str) -> str:
    """
    Convert Windows absolute paths to POSIX for subprocess execution.
    
    Some tools (especially Unix wrappers on Windows like MAFFT) fail with
    Windows-style paths. Convert C:\\Users\\... to /c/Users/... for WSL-style
    or keep as-is if already POSIX. Always use forward slashes internally.
    """
    if not path or not isinstance(path, str):
        return path
    
    # Already POSIX
    if "/" in path and "\\" not in path:
        return path
    
    # Convert Windows path to POSIX-like format
    p = Path(path)
    posix_str = p.as_posix()
    return posix_str


def ensure_parent_dirs(path: str) -> None:
    """Create parent directories if they don't exist."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def validate_input_file(path: str) -> Optional[str]:
    """
    Check if an input file exists and is readable.
    Return error message if invalid, None if OK.
    """
    if not os.path.exists(path):
        return f"Input file not found: {path}"
    if not os.path.isfile(path):
        return f"Not a file: {path}"
    if not os.access(path, os.R_OK):
        return f"File not readable: {path}"
    return None


def validate_output_file(path: str, allow_empty: bool = False) -> Optional[str]:
    """
    Check if an output file exists, is readable, and is non-empty.
    Return error message if invalid, None if OK.
    """
    if not os.path.exists(path):
        return f"Output file not created: {path}"
    if not os.path.isfile(path):
        return f"Not a file: {path}"
    size = os.path.getsize(path)
    if size == 0 and not allow_empty:
        return f"Output file is empty: {path}"
    return None


def is_fasta_file(path: str) -> bool:
    """Quick check: file starts with '>' and has multiple lines."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            line = f.readline().strip()
            return line.startswith(">")
    except Exception:
        return False


def count_fasta_records(path: str) -> Optional[int]:
    """Count '>' lines in a FASTA file. Return None if invalid."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for line in f if line.startswith(">"))
    except Exception:
        return None


def init_run() -> str:
    """Create one per-query run directory. Do not create typed subfolders."""
    global _RUN_DIR
    run_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    _RUN_DIR = os.path.join(WORKING_DIR, "runs", run_name)
    os.makedirs(_RUN_DIR, exist_ok=True)
    return run_name


def current_run_dir() -> Optional[str]:
    return _RUN_DIR


def resolve_path(filename: str) -> str:
    if not filename or not isinstance(filename, str):
        raise ValueError("filename must be a non-empty string")

    filename = filename.replace("\\", "/")

    if filename == "workspace":
        filename = ""
    elif filename.startswith("workspace/"):
        filename = filename[len("workspace/"):]

    if os.path.isabs(filename):
        raise ValueError(f"Absolute paths are not allowed: {filename!r}")

    parts = [p for p in filename.split("/") if p]
    if ".." in parts:
        raise ValueError(f"Path escape blocked: {filename!r}")

    # Strip obsolete category prefixes.
    if parts and parts[0] in {"scripts", "results", "logs", "data"}:
        parts = parts[1:]

    safe_name = "_".join(parts) if parts else ""
    base = _RUN_DIR or WORKING_DIR
    path = os.path.abspath(os.path.join(base, safe_name))

    if not path.startswith(WORKING_DIR + os.sep) and path != WORKING_DIR:
        raise ValueError(f"Path escape blocked: {filename!r}")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path

def write_script(filename: str, code: str):
    path = resolve_path(filename)
    with open(path, "w", newline="\n", encoding="utf-8") as f:
        f.write(code)
    return f"Script written to {os.path.relpath(path, WORKING_DIR)}"

def route_arg(arg: str) -> str:
    if not isinstance(arg, str):
        return arg

    # Leave flags/options alone.
    if arg.startswith("-"):
        return arg

    # Leave URLs or obvious non-file values alone.
    if "://" in arg:
        return arg

    # Route likely file paths.
    if (
        os.sep in arg
        or "/" in arg
        or "\\" in arg
        or os.path.splitext(arg)[1]
    ):
        return resolve_path(arg)

    return arg

def run_script(filename: str, interpreter: Optional[str] = None, timeout: int = 300, args: Optional[List[str]] = None):
    """
    Run a script from the sandbox. Returns stdout, stderr, and returncode.
    
    Pre-execution checks:
    - Validate input file args exist
    - Normalize all paths to POSIX for subprocess
    - Create parent dirs for output args
    """
    try:
        path = resolve_path(filename)
        ext = os.path.splitext(path)[1]
        rel_path = os.path.relpath(path, WORKING_DIR)

        if interpreter:
            cmd = [interpreter, path]
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
            cmd = base_cmd + [path]

        processed_args = []
        if args:
            for arg in args:
                if isinstance(arg, str) and (os.sep in arg or "/" in arg or "\\" in arg or os.path.splitext(arg)[1]):
                    routed_arg = route_arg(arg)
                    normalized_arg = normalize_path_for_subprocess(routed_arg)
                    processed_args.append(normalized_arg)
                    
                    # If it looks like an input file, validate existence
                    if not arg.startswith("-"):
                        err = validate_input_file(routed_arg)
                        if err:
                            return {"stdout": "", "stderr": err, "returncode": 1}
                else:
                    processed_args.append(arg)
            cmd.extend(processed_args)

        if ext == ".sh":
            os.chmod(path, 0o755)

        result = subprocess.run(
            cmd,
            cwd=_RUN_DIR or WORKING_DIR,
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


def run_mafft(input_fasta: str, output_fasta: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Run MAFFT alignment with pre/post validation and path normalization.
    
    - Validates input FASTA exists and is parsable
    - Normalizes paths for Windows→POSIX subprocess compatibility
    - Validates output is created, non-empty, and valid FASTA
    """
    try:
        input_path = resolve_path(input_fasta)
        output_path = resolve_path(output_fasta)
        
        # Pre-execution validation
        err = validate_input_file(input_path)
        if err:
            return {"stdout": "", "stderr": err, "returncode": 1}
        
        if not is_fasta_file(input_path):
            return {"stdout": "", "stderr": f"Input is not a valid FASTA file: {input_path}", "returncode": 1}
        
        input_count = count_fasta_records(input_path)
        if not input_count or input_count == 0:
            return {"stdout": "", "stderr": f"Input FASTA has no records: {input_path}", "returncode": 1}
        
        # Ensure output dir exists
        ensure_parent_dirs(output_path)
        
        # Build command with normalized paths
        normalized_input = normalize_path_for_subprocess(input_path)
        normalized_output = normalize_path_for_subprocess(output_path)
        
        cmd = ["mafft", "--auto", normalized_input]
        if args:
            cmd.extend(args)
        
        # Redirect output to file
        with open(output_path, "w", encoding="utf-8") as out_f:
            result = subprocess.run(
                cmd,
                stdout=out_f,
                stderr=subprocess.PIPE,
                text=True,
                timeout=600,
                cwd=_RUN_DIR or WORKING_DIR,
            )
        
        # Post-execution validation
        err = validate_output_file(output_path, allow_empty=False)
        if err:
            return {"stdout": "", "stderr": err, "returncode": 1}
        
        if not is_fasta_file(output_path):
            return {"stdout": "", "stderr": f"Output is not a valid FASTA file: {output_path}", "returncode": 1}
        
        output_count = count_fasta_records(output_path)
        if output_count != input_count:
            return {
                "stdout": "",
                "stderr": f"MAFFT record count mismatch: input={input_count}, output={output_count}",
                "returncode": 1,
            }
        
        rel_output = os.path.relpath(output_path, WORKING_DIR)
        return {
            "stdout": f"MAFFT alignment complete: {rel_output} ({output_count} sequences)",
            "stderr": result.stderr or "",
            "returncode": 0,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "MAFFT timed out after 600 seconds", "returncode": 1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": 1}


def run_iqtree(input_fasta: str, output_prefix: str = "results/tree", args: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Run IQ-TREE with pre/post validation and path normalization.
    
    - Validates input FASTA exists and is parsable
    - Normalizes paths for Windows→POSIX subprocess compatibility
    - Validates tree file is created
    """
    try:
        input_path = resolve_path(input_fasta)
        output_prefix_path = resolve_path(output_prefix)
        
        # Pre-execution validation
        err = validate_input_file(input_path)
        if err:
            return {"stdout": "", "stderr": err, "returncode": 1}
        
        if not is_fasta_file(input_path):
            return {"stdout": "", "stderr": f"Input is not a valid FASTA file: {input_path}", "returncode": 1}
        
        input_count = count_fasta_records(input_path)
        if not input_count or input_count < 3:
            return {"stdout": "", "stderr": f"Input FASTA needs at least 3 sequences for tree building (found {input_count})", "returncode": 1}
        
        # Ensure output dir exists
        ensure_parent_dirs(output_prefix_path)
        
        # Build command with normalized paths
        normalized_input = normalize_path_for_subprocess(input_path)
        normalized_prefix = normalize_path_for_subprocess(output_prefix_path)
        
        cmd = ["iqtree", "-s", normalized_input, "-m", "TEST", "-bb", "1000", "-T", "AUTO", "-pre", normalized_prefix]
        if args:
            cmd.extend(args)
        
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1200,
            cwd=_RUN_DIR or WORKING_DIR,
        )
        
        # Post-execution validation: check for treefile
        treefile = output_prefix_path + ".treefile"
        if result.returncode == 0 and not os.path.exists(treefile):
            return {
                "stdout": "",
                "stderr": f"IQ-TREE exited with code 0 but treefile not found: {treefile}",
                "returncode": 1,
            }
        
        if result.returncode == 0:
            err = validate_output_file(treefile, allow_empty=False)
            if err:
                return {"stdout": "", "stderr": err, "returncode": 1}
            
            rel_treefile = os.path.relpath(treefile, WORKING_DIR)
            return {
                "stdout": f"IQ-TREE tree inference complete: {rel_treefile}",
                "stderr": result.stderr or "",
                "returncode": 0,
            }
        else:
            return {
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "returncode": result.returncode,
            }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "IQ-TREE timed out after 1200 seconds", "returncode": 1}
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


def probe_environment(
    tools=None,
    python_packages=None,
    r_packages=None,
):
    tools = tools or []
    python_packages = python_packages or []
    r_packages = r_packages or []

    return {
        "tools": {
            tool: probe_executable(tool)
            for tool in tools
        },
        "python_packages": {
            package: probe_python_package(package)
            for package in python_packages
        },
        "r_packages": {
            package: {"installed": None, "reason": "not implemented yet"}
            for package in r_packages
        },
    }

def probe_executable(tool: str, test_args=None, timeout: int = 10):
    import shutil
    import subprocess

    path = shutil.which(tool)
    if not path:
        return {
            "found": False,
            "invokable": False,
            "path": None,
            "reason": "not found in PATH",
        }

    try:
        result = subprocess.run(
            [path] + (test_args or ["--help"]),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return {
            "found": True,
            "invokable": result.returncode == 0,
            "path": path,
            "returncode": result.returncode,
            "stdout_preview": (result.stdout or "")[:500],
            "stderr_preview": (result.stderr or "")[:500],
        }

    except Exception as e:
        return {
            "found": True,
            "invokable": False,
            "path": path,
            "reason": str(e),
        }
    
def probe_python_package(package: str):
    import importlib.util

    return {
        "installed": importlib.util.find_spec(package) is not None
    }

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
