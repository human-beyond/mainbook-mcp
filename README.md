# MainBook Bank Statement Converter

<!-- mcp-name: ai.mainbook/bank-statement-converter -->

[![PyPI](https://img.shields.io/pypi/v/mainbook-mcp)](https://pypi.org/project/mainbook-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/mainbook-mcp)](https://pypi.org/project/mainbook-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**A finance MCP server scoped to one job: turning PDF bank statements into checked JSON, Excel or
CSV — not a general accounting MCP.** It runs locally with your MainBook API key, or over MainBook's
hosted endpoint at `https://mcp.mainbook.ai/mcp` with that same key.

Point your assistant at a statement and ask for a spreadsheet. The PDF goes to
[MainBook](https://mainbook.ai/mcp), which extracts every transaction, normalises dates to
`YYYY-MM-DD`, keeps money as exact amounts, and re-adds the statement so that
`opening balance + credits − debits` has to match the closing balance. Rows that do not fit are
flagged instead of being passed on quietly.

```text
> Convert ~/Downloads/march-statement.pdf and save the Excel next to it.

  mainbook - convert_bank_statement (MCP)
  63 transactions · 4 pages · 4 credits
  Totals reconciled against the statement
  Saved to ~/Downloads/march-statement.xlsx

Done — 63 transactions. Opening 4,127.50 and closing 3,881.05 both match
the statement, and nothing was flagged.
```

### What it is not

It does **not** connect to bank accounts and is **not** an Open Banking or bank-data API. It reads
statement files you already have. Nothing is scraped and no banking credentials are involved.

### What you need

A MainBook API key from <https://mainbook.ai/developer>, and the folders holding your statements.
Conversion is the only tool that spends page credits. Of the other four, `get_balance` and
`list_conversions` only read, `get_conversion` may write a result file, and `output_folder` changes
a local preference; none of them changes anything in your MainBook account.

## Add it to your client

Add one entry to your client's MCP configuration. This is the same block for Claude Desktop
(**Settings → Developer → Edit Config**), Claude Code, and Cursor:

```json
{
  "mcpServers": {
    "mainbook": {
      "command": "uvx",
      "args": ["mainbook-mcp", "~/Downloads", "~/Desktop", "~/Documents"],
      "env": { "MAINBOOK_API_KEY": "mb_live_…" }
    }
  }
}
```

Codex reads TOML, so put the same thing in `~/.codex/config.toml`:

```toml
[mcp_servers.mainbook]
command = "uvx"
args = ["mainbook-mcp", "~/Downloads", "~/Desktop", "~/Documents"]

[mcp_servers.mainbook.env]
MAINBOOK_API_KEY = "mb_live_…"
```

`uvx` comes with [uv](https://docs.astral.sh/uv/); install it once with `brew install uv` or
`curl -LsSf https://astral.sh/uv/install.sh | sh`. It fetches and runs the published package, so
there is nothing to download by hand and nothing to update. If you would rather not add uv, run
`pip install mainbook-mcp` and use `"command": "mainbook-mcp"` with the same arguments — you then
upgrade it yourself with `pip install -U mainbook-mcp`.

The folder arguments are the only places the server may read a statement from or write a result to;
anything outside them is refused. `MAINBOOK_ALLOWED_DIRS` sets the same list through the
environment instead, separated by the platform's `os.pathsep` (`:` on macOS/Linux, `;` on Windows).

### Claude Desktop, without touching a config file

Claude Desktop also accepts a one-file bundle: **Extensions → Install Extension…** and pick
`mainbook.mcpb`. It asks for the API key and the folders in a dialog and manages its own Python
runtime, so nothing needs installing first. The config block above does the same job and is the
better fit if you already keep other servers there. Build the bundle from this directory with:

```bash
npx --yes @anthropic-ai/mcpb@2.1.2 validate manifest.json
npx --yes @anthropic-ai/mcpb@2.1.2 pack . dist/mainbook.mcpb
```

## What it exposes

- `convert_bank_statement`: creates a paid page-credit job, uploads one PDF, starts conversion,
  polls for up to 30-900 seconds, and returns the reviewed result. JSON stays inline. In local
  stdio mode, XLSX/CSV bytes are written to disk and only the full path enters model context.
- `get_conversion`: checks a job after a timeout and returns JSON inline or writes XLSX/CSV to a
  chosen local destination.
- `list_conversions`: returns one cursor page of account jobs plus `next_cursor`.
- `get_balance`: returns total, reserved, and available credits, all measured in PDF pages.
- `output_folder`: reads or changes the default local result folder.

All five are listed over both transports, but `output_folder` is a local setting: a remote HTTP
client that calls it gets a clear refusal, so four of the five are useful over HTTP.

There are no tools for buying credits, payments, deleting jobs, or changing account data.
Tools that can create a conversion, write a result file, or change the output preference are marked
non-read-only. None is marked destructive because existing result files are never replaced.

## Where result files go

For local stdio clients (Claude Desktop, Claude Code, Cursor, and Codex), XLSX and CSV results are
written to the first available destination in this order:

1. `output_path` supplied to `convert_bank_statement` or `get_conversion` (an absolute filename or
   an existing folder);
2. the folder remembered by `output_folder`;
3. next to the source PDF, with the same base name and the result extension.

`get_conversion` cannot infer the original PDF folder. Without `output_path` or a valid remembered
folder it returns a clear error instead of guessing a destination. Every successful file response
contains the absolute path and explains which rule selected it. Existing files are never replaced:
`statement.xlsx` is followed by `statement (2).xlsx`, then `(3)`, and so on.

Ask the client to call `output_folder` with no argument to see the current setting and every allowed
folder. Set it with an allowed absolute directory, or pass `next_to_source` to restore the default.
The preference is shared by local clients on the same machine in `~/.mainbook/preferences.json`.
A saved folder that is missing or no longer allowed is ignored, and that fallback is stated in the
result.

JSON remains inline. It is also written to a `.json` file only when an explicit `output_path` is
provided. In remote HTTP mode, local paths and `output_folder` are unavailable; XLSX/CSV continues
to return a REST download instruction because the server disk does not belong to the client.

## Manual requirements and installation

- Python 3.11 or newer
- A MainBook API key created at `https://mainbook.ai/developer`

From this directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Use a plain install, not `pip install -e .`. In this checkout the editable install writes a `.pth`
file that the interpreter does not pick up, so `python -m mainbook_mcp` fails with "No module named
mainbook_mcp" while the package looks installed. An identical file under another name is honoured,
so the content is fine and the cause is still unexplained — a plain install sidesteps it entirely.

Keep `mb_live_...` values in a personal environment or client configuration. Never commit them.

### Keeping the key out of every client config

`tools/mainbook-mcp-local` reads the key from `~/.mainbook/api_key` and launches the server, so the
key lives in one file instead of being copied into Claude Desktop, Claude Code, Cursor and Codex
configs separately — one place to rotate, and nothing secret inside a file you might share.

```bash
mkdir -p ~/.mainbook && chmod 700 ~/.mainbook
printf 'mb_live_YOURKEY' > ~/.mainbook/api_key && chmod 600 ~/.mainbook/api_key
```

Then point any client's `command` at `tools/mainbook-mcp-local` with no arguments and no `env`.

## Streamable HTTP mode

MainBook runs this server for you at `https://mcp.mainbook.ai/mcp`, so a client that speaks remote
MCP needs nothing installed. Point it at that URL and send your own key:

```text
Authorization: Bearer mb_live_REPLACE_ME
```

The key is read from each request, so every user of a client reaches their own MainBook account and
spends their own page credits. `initialize` and `tools/list` answer without a key; every tool call
requires one. Local file paths and `output_folder` do not exist over HTTP — pass `file_url` instead
of `file_path`, and XLSX or CSV results come back as a REST download instruction, because the
server's disk is not yours.

You can also run the same remote mode yourself. It is stateless Streamable HTTP with JSON responses:

```bash
mainbook-mcp --transport http --host 127.0.0.1 --port 8000
```

The MCP endpoint is then `http://127.0.0.1:8000/mcp`. Each client should send its own header:

```text
Authorization: Bearer mb_live_REPLACE_ME
```

The header is read from each tool-call request and never stored in global state. If no header is
present, `MAINBOOK_API_KEY` is an optional single-deployment fallback. For Codex remote mode:

```toml
[mcp_servers.mainbook]
url = "https://mcp.mainbook.ai/mcp"
bearer_token_env_var = "MAINBOOK_API_KEY"
tool_timeout_sec = 920
default_tools_approval_mode = "writes"
```

Replace the URL with your own host if you deploy this yourself; a self-hosted deployment still needs
normal HTTPS termination and access controls.

## Environment variables

- `MAINBOOK_API_KEY`: required in stdio; fallback only in HTTP mode.
- `MAINBOOK_API_BASE_URL`: REST host, default `https://api.mainbook.ai`. The server appends
  `/api/v1/developer`.
- `MAINBOOK_ALLOWED_DIRS`: local folders allowed for source reads and result writes, separated by the platform's
  `os.pathsep` (`:` on macOS/Linux and `;` on Windows). Positional directory arguments take
  priority. If neither is supplied, the defaults are `~/Downloads`, `~/Desktop`, and
  `~/Documents`.
- `MAINBOOK_MCP_TRANSPORT`: `stdio` (default) or `http`.
- `MAINBOOK_MCP_HOST`: HTTP bind host, default `127.0.0.1`.
- `MAINBOOK_MCP_PORT`: HTTP bind port, default `8000`.

## File and network safety

- `file_path` and `file_url` are mutually exclusive. `file_path` is accepted only over local
  stdio; HTTP mode rejects it before the filesystem loader runs and requires `file_url`.
- Local `file_path` access and result-file writes use the same configured folders. Positional CLI directories take
  priority over `MAINBOOK_ALLOWED_DIRS`; the environment takes priority over the defaults
  `~/Downloads`, `~/Desktop`, and `~/Documents`. Every root is expanded and resolved, missing
  roots are ignored, and the active roots are printed to stderr when the server starts. If no
  roots remain, local access fails closed while the server continues running.
- Output parents are resolved before writing and checked by directory identity, so a symlink cannot
  redirect a result outside the allowed folders. Result creation is exclusive and collision-safe;
  existing files are not overwritten.
- `~/.mainbook/preferences.json` is replaced atomically. The `.mainbook` directory is mode `0700`
  and the preference file is mode `0600`; malformed or unreadable preferences are ignored safely.
- Local paths are expanded and strictly resolved before the allowlist check, so `..` and symlinks
  cannot make an outside target appear to be inside an allowed folder. The resolved path must be
  strictly below a root, not equal to the root itself.
- The local file is opened once. The server uses `fstat` on that descriptor to require a regular
  file and enforce the 50 MiB limit, then performs the bounded read through the same descriptor.
  This closes the check-versus-read replacement window, but it does not fully eliminate the race
  between resolving the path and opening it; the path can still be replaced during that interval.
- A local file must contain `%PDF-` within its first 1024 bytes before `pypdf` is invoked. Filename
  extensions are not used to decide whether a file is a PDF.
- Remote files must use HTTPS. Redirects are not followed.
- DNS answers are rejected if any address is private, loopback, link-local, metadata, reserved, or
  otherwise non-public, for IPv4 and IPv6.
- URL downloads connect to an already validated numeric IP while retaining the original hostname
  for TLS certificate verification and the HTTP `Host` header, closing DNS-rebinding races.
- `Content-Length` and the actual streamed byte count are independently capped at 50 MiB.
- PDFs are parsed locally with `pypdf` and capped at 500 pages.
- Presigned upload headers from MainBook are forwarded unchanged; the MainBook Bearer key is never
  sent to storage.

## Development checks

```bash
.venv/bin/python -m pip install '.[dev]'
.venv/bin/pytest
.venv/bin/pytest --cov=mainbook_mcp --cov-report=term-missing --cov-report=annotate:cov_annotate
.venv/bin/ruff check .
```

All REST tests use mocks or a local stub. No test requires or accepts a real MainBook API key.
