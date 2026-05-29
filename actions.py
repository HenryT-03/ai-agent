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
    ".bat": ["cmd.exe", "/c"],
    ".R": ["Rscript"],
}

_RUN_DIR: Optional[str] = None



# ---------------------------
# RUN / SESSION ACTIONS
# ---------------------------


def init_run() -> str:
    """Create one per-query run directory. Do not create typed subfolders."""
    global _RUN_DIR
    run_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    _RUN_DIR = os.path.join(WORKING_DIR, "runs", run_name)
    os.makedirs(_RUN_DIR, exist_ok=True)
    return run_name

def current_run_dir() -> Optional[str]:
    return _RUN_DIR



# ---------------------------
# PATH / SANDBOX UTILITIES
# ---------------------------


def resolve_path(filename: str) -> str:
    if not filename or not isinstance(filename, str):
        raise ValueError("filename must be a non-empty string")

    filename = filename.replace("\\", "/")

    if filename == "workspace":
        filename = ""
    elif filename.startswith("workspace/"):
        filename = filename[len("workspace/"):]

    if os.path.isabs(filename):
        abs_path = os.path.abspath(filename)
        if abs_path.startswith(os.path.abspath(WORKING_DIR) + os.sep) or abs_path == os.path.abspath(WORKING_DIR):
            return abs_path
        if abs_path.startswith(os.path.abspath(BASE_DIR) + os.sep) or abs_path == os.path.abspath(BASE_DIR):
            return abs_path
        raise ValueError(f"Absolute paths outside workspace are not allowed: {filename!r}")

    parts = [p for p in filename.split("/") if p]
    if ".." in parts:
        raise ValueError(f"Path escape blocked: {filename!r}")

    # Strip obsolete category prefixes.
    if parts and parts[0] in {"scripts", "results", "logs", "data"}:
        parts = parts[1:]

    # File listings and run banners include workspace/runs/<timestamp>/ for
    # readability. Treat that as a display prefix so model-provided paths do not
    # get nested under the active run a second time.
    if len(parts) >= 2 and parts[0] == "runs":
        parts = parts[2:]

    safe_name = "_".join(parts) if parts else ""
    base = _RUN_DIR or WORKING_DIR
    path = os.path.abspath(os.path.join(base, safe_name))

    if not path.startswith(os.path.abspath(WORKING_DIR) + os.sep) and path != os.path.abspath(WORKING_DIR):
        raise ValueError(f"Path escape blocked: {filename!r}")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path

def model_path(path: str) -> str:
    """Return a model-facing bare path relative to the active run directory."""
    abs_path = os.path.abspath(path)
    if _RUN_DIR:
        run_abs = os.path.abspath(_RUN_DIR)
        try:
            if os.path.commonpath([abs_path, run_abs]) == run_abs:
                return os.path.relpath(abs_path, run_abs)
        except ValueError:
            pass

    workspace_abs = os.path.abspath(WORKING_DIR)
    try:
        if os.path.commonpath([abs_path, workspace_abs]) == workspace_abs:
            rel = os.path.relpath(abs_path, workspace_abs)
            parts = rel.replace("\\", "/").split("/")
            if len(parts) >= 3 and parts[0] == "runs":
                return os.path.join(*parts[2:])
            return rel
    except ValueError:
        pass

    return os.path.basename(abs_path)

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

def validate_script_arg(arg: str) -> Optional[str]:
    """
    Reject shell metacharacters in arguments passed to generated shell/batch
    scripts. subprocess avoids shell parsing for Python scripts, but .bat is
    executed through cmd.exe and generated shell scripts may mishandle quoting.
    """
    if not isinstance(arg, str):
        return None

    unsafe_chars = set("&|;<>`")
    if any(ch in arg for ch in unsafe_chars) or "$(" in arg:
        return f"Unsafe shell metacharacter in script argument: {arg!r}"
    return None

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



# ---------------------------
# FILE ACTIONS / VALIDATION
# ---------------------------


def import_file(source_path: str, dest_filename: str = None):
    if not os.path.isabs(source_path):
        return {"stdout": "", "stderr": "source_path must be absolute", "returncode": 1}

    if not os.path.exists(source_path):
        return {"stdout": "", "stderr": f"Source file not found: {source_path}", "returncode": 1}

    dest_filename = dest_filename or os.path.basename(source_path)
    dest_path = resolve_path(dest_filename)

    import shutil
    shutil.copy2(source_path, dest_path)

    return {
        "stdout": f"Imported {source_path} to {model_path(dest_path)}",
        "stderr": "",
        "returncode": 0,
    }

def read_file(filename: str):
    try:
        path = resolve_path(filename)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

