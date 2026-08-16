"""Real MCP-client smoke for stdio and Streamable HTTP against a local REST stub."""

from __future__ import annotations

import asyncio
import io
import os
import socket
import tempfile
from contextlib import suppress
from pathlib import Path

import httpx2
import uvicorn
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from pypdf import PdfWriter
from stub_rest import STUB_API_KEY, build_stub_app

PUBLIC_PDF_URL = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_for_port(port: int) -> None:
    for _ in range(200):
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.02)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise RuntimeError(f"port {port} did not open")


def write_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    path.write_bytes(buffer.getvalue())


async def exercise(client: Client, label: str, source: dict[str, str]) -> None:
    listed = await client.list_tools()
    names = [tool.name for tool in listed.tools]
    print(f"[{label}] list_tools={names}")

    converted = await client.call_tool(
        "convert_bank_statement",
        {**source, "timeout_seconds": 30},
    )
    if converted.is_error:
        raise RuntimeError(converted.content[0].text)
    payload = converted.structured_content or {}
    job_id = payload["job_id"]
    print(
        f"[{label}] convert_bank_statement=state:{payload['state']} "
        f"pages:{payload['pages']} transactions:{len(payload['data']['transactions'])}"
    )

    balance = await client.call_tool("get_balance", {})
    print(f"[{label}] get_balance={balance.structured_content}")

    conversions = await client.call_tool("list_conversions", {"limit": 25})
    jobs = (conversions.structured_content or {})["conversions"]
    print(f"[{label}] list_conversions=count:{len(jobs)} next_cursor:None")

    fetched = await client.call_tool("get_conversion", {"job_id": job_id})
    fetched_payload = fetched.structured_content or {}
    print(
        f"[{label}] get_conversion=state:{fetched_payload['state']} "
        f"has_warnings:{fetched_payload['data']['has_warnings']}"
    )


async def exercise_http_guards(client: Client, pdf_path: Path, rest_port: int) -> None:
    existing_path = await client.call_tool(
        "convert_bank_statement",
        {"file_path": str(pdf_path), "timeout_seconds": 30},
    )
    missing_path = await client.call_tool(
        "convert_bank_statement",
        {"file_path": str(pdf_path.with_name("missing.pdf")), "timeout_seconds": 30},
    )
    if not existing_path.is_error or not missing_path.is_error:
        raise RuntimeError("HTTP file_path was not rejected")
    existing_message = existing_path.content[0].text
    missing_message = missing_path.content[0].text
    if existing_message != missing_message or "file_url" not in existing_message:
        raise RuntimeError("HTTP file_path rejection leaked filesystem state")
    print("[streamable-http] file_path_rejected=before_loader same_for_existing_and_missing:True")

    internal_url = await client.call_tool(
        "convert_bank_statement",
        {"file_url": f"https://127.0.0.1:{rest_port}/private.pdf", "timeout_seconds": 30},
    )
    if not internal_url.is_error:
        raise RuntimeError("HTTP internal file_url was not rejected")
    internal_message = internal_url.content[0].text
    if "public Internet address" not in internal_message or "127.0.0.1" in internal_message:
        raise RuntimeError("HTTP internal file_url rejection was not sanitized")
    print("[streamable-http] internal_file_url_rejected=before_download address_hidden:True")


async def run() -> None:
    rest_port = free_port()
    rest_origin = f"http://127.0.0.1:{rest_port}"
    rest_server = uvicorn.Server(
        uvicorn.Config(
            build_stub_app(rest_origin), host="127.0.0.1", port=rest_port, log_level="error"
        )
    )
    rest_task = asyncio.create_task(rest_server.serve())
    await wait_for_port(rest_port)

    with tempfile.TemporaryDirectory(prefix="mainbook-mcp-live-") as temp_dir:
        pdf_path = Path(temp_dir) / "statement.pdf"
        write_pdf(pdf_path)
        package_root = Path(__file__).resolve().parents[1]
        python = package_root / ".venv" / "bin" / "python"
        shared_env = {
            "MAINBOOK_API_BASE_URL": rest_origin,
            "PYTHONPATH": str(package_root / "src"),
        }

        stdio_params = StdioServerParameters(
            command=str(python),
            args=["-m", "mainbook_mcp"],
            env={**shared_env, "MAINBOOK_API_KEY": STUB_API_KEY},
            cwd=package_root,
        )
        async with Client(stdio_client(stdio_params)) as client:
            await exercise(client, "stdio", {"file_path": str(pdf_path)})

        http_port = free_port()
        process_env = os.environ.copy()
        process_env.update(shared_env)
        process_env.pop("MAINBOOK_API_KEY", None)
        process = await asyncio.create_subprocess_exec(
            str(python),
            "-m",
            "mainbook_mcp",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(http_port),
            cwd=package_root,
            env=process_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await wait_for_port(http_port)
        try:
            async with (
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {STUB_API_KEY}"}
                ) as http_client,
                Client(
                    streamable_http_client(
                        f"http://127.0.0.1:{http_port}/mcp",
                        http_client=http_client,
                    )
                ) as client,
            ):
                await exercise_http_guards(client, pdf_path, rest_port)
                await exercise(client, "streamable-http", {"file_url": PUBLIC_PDF_URL})
        finally:
            process.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5)
            if process.returncode is None:
                process.kill()
                await process.wait()
            _stdout, stderr = await process.communicate()
            if process.returncode not in (0, -15):
                raise RuntimeError(stderr.decode("utf-8", errors="replace"))

    rest_server.should_exit = True
    await rest_task


if __name__ == "__main__":
    asyncio.run(run())
