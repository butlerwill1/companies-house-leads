from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import anyio


LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "run_companies_house_mcp.py"


def test_launcher_checks_database_from_another_working_directory(seeded_db_path, tmp_path):
    result = subprocess.run([sys.executable, str(LAUNCHER), "--check"], cwd=tmp_path,
        env={**os.environ, "COMPANIES_HOUSE_DB": str(seeded_db_path)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '"read_only": true' in result.stdout
    assert '"total": 3' in result.stdout


def test_launcher_missing_database_fails_without_creating_it(tmp_path):
    missing = tmp_path / "missing.db"
    result = subprocess.run([sys.executable, str(LAUNCHER), "--check"], cwd=tmp_path,
        env={**os.environ, "COMPANIES_HOUSE_DB": str(missing)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "Database not found" in result.stderr
    assert not missing.exists()


def test_stdio_launcher_initialises_and_serves_queries(seeded_db_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable,
            args=[str(LAUNCHER), "--db", str(seeded_db_path)],
            env={**os.environ, "COMPANIES_HOUSE_DB": str(seeded_db_path)})
        with anyio.fail_after(20):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert all(tool.annotations.readOnlyHint for tool in tools.tools)
                    summary = await session.call_tool("get_lead_pipeline_summary", {})
                    assert not summary.isError
                    assert summary.structuredContent["lead_counts"]["total"] == 3
                    evidence = await session.call_tool("get_company_web_evidence", {"company_number": "13406761"})
                    assert not evidence.isError
                    assert evidence.structuredContent["coverage"]["market_assessed"] is False

    anyio.run(run)
