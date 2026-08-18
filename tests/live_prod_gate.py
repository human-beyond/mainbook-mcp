"""D8 live gate: a real bank statement through the MCP tool against production.

Seeds a throwaway account on api.mainbook.ai, mints one API key, drives the
stdio MCP server exactly the way Claude Desktop would, converts the Chase
regression fixture, and asserts the ending balance against the value the
engine has been held to since D2 ($423.36 — the number an earlier model
hallucinated as $9,423.36).

Never prints the admin secret or the API key. Cleans up in a finally block.

This remains a manual, production-mutating gate and is never part of pytest collection.
It requires the private Django checkout and its `.env`:

    MAINBOOK_BACKEND_ROOT=../mainbook-dev/backend .venv/bin/python tests/live_prod_gate.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx2 as httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

API = "https://api.mainbook.ai/api/v1"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
BACKEND = Path(
    os.environ.get("MAINBOOK_BACKEND_ROOT", PACKAGE_ROOT.parent / "mainbook-dev" / "backend")
).expanduser().resolve()
FIXTURE = BACKEND / "apps/documents/tests/fixtures/real/real_chase_2026-05_business.pdf"
EXPECTED_ENDING_BALANCE_CENTS = 42336

TEST_EMAIL = "d8-mcp-live@example.com"
TEST_PASSWORD = "D8-mcp-live-Passw0rd!"


def admin_secret() -> str:
    for line in (BACKEND / ".env").read_text().splitlines():
        if line.startswith("INTERNAL_ADMIN_SECRET="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("INTERNAL_ADMIN_SECRET not found in backend/.env")


async def main() -> int:
    secret = admin_secret()
    if not FIXTURE.is_file():
        raise SystemExit(f"fixture missing: {FIXTURE}")

    api_key: str | None = None
    key_id: str | None = None
    async with httpx.AsyncClient(timeout=60.0) as http:
        try:
            seeded = await http.post(
                f"{API}/internal/seed-user/",
                headers={"X-Admin-Secret": secret},
                json={
                    "email": TEST_EMAIL,
                    "password": TEST_PASSWORD,
                    "verified": True,
                    "quiz_completed": True,
                    "purge": True,
                },
            )
            print(f"seed-user: {seeded.status_code}")
            if seeded.status_code != 200:
                print(seeded.text[:400])
                return 1

            login = await http.post(
                f"{API}/auth/login",
                json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
            )
            print(f"login: {login.status_code}")
            if login.status_code != 200:
                print(login.text[:400])
                return 1
            csrf = http.cookies.get("mb_csrftoken")

            created = await http.post(
                f"{API}/me/api-keys",
                headers={"X-CSRFToken": csrf or ""},
                json={
                    "name": "D8 MCP live gate",
                    "api_terms_accepted": True,
                    "api_terms_version": "1.0",
                },
            )
            print(f"create key: {created.status_code}")
            if created.status_code != 201:
                print(created.text[:400])
                return 1
            body = created.json()
            api_key = body["key"]
            key_id = body["id"]
            print(f"key prefix: {body['prefix']} (full key never printed)")

            balance = await http.get(
                f"{API}/developer/balance",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            print(f"balance before: {balance.json()}")

            params = StdioServerParameters(
                command=str(PACKAGE_ROOT / ".venv/bin/python"),
                args=["-m", "mainbook_mcp"],
                env={
                    "MAINBOOK_API_KEY": api_key,
                    "MAINBOOK_API_BASE_URL": "https://api.mainbook.ai",
                    "PYTHONPATH": str(PACKAGE_ROOT / "src"),
                    "PATH": os.environ.get("PATH", ""),
                },
                cwd=PACKAGE_ROOT,
            )
            async with Client(stdio_client(params)) as client:
                listed = await client.list_tools()
                print("tools:", [t.name for t in listed.tools])

                print(f"converting {FIXTURE.name} through the MCP tool ...")
                result = await client.call_tool(
                    "convert_bank_statement",
                    {"file_path": str(FIXTURE), "timeout_seconds": 600},
                )
                if result.is_error:
                    print("TOOL ERROR:", result.content[0].text[:600])
                    return 1
                payload = result.structured_content or {}

            state = payload.get("state")
            validation = payload.get("validation")
            doc = (payload.get("data") or {}).get("document") or {}
            ending = doc.get("ending_balance_cents")
            print(f"state:      {state}")
            print(f"pages:      {payload.get('pages')}")
            print(f"validation: {validation}")
            print(f"ending_balance_cents: {ending}  (expected {EXPECTED_ENDING_BALANCE_CENTS})")
            txns = (payload.get("data") or {}).get("transactions") or []
            print(f"transactions: {len(txns)}")

            after = await http.get(
                f"{API}/developer/balance",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            print(f"balance after: {after.json()}")

            ok = (
                state in {"succeeded", "succeeded_with_warnings"}
                and ending == EXPECTED_ENDING_BALANCE_CENTS
            )
            print("GATE:", "PASS" if ok else "FAIL")
            return 0 if ok else 1
        finally:
            if key_id:
                revoked = await http.delete(
                    f"{API}/me/api-keys/{key_id}",
                    headers={"X-CSRFToken": http.cookies.get("mb_csrftoken") or ""},
                )
                print(f"revoke key: {revoked.status_code}")
            cleanup = await http.delete(
                f"{API}/internal/seed-user/?email={TEST_EMAIL}",
                headers={"X-Admin-Secret": secret},
            )
            print(f"seed cleanup: {cleanup.status_code}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
