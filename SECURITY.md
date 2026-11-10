# Security policy

## Supported versions

Only the latest release is supported. Fixes are made on the current code and shipped in a new release.

## Reporting a vulnerability

Report it privately. Open the repository's Security tab and choose "Report a vulnerability" (GitHub's private vulnerability reporting). Do not open a public issue or pull request for it.

The reason is what the plugin does. Its endpoint runs JavaScript with Zotero's full rights, so a public report of a way around its guards would hand out an exploit before there is a fix.

Please include:

- the plugin version;
- the Zotero version;
- your operating system;
- the steps to reproduce, and what you expected to happen instead.

## What counts

The plugin is built to run code for whoever holds its token. A new token is created each time Zotero starts and is kept in a file in the user's home folder, so anyone who can read that file can already run code as that user, and that is by design. A report is useful when it shows a way to reach the endpoint, or to run a script, without the token; a way for another user account to read the token file; or a way for a web page or another program to get past the other checks on a request.

The README describes the guards and what they do not cover.
