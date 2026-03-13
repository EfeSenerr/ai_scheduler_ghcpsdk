"""
Launcher for AI Scheduler — Multi-Page Change Monitor.

Builds into a standalone .exe via PyInstaller.
Activates the venv, runs checkPageChanges.py, streams output to the console,
and waits for a keypress before closing so you can read the results.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    # Resolve paths relative to the exe/script location
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).parent

    script = base_dir / "checkPageChanges.py"
    venv_python = base_dir / ".venv" / "Scripts" / "python.exe"
    pages_file = base_dir / "pages.json"

    if not script.exists():
        print(f"ERROR: {script} not found.")
        input("\nPress Enter to close...")
        sys.exit(1)

    if not venv_python.exists():
        print(f"ERROR: Virtual environment not found at {venv_python}")
        print("Run:  uv venv .venv && uv pip install -r requirements.txt")
        input("\nPress Enter to close...")
        sys.exit(1)

    # Count enabled pages
    page_count = 0
    if pages_file.exists():
        with open(pages_file, encoding="utf-8") as f:
            pages = json.load(f)
        page_count = sum(1 for p in pages if p.get("enabled", True))

    print("=" * 60)
    print("  AI Scheduler — Page Change Monitor")
    if page_count:
        print(f"  Monitoring {page_count} page(s)")
    print("=" * 60)
    print()

    # Run the agent script with the venv Python, streaming output live
    env = {**os.environ, "VIRTUAL_ENV": str(base_dir / ".venv")}
    process = subprocess.run(
        [str(venv_python), str(script)],
        cwd=str(base_dir),
        env=env,
    )

    print()
    print("=" * 60)
    if process.returncode == 0:
        print("  Agent finished successfully.")
    else:
        print(f"  Agent exited with code {process.returncode}.")
    print("=" * 60)

    input("\nPress Enter to close...")


if __name__ == "__main__":
    main()
