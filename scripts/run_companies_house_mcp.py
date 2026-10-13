"""Launch the local MCP server using a repo virtual environment, from any cwd."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    # Prefer the handover's .venv; retain the existing development environment.
    # Re-exec rather than spawning another process, so stdin/stdout stay MCP pipes.
    for directory in (".venv", ".venv-claude"):
        executable = REPO_ROOT / directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if executable.is_file():
            # Compare environment prefixes, not resolved binaries: Unix venv
            # executables are often symlinks to the same system interpreter.
            if Path(sys.prefix).resolve() != (REPO_ROOT / directory).resolve():
                os.execv(str(executable), [str(executable), str(Path(__file__).resolve()), *sys.argv[1:]])
            break
    if importlib.util.find_spec("mcp") is None:
        print("MCP dependencies are missing. From the repository run: python -m venv .venv; "
              "then install requirements.txt using .venv's Python. See docs/MCP_SETUP.md.", file=sys.stderr)
        return 1
    sys.path.insert(0, str(REPO_ROOT))
    from companies_house_mcp.service import CompaniesHouseDataService

    db_path = Path(os.environ.get("COMPANIES_HOUSE_DB", str(REPO_ROOT / "companies-house.db")))
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    if sys.argv[1:] == ["--check"]:
        try:
            summary = CompaniesHouseDataService(db_path).get_lead_pipeline_summary()
        except (OSError, sqlite3.Error) as exc:
            print(f"MCP setup check failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"database": str(db_path), "read_only": True, "summary": summary}, indent=2))
        return 0
    from companies_house_mcp.server import main as server_main

    return server_main(["--db", str(db_path), *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
