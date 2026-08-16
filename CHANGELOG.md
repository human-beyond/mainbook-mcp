# Changelog

All notable changes to this project are documented here. Versions follow
[semantic versioning](https://semver.org/) and match the published
[PyPI releases](https://pypi.org/project/mainbook-mcp/).

## [0.4.2] — 2026-08-16

No functional changes to the server itself; this release exists so that every
place that describes it says the same thing.

### Added

- `remotes` in `server.json`: the hosted Streamable HTTP endpoint at
  <https://mcp.mainbook.ai/mcp> is now declared in the Official MCP Registry
  entry, not only on the website. Clients that read the registry can offer the
  remote connection without installing anything.

### Changed

- README documents the hosted endpoint alongside local stdio, including which
  behaviour differs over HTTP (no local paths, no output folder).

## [0.4.1] — 2026-08-15

No functional changes.

### Added

- Public source repository at <https://github.com/human-beyond/mainbook-mcp>.
- `server.json` and the `mcp-name: ai.mainbook/bank-statement-converter` marker, which the
  Official MCP Registry uses to verify that this PyPI package belongs to us.
- `SECURITY.md`, this changelog, and a rewritten README.

## [0.4.0] — 2026-08-14

First public release.

### Added

- `convert_bank_statement` — upload one PDF, run the conversion, return the reviewed result.
  JSON stays inline; over local stdio, XLSX and CSV are written to an allowed folder and only the
  path enters model context.
- `get_conversion` — fetch a job after a timeout and return JSON or write XLSX/CSV.
- `list_conversions` — one cursor page of account jobs.
- `get_balance` — total, reserved, and available credits, measured in PDF pages.
- `output_folder` — read or change the default local result folder.
- Two transports: local `stdio` and stateless Streamable HTTP.

### Security

- Local paths are refused over HTTP; remote callers must pass `file_url`.
- Remote downloads require HTTPS, refuse redirects, and reject any hostname that resolves to a
  private, loopback, link-local, metadata, or otherwise non-public address, for IPv4 and IPv6.
- Downloads connect to the already-validated numeric address while keeping the original hostname
  for TLS and the `Host` header, which closes DNS-rebinding races.
- The API key is validated with a cheap balance call before any file is fetched.
- Local reads happen through a single file descriptor with `fstat` checks, a 50 MiB cap, and a
  500-page cap. Result files are never overwritten.
