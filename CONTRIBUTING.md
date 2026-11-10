# Contributing

Issues and pull requests are welcome. The maintainer reviews and merges them.

For a security problem, do not open an issue. See [SECURITY.md](SECURITY.md).

## Build and test

You need Python 3.11 or newer and Node.js. The commands below use `python`; on Windows the launcher is `py`.

```bash
node --check plugin/bootstrap.js
python -m unittest discover -s tests
python scripts/build_xpi.py
```

`node --check` only parses the file. It cannot tell a wrong Zotero or Gecko function name from a right one.

The build writes `dist/zotero-script-bridge-<version>.xpi` and prints its SHA-256. The unit tests cover the Python client and the scripts. They never talk to a real Zotero.

## Checking a plugin change

The plugin runs inside Zotero, so no test outside Zotero can check it. Install your own build in Zotero (Tools > Plugins > gear menu > Install Plugin From File), try the change, and say in the pull request what you ran and what you saw. Use a test library, not your real one.

## Code

- Python is standard library only. Do not add a third-party import.
- Comments explain why the code does something, not what it does.
- Plugin code runs in Gecko's privileged scope. Use `IOUtils`, `PathUtils`, `Services`, `Cc`/`Ci` and `Zotero.*`, and do not assume Web APIs such as `fetch` exist there.
- Every new client function gets a test in `tests/`.
- After changing `client/zotero_script_bridge.py`, run `python scripts/sync_skill_client.py` to update the copy in `skills/zotero-script-bridge/`; the unit tests fail until the copy matches.

## Changes to the contract

Callers depend on the endpoint path, the header name, where the token is kept, the shape of the JSON reply and the client's exit codes. A change to any of these needs an entry under Unreleased in `CHANGELOG.md`, and the pull request should say why it is needed.

## Releases

Each release carries an `updates.json` file, which is how installed copies find a new version. Only the maintainer cuts releases.
