# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The agent skill folder now carries the client beside `SKILL.md`, so
  `npx skills add miguelppais/zotero-script-bridge` installs both. The plugin
  is still installed from the `.xpi`.

## [0.2.0] - 2026-11-10

First release.

### Added

- Zotero 7 to 10 bootstrapped plugin that registers
  `POST /script-bridge/execute` on Zotero's local server
  (`127.0.0.1:23119`). The `text/plain` body runs as the body of an async
  function with `Zotero` in scope, as in Tools > Developer > Run JavaScript.
- JSON replies: `{ok: true, result}` on success,
  `{ok: false, error, stack}` with status 500 when the script throws. A result
  that cannot be turned into JSON (a cycle, a BigInt) returns a 500 with the
  error "the script ran, but its result could not be serialised to JSON", so a
  caller can tell it from a script that threw; the script's effects have
  already happened.
- Token guard: a 32-byte token from `nsIRandomGenerator`, made on every Zotero
  start (an upgrade included), held in memory and written to
  `~/.zotero-script-bridge/token`, which is removed when Zotero closes or the
  plugin is uninstalled. Requests must send it in the `X-Script-Bridge-Token`
  header; it is compared in constant time. Missing or wrong token: 403. Read
  the token file on each call rather than keeping a copy.
- Origin guard: any request carrying an `Origin` header is refused with 403.
- Empty-body guard: 400.
- Port condition: the plugin starts the bridge only when Zotero's server
  listens on port 23119, the port the bundled client uses. On another port, or
  with the server off, it logs one error, writes no token file and registers no
  endpoint.
- Python client `client/zotero_script_bridge.py` (standard library only,
  Python 3.11 or later): runs a file, `--code` or `--ping`, with `--timeout`
  and the `ZOTERO_SCRIPT_BRIDGE_TOKEN_FILE` override. Exit codes:
  - 0: the script returned.
  - 1: the script threw.
  - 2: bad arguments, including a `--timeout` that is not above 0 and at most
    86 400 seconds; a script file or token file that is missing, unreadable or
    not a regular file, or an empty token file; script text that cannot be
    encoded as UTF-8. With Zotero closed there is no token file, so the client
    exits 2.
  - 3: Zotero or the plugin unreachable; a connection that failed after the
    script was sent (the message says the script may have run); a reply that is
    not the bridge's (a redirect, a reply of the wrong shape, an HTTP error
    status other than 400, 403, 413, 415 and 500, a reply body over 64 MiB, or no
    complete reply within `--timeout` plus 2 seconds, which a watchdog enforces
    so that exit 3 comes within roughly `--timeout` whatever answers on the
    port); stdout closed or unwritable when the result is written; an
    unexpected failure in the client (the script may have run).
  - 4: the bridge refused the request (token, origin or host), including a 403
    without an error message.
  - 5: the bridge rejected the request: an empty body (400), a body over
    1 048 576 bytes (413) or a wrong Content-Type (415).
  - 130: interrupted with Ctrl+C, with a one-line message.
- Client flag `--raw`: print the result on stdout without escaping, for a
  program that reads it. Messages on stderr stay escaped.
- Client output: stdout is always UTF-8, and a character it cannot encode is
  written as a backslash escape. Control characters (including carriage return)
  and the bidirectional and zero-width characters U+200B, U+202A to U+202E,
  U+2066 to U+2069 and U+FEFF in a reply are shown as `\xNN` or `\uNNNN`
  escapes, whether or not the output is a terminal; a pipe into a terminal
  emulator looks like a file to Python. A result nested too deeply for the
  indented JSON printer is printed without indentation.
- Client limits: the token file is read up to 4 KiB and the script file up to
  1 MiB and one byte, and both must be regular files (a script over the limit
  is sent as 1 MiB and a byte so the bridge's 413 gives exit 5).
- Build script `scripts/build_xpi.py`, standard library only. It writes
  `dist/zotero-script-bridge-<version>.xpi` from `plugin/` and prints its
  SHA-256; two builds of one tree give the same bytes on one machine. It
  refuses any file in `plugin/` other than `bootstrap.js` and `manifest.json`,
  and it also writes `dist/updates.json` (the update manifest for the GitHub
  release) from the manifest and the `.xpi`'s SHA-256.
- Updates: installed copies check for updates through GitHub releases. The
  manifest's `update_url` points at the latest release's `updates.json`, which
  names the versioned `.xpi` and its SHA-256.
- `CONTRIBUTING.md` (how to build, test and propose changes) and `SECURITY.md` (how to report a vulnerability privately).
- Example scripts in `examples/` for everyday library tasks; write scripts default to a dry run and remove only to the Zotero trash.
- An agent skill, `skills/zotero-script-bridge/SKILL.md`, for coding agents that use the bridge: how to run scripts, read replies and exit codes, and the safeguards for scripts that write.

### Security

- Host guard: a request whose `Host` header is not `127.0.0.1:<port>` or
  `localhost:<port>`, with `<port>` the port Zotero's server is listening on
  (`Zotero.Server.port`), is refused with 403 before the token check. A
  missing `Host` is refused. This is the plugin's own defence against DNS
  rebinding, kept in addition to the host checks Zotero's server makes.
- Body size guard: a body larger than 1 MiB (1 048 576 bytes of UTF-8) is
  refused with 413 after the token check, and is not compiled or run. Zotero's
  server reads the whole body before the plugin sees it, so this limits what
  runs, not what is received. The bundled client exits 5 on a 413.
- Content-Type guard: a request whose `Content-Type` media type is not
  `text/plain` (case-insensitive, `charset` and other parameters allowed) is
  refused with 415 after the token check. Zotero's server answers 400 itself to
  any other type that is present (measured on Zotero 10.0.7, and before the
  token check), so in practice this refuses a request with no `Content-Type`.
  The bundled client always sends `text/plain`.
- Token file permissions on macOS and Linux: on every start the folder
  `~/.zotero-script-bridge` is set to mode 0700 before the token is written and
  the file to 0600 after, which also tightens a folder or file left loose. If
  setting the mode fails, startup fails and the endpoint is not registered. Not
  yet run on macOS or Linux. On Windows the plugin does not change the ACL
  (`IOUtils` cannot); the file keeps the ACL it inherits from the home folder.
- The client ignores proxy settings and never follows a redirect, so the token and the script go only to 127.0.0.1; a redirect reply exits 3.
- A token read once, from a backup, a log or another program on the port, stops working at the next Zotero start.
- The token file is written to a temporary file and moved into place, and is removed at shutdown only while it still holds the token that instance wrote. A start that is overtaken by a shutdown removes what it wrote and registers nothing. Startup fails if the endpoint entry cannot be set.
- The four lifecycle functions cannot be reassigned by a script, so disabling the plugin always reaches its own shutdown.
