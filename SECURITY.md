# Security

## Reporting a vulnerability

Email **support@mainbook.ai** with the details and, if you can, a way to reproduce the issue.
Please do not open a public issue for a security problem.

We aim to acknowledge a report within three working days.

## What this server touches

- **Your API key.** In local `stdio` mode `MAINBOOK_API_KEY` takes precedence, followed by a
  browser-login credential in the OS keyring or the private local fallback file. In HTTP mode the
  key is read only from each request's `Authorization` header; the environment, keyring, and local
  credential file are not consulted. The key is sent only to the MainBook API; presigned upload
  headers are forwarded to storage unchanged and never carry it.
- **Your files.** The server may read statements from, and write results to, only the folders you
  list when starting it. Paths are expanded and strictly resolved before the check, so `..` and
  symlinks cannot escape. Before a result is written the checked folder is held open as a
  descriptor and every write addresses that descriptor, so a folder swapped after the check —
  including a swap for a symlink pointing elsewhere — is refused rather than followed. Existing
  files are never overwritten. On the read side a narrow race remains between resolving a local
  path and opening it, and the README says so.
- **The network.** Remote source files must be HTTPS, redirects are refused, and any hostname
  resolving to a private, loopback, link-local, metadata, or reserved address is rejected. The
  connection is pinned to the validated numeric address so DNS cannot be changed mid-request.

## Boundaries by design

- There are no tools for buying credits, taking payments, deleting jobs, or changing account data.
- Local file paths are rejected over HTTP: a remote caller must not be able to read the server's
  disk.
- Files are checked for a real PDF header rather than trusting the filename, and are capped at
  50 MiB and 500 pages.