def list_files(directory: str = ""):
    """
    List files under the current run directory.
    Returns model-facing bare path, byte size, and empty-file marker.
    """
    try:
        if directory:
            base = resolve_path(directory)
        else:
            base = _RUN_DIR or WORKING_DIR

        if not os.path.exists(base):
            return f"Directory not found: {directory!r}"

        entries = []
        for fname in sorted(os.listdir(base)):
            full = os.path.join(base, fname)
            if not os.path.isfile(full):
                continue
            rel = model_path(full)
            size = os.path.getsize(full)
            entries.append(f"{rel}  ({size} bytes{'  [EMPTY]' if size == 0 else ''})")

        return "\n".join(entries) if entries else "No files found."
    except Exception as e:
        return f"Error listing files: {e}"

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

def remove_if_exists(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass



# ---------------------------
# BIOINFORMATICS ACTIONS
# ---------------------------


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

def run_mafft(input_fasta: str, output_fasta: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Run MAFFT alignment with pre/post validation and path normalization.
    
    - Validates input FASTA exists and is parsable
    - Normalizes paths for Windows->POSIX subprocess compatibility
    - Validates output is created, non-empty, and valid FASTA
    """
    try:
        input_path = resolve_path(input_fasta)
        output_path = resolve_path(output_fasta)
        
        # If the input file is outside the current run directory, import it
        # into the run directory to guarantee tool access and avoid path issues.
        try:
            run_dir_abs = os.path.abspath(_RUN_DIR) if _RUN_DIR else None
        except Exception:
            run_dir_abs = None

        if run_dir_abs:
            try:
                same = os.path.commonpath([os.path.abspath(input_path), run_dir_abs]) == run_dir_abs
            except Exception:
                same = False
        else:
            same = False

        if not same:
            imp = import_file(input_path)
            if not isinstance(imp, dict) or imp.get("returncode", 1) != 0:
                return {"stdout": "", "stderr": f"Failed to import input file: {imp.get('stderr', str(imp))}", "returncode": 1}
            # Now point input_path to the imported copy inside the run dir
            input_path = resolve_path(os.path.basename(input_path))

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
        temp_output_path = output_path + ".tmp"
        remove_if_exists(temp_output_path)
        
        mafft_path = resolve_executable("mafft")
        if not mafft_path:
            return {"stdout": "", "stderr": "mafft not found in PATH", "returncode": 1}

        mafft_input = normalized_input

        if mafft_path.lower().endswith((".bat", ".cmd")):
            cmd = ["cmd.exe", "/c", mafft_path, "--auto", mafft_input]
        else:
            cmd = [mafft_path, "--auto", mafft_input]

        if args:
            cmd.extend(args)
        
        # Write to a temp artifact first. A failed external command must not
        # clobber an existing valid final output with a 0-byte redirect target.
        with open(temp_output_path, "w", encoding="utf-8") as out_f:
            result = subprocess.run(
                cmd,
                stdout=out_f,
                stderr=subprocess.PIPE,
                text=True,
                timeout=600,
                cwd=_RUN_DIR or WORKING_DIR,
            )

        if result.returncode != 0:
            remove_if_exists(temp_output_path)
            return {"stdout": result.stdout or "", "stderr": result.stderr or "", "returncode": result.returncode}
        
        # Post-execution validation
        err = validate_output_file(temp_output_path, allow_empty=False)
        if err:
            remove_if_exists(temp_output_path)
            return {"stdout": "", "stderr": err, "returncode": 1}
        
        if not is_fasta_file(temp_output_path):
            remove_if_exists(temp_output_path)
            return {"stdout": "", "stderr": f"Output is not a valid FASTA file: {temp_output_path}", "returncode": 1}
        
        output_count = count_fasta_records(temp_output_path)
        if output_count != input_count:
            remove_if_exists(temp_output_path)
            return {
                "stdout": "",
                "stderr": f"MAFFT record count mismatch: input={input_count}, output={output_count}",
                "returncode": 1,
            }

        os.replace(temp_output_path, output_path)
        
        return {
            "stdout": f"MAFFT alignment complete: {model_path(output_path)} ({output_count} sequences)",
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
    - Normalizes paths for Windows->POSIX subprocess compatibility
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
        if not input_count or input_count < 4:
            return {"stdout": "", "stderr": f"Bootstrap requires at least 4 sequences (found {input_count}). Add more sequences or pass -bb 0 to disable.", "returncode": 1}
        
        # Ensure output dir exists
        ensure_parent_dirs(output_prefix_path)
        
        # Build command with normalized paths
        normalized_input = normalize_path_for_subprocess(input_path)
        temp_prefix_path = output_prefix_path + ".tmp"
        normalized_prefix = normalize_path_for_subprocess(temp_prefix_path)
        final_treefile = output_prefix_path + ".treefile"
        temp_treefile = temp_prefix_path + ".treefile"

        # Clean stale temp files only. Final outputs are preserved until the
        # temp run has succeeded and passed validation.
        parent = os.path.dirname(temp_prefix_path) or "."
        temp_base = os.path.basename(temp_prefix_path)
        if os.path.isdir(parent):
            for fname in os.listdir(parent):
                if fname.startswith(temp_base):
                    remove_if_exists(os.path.join(parent, fname))
        
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
        if result.returncode == 0 and not os.path.exists(temp_treefile):
            if os.path.isdir(parent):
                for fname in os.listdir(parent):
                    if fname.startswith(temp_base):
                        remove_if_exists(os.path.join(parent, fname))
            return {
                "stdout": "",
                "stderr": f"IQ-TREE exited with code 0 but treefile not found: {temp_treefile}",
                "returncode": 1,
            }
        
        if result.returncode == 0:
            err = validate_output_file(temp_treefile, allow_empty=False)
            if err:
                if os.path.isdir(parent):
                    for fname in os.listdir(parent):
                        if fname.startswith(temp_base):
                            remove_if_exists(os.path.join(parent, fname))
                return {"stdout": "", "stderr": err, "returncode": 1}

            if os.path.isdir(parent):
                for fname in os.listdir(parent):
                    if not fname.startswith(temp_base):
                        continue
                    src = os.path.join(parent, fname)
                    suffix = fname[len(temp_base):]
                    dest = output_prefix_path + suffix
                    os.replace(src, dest)
            
            return {
                "stdout": f"IQ-TREE tree inference complete: {model_path(final_treefile)}",
                "stderr": result.stderr or "",
                "returncode": 0,
            }
        else:
            if os.path.isdir(parent):
                for fname in os.listdir(parent):
                    if fname.startswith(temp_base):
                        remove_if_exists(os.path.join(parent, fname))
            return {
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "returncode": result.returncode,
            }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "IQ-TREE timed out after 1200 seconds", "returncode": 1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": 1}



# ---------------------------
# SCRIPT ACTIONS
# ---------------------------


def write_script(filename: str, code: str):
    path = resolve_path(filename)
    with open(path, "w", newline="\n", encoding="utf-8") as f:
        f.write(code)
    return f"Script written to {model_path(path)}"

def run_script(
    filename: str,
    interpreter: Optional[str] = None,
    timeout: int = 300,
    args: Optional[List[str]] = None,
    expected_outputs: Optional[List[str]] = None,
):
    """
    Run a script from the sandbox. Returns stdout, stderr, and returncode.
    
    Pre-execution checks:
    - Validate input file args exist
    - Normalize all paths to POSIX for subprocess
    - Create parent dirs for output args

    expected_outputs is accepted by the action schema for main.py postcondition
    checks; script execution itself does not consume it.
    """
    try:
        raw_ext = os.path.splitext(filename)[1].lower() if isinstance(filename, str) else ""
        raw_base = os.path.basename(str(filename)).lower()
        if isinstance(filename, str) and os.path.isabs(filename) and (
            raw_ext in {".exe", ".com"} or raw_base.startswith(("mafft", "iqtree"))
        ):
            return {
                "stdout": "",
                "stderr": (
                    "run_script executes scripts, not absolute external tool binaries. "
                    "For internal bioinformatics execution use run_mafft/run_iqtree; "
                    "for a deliverable, write and test a script artifact instead."
                ),
                "returncode": 1,
            }

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
            # Shell and batch scripts: pass basename only since cwd is already the run directory.
            # Passing an absolute Windows path to bash/cmd causes path mangling issues.
            if (ext in (".sh", ".bat")) and _RUN_DIR:
                script_arg = os.path.basename(path)
            else:
                script_arg = path
            cmd = base_cmd + [script_arg]

        processed_args = []
        if args:
            for arg in args:
                if ext in (".sh", ".bat"):
                    err = validate_script_arg(arg)
                    if err:
                        return {"stdout": "", "stderr": err, "returncode": 1}

                if isinstance(arg, str) and (os.sep in arg or "/" in arg or "\\" in arg or os.path.splitext(arg)[1]):
                    routed_arg = route_arg(arg)
                    normalized_arg = normalize_path_for_subprocess(routed_arg)
                    processed_args.append(normalized_arg)
                    
                    # Existing file-like args are inputs and must be readable.
                    # Missing file-like args are allowed so scripts can create
                    # named outputs, which the runtime validates after exit.
                    if not arg.startswith("-") and os.path.exists(routed_arg):
                        err = validate_input_file(routed_arg)
                        if err:
                            return {"stdout": "", "stderr": err, "returncode": 1}
                else:
                    processed_args.append(arg)
            cmd.extend(processed_args)

        if ext == ".sh" or ext == ".bat":
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

        stderr = result.stderr or ""
        if result.returncode != 0 and expected_outputs:
            zeroed = []
            for output in expected_outputs:
                if not isinstance(output, str) or not output.strip():
                    continue
                try:
                    out_path = resolve_path(output)
                    if os.path.exists(out_path) and os.path.getsize(out_path) == 0:
                        zeroed.append(model_path(out_path))
                except Exception:
                    continue
            if zeroed:
                warning = "Expected output(s) became 0 bytes after failed script: " + ", ".join(zeroed)
                stderr = f"{stderr}\n{warning}".strip()

        return {"stdout": stdout, "stderr": stderr, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Script timed out after {timeout} seconds", "returncode": 1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": 1}


def run_rscript(
    script_path: str,
    args: Optional[List[str]] = None,
    expected_outputs: Optional[List[str]] = None,
    timeout: int = 300,
) -> Dict[str, Any]:
    """
    Execute an R script with pre/post validation.

    Use this instead of run_script for .R files so R failures and output
    postconditions are reported consistently.
    """
    try:
        path = resolve_path(script_path)
        err = validate_input_file(path)
        if err:
            return {"stdout": "", "stderr": err, "returncode": 1}

        rscript = resolve_executable("Rscript")
        if not rscript:
            return {"stdout": "", "stderr": "Rscript not found in PATH", "returncode": 1}

        cmd = [rscript, normalize_path_for_subprocess(path)]
        if args:
            for arg in args:
                if isinstance(arg, str) and (
                    os.sep in arg or "/" in arg or "\\" in arg or os.path.splitext(arg)[1]
                ):
                    cmd.append(normalize_path_for_subprocess(route_arg(arg)))
                else:
                    cmd.append(arg)

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            cwd=_RUN_DIR or WORKING_DIR,
        )

        stderr = result.stderr or ""
        if expected_outputs:
            for output in expected_outputs:
                if not isinstance(output, str) or not output.strip():
                    continue
                out_path = resolve_path(output)
                rel = model_path(out_path)
                if result.returncode == 0:
                    err = validate_output_file(out_path, allow_empty=False)
                    if err:
                        return {"stdout": result.stdout or "", "stderr": err, "returncode": 1}
                elif os.path.exists(out_path) and os.path.getsize(out_path) == 0:
                    warning = f"Expected output became 0 bytes after failed Rscript: {rel}"
                    stderr = f"{stderr}\n{warning}".strip()

        return {
            "stdout": result.stdout or "",
            "stderr": stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Rscript timed out after {timeout}s", "returncode": 1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": 1}



# ---------------------------
# ENVIRONMENT / DEPENDENCY ACTIONS
# ---------------------------


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
    import platform as platform_module
    
    tools = tools or []
    python_packages = python_packages or []
    r_packages = r_packages or []

    env_info = {
        "platform": platform_module.system(),  # "Windows", "Linux", "Darwin"
        "tools": {
            tool: probe_executable(tool)
            for tool in tools
        },
        "python_packages": {
            package: probe_python_package(package)
            for package in python_packages
        },
        "r_packages": {
            package: probe_r_package(package)
            for package in r_packages
        },
    }
    
    # Detect if tools are reachable via cmd.exe (Windows) vs bash (Unix)
    # If any tool path ends in .bat or .cmd, we're in Windows cmd-land
    tool_is_windows_native = False
    for tool_info in env_info["tools"].values():
        if isinstance(tool_info, dict):
            path = tool_info.get("path", "")
            if path and path.lower().endswith((".bat", ".cmd")):
                tool_is_windows_native = True
                break
    
    if env_info["platform"] == "Windows" or tool_is_windows_native:
        env_info["script_format"] = "bat"  # Recommend .bat for Windows
    else:
        env_info["script_format"] = "sh"   # Recommend .sh for Unix
    
    return env_info

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

def probe_r_package(package: str):
    script = f'cat(requireNamespace("{package}", quietly=TRUE))'
    try:
        result = subprocess.run(
            ["Rscript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return {"installed": result.stdout.strip() == "TRUE"}
    except Exception as e:
        return {"installed": False, "reason": str(e)}

def resolve_executable(tool: str) -> Optional[str]:
    import shutil
    return shutil.which(tool)
