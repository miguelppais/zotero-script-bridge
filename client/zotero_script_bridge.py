"""Run JavaScript inside the live Zotero client through the Zotero Script Bridge plugin.

    zotero_script_bridge.py FILE.js        run a script file
    zotero_script_bridge.py --code "..."   run inline code
    zotero_script_bridge.py --ping         print Zotero's version
    zotero_script_bridge.py --raw ...      print the result as it is

The code runs as the body of an async function with `Zotero` in scope, as in
Tools > Developer > Run JavaScript: `await` works and `return` sets the result,
so a script written for that window runs unchanged.

Needs Python 3.11 or later (the JSON parser limits the digits of an integer from
that version on).

The token is read from ~/.zotero-script-bridge/token, which the plugin writes on
every Zotero start; ZOTERO_SCRIPT_BRIDGE_TOKEN_FILE overrides the location.

Output is UTF-8; a character that cannot be encoded is written as a backslash
escape. Control characters and bidirectional or zero-width characters in the
reply are shown as \\xNN or \\uNNNN escapes, whether or not the output is a
terminal, so a reply cannot drive the terminal or disguise text (a pipe into a
terminal emulator looks like a file to Python). --raw prints the result on
stdout unescaped, for a program that reads it; messages on stderr stay escaped.

The whole exchange is limited by a watchdog: if it is not finished within
--timeout plus 2 seconds, the client prints one line and exits 3, whatever
Zotero or the program on the port is doing.

Exit codes: 0 the script returned; 1 the script threw (stack printed);
2 bad arguments (including a --timeout outside 0 to 86400 seconds), or the script
file is missing, unreadable or not a regular file, or the token file is missing,
empty, over 4 KiB, not a regular file or unreadable, or the script text cannot
be encoded as UTF-8 (an empty script is not an error here: it is sent and the
bridge rejects it, exit 5; a script file over 1 MiB is sent as 1 MiB and one
byte, and the bridge rejects it, exit 5);
3 Zotero or the plugin is not reachable, or the connection failed after the
script was sent (the script may have run), or the reply was not the bridge's (a
redirect, a reply of the wrong shape, an HTTP error status other than 400, 403,
413, 415 and 500, a reply body over 64 MiB, or no complete reply within
--timeout plus 2 seconds), or stdout was closed or could not be written before
the result was written (the script has already run; only its output was lost),
or an unexpected failure in the client (the script may have run);
4 the bridge refused the request (token, origin or host);
5 the bridge rejected the request: 400 (empty body), 413 (body too large) or 415
(wrong content type);
130 interrupted with Ctrl+C (the script may still be running in Zotero).
"""
import argparse
import http.client
import json
import math
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "http://127.0.0.1:23119/script-bridge/execute"
# None means "work it out in main"; tests patch it with a path.
TOKEN_FILE = None
MAX_REPLY_BYTES = 64 * 1024 * 1024
# The bridge refuses a body over this; one byte more is read so the bridge can say so.
MAX_SCRIPT_BYTES = 1048576
MAX_TOKEN_BYTES = 4096
# The only error statuses the plugin sends besides 403 and 500.
REJECTED_STATUSES = (400, 413, 415)
MAX_TIMEOUT = 86400
# Seconds the watchdog waits beyond --timeout, so the checks that give a better
# message (the reply deadline, the socket timeout) normally fire first.
WATCHDOG_GRACE = 2.0

# C0 controls except tab and newline, DEL, C1 controls, and the characters that
# reorder or hide text: U+200B, U+202A to U+202E, U+2066 to U+2069, U+FEFF.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b\u202a-\u202e\u2066-\u2069\ufeff]")


class ReplyLimit(Exception):
    """The reply was too large, too slow, or could not be held in memory."""


def parse_object(raw: str):
    """Return the JSON object in raw, or None for anything else.

    The bridge always answers with an object; a list or a number means something
    else (Zotero itself, or another program on the port) replied.
    """
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        # Deeply nested input exhausts the parser's stack rather than failing as a ValueError.
        return None
    return body if isinstance(body, dict) else None


def _visible(match):
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def show(text, stream, escape=True):
    """Print text to stream, with control and text-reordering characters made visible.

    A reply is not trusted: escape sequences in it could hide earlier output or
    write to the clipboard. Python cannot always tell a terminal from a pipe into
    one, so the text is escaped unless the caller asks otherwise. A missing stream
    (closed at start-up) prints nothing: print(file=None) would write to stdout.
    """
    if stream is None:
        return
    if escape:
        text = _CONTROL.sub(_visible, text)
    print(text, file=stream)


def warn(text):
    """Write a diagnostic to stderr; a stderr that cannot be written never changes the exit code."""
    try:
        show(text, sys.stderr)
    except (OSError, ValueError):
        pass


def timeout_seconds(text: str) -> float:
    """argparse type: a finite number of seconds, above 0 and at most MAX_TIMEOUT."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    # The socket layer raises on negative, NaN and infinite values and treats 0 as non-blocking.
    if not (math.isfinite(value) and 0 < value <= MAX_TIMEOUT):
        raise argparse.ArgumentTypeError(f"must be above 0 and at most {MAX_TIMEOUT} seconds")
    return value


def read_capped(path: Path, cap: int) -> bytes:
    """Return up to cap + 1 bytes of a regular file; raise OSError for anything else.

    A FIFO, a device or an endless file would otherwise block or fill memory before
    any timeout applies.
    """
    if not stat.S_ISREG(path.stat().st_mode):
        raise OSError("not a regular file")
    with path.open("rb") as f:
        return f.read(cap + 1)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so urllib raises HTTPError with the 3xx code instead.

    The stock handler parses the Location header before it asks whether to follow,
    and a malformed one raises ValueError; answering None first means it is never read.
    """

    def _refuse(self, req, fp, code, msg, headers):
        return None

    http_error_301 = http_error_302 = http_error_303 = _refuse
    http_error_307 = http_error_308 = _refuse


def open_request(req, timeout):
    """Send req through an opener that uses no proxy and follows no redirect.

    The token and the script must travel only to the loopback address in URL. The
    default opener would send them to a proxy named in the environment, and would
    re-send a redirected request, token header included, to whatever the reply names.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    return opener.open(req, timeout=timeout)


def read_reply(resp, deadline: float) -> str:
    """Read the reply body in chunks, within a byte cap and until deadline.

    The socket timeout covers one wait, not the whole reply, so a server that sends
    a byte at a time would otherwise hold the client for ever. read1 returns what
    one wait delivered instead of blocking until the whole chunk arrives.
    """
    read = getattr(resp, "read1", None) or resp.read
    chunks, size = [], 0
    while True:
        try:
            chunk = read(65536)
        except (MemoryError, OverflowError) as e:
            raise ReplyLimit(f"reply could not be read: {e!r}") from e
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_REPLY_BYTES:
            raise ReplyLimit(f"reply larger than {MAX_REPLY_BYTES} bytes")
        chunks.append(chunk)
        if time.monotonic() > deadline:
            raise ReplyLimit("reply not complete within the timeout")
    # read1 returns quietly at a premature EOF; read() would have raised.
    if getattr(resp, "length", 0):
        raise http.client.IncompleteRead(b"".join(chunks), resp.length)
    return b"".join(chunks).decode("utf-8", "replace")


def watchdog_expired(timeout):
    """Give up on the whole exchange. Runs on the timer's thread, so it must end the process."""
    warn(f"no complete reply within {timeout:g} seconds; giving up (the script may have run)")
    # The main thread may be blocked in a socket call that nothing else can interrupt.
    os._exit(3)


def run_request(req, timeout, escape_result) -> int:
    """Send req, read and check the reply, print the outcome; return the exit code."""
    deadline = time.monotonic() + timeout
    try:
        with open_request(req, timeout) as r:
            status = r.status
            raw = read_reply(r, deadline)
    except urllib.error.HTTPError as e:
        status = e.code
        # The error holds the open reply; closing it here keeps the garbage collector
        # from closing it later with a ResourceWarning.
        with e:
            # Checked before the body is read: a redirect with a JSON-object body is still
            # not the bridge's reply.
            if 300 <= status < 400:
                warn(f"unexpected redirect (HTTP {status}) from {URL}; not followed")
                return 3
            try:
                raw = read_reply(e, deadline)
            except ReplyLimit as limit:
                warn(f"HTTP {status}: {limit}")
                return 3
            except (OSError, http.client.HTTPException) as read_error:
                # A reply cut short (IncompleteRead) is an HTTPException, not an OSError.
                warn(f"HTTP {status}: could not read the reply body: {read_error!r}")
                return 3
    except ReplyLimit as limit:
        warn(f"reply from {URL} not accepted: {limit}")
        return 3
    except urllib.error.URLError as e:
        # urllib wraps what goes wrong while connecting and sending the request; the
        # script had not been delivered in full.
        warn(f"Zotero not reachable at {URL}: {e}")
        return 3
    except (OSError, http.client.HTTPException) as e:
        # Anything raised later, while the reply is read, happened after the request was sent.
        warn(f"connection failed after the script was sent; the script may have run: {e!r}")
        return 3

    snippet = raw[:200] or "(no body)"
    body = parse_object(raw)
    if body is None:
        warn(f"HTTP {status}: {snippet}" if status >= 400 else f"unexpected reply from {URL}: {snippet}")
        return 3
    if status == 403:
        error = body.get("error")
        warn(f"refused: {error}" if error is not None else "refused")
        return 4
    if status == 500:
        # The plugin answers a thrown script with ok false and an error; anything else
        # is another server.
        if body.get("ok") is not False or body.get("error") is None:
            warn(f"HTTP 500: not the bridge's reply: {snippet}")
            return 3
    elif status in REJECTED_STATUSES:
        warn(f"rejected (HTTP {status}): {body.get('error')}")
        return 5
    elif status >= 400:
        # The plugin sends only the statuses above; another status with a JSON object
        # (a proxy's 502, another program's 404) did not come from the bridge.
        warn(f"HTTP {status}: not the bridge's reply: {snippet}")
        return 3
    elif body.get("ok") is True:
        res = body.get("result")
        if isinstance(res, str):
            text = res
        else:
            try:
                text = json.dumps(res, ensure_ascii=False, indent=1)
            except RecursionError:
                # indent selects the pure-Python encoder, which has the lower depth limit.
                try:
                    text = json.dumps(res, ensure_ascii=False)
                except RecursionError:
                    warn(f"the result is nested too deeply to print: {snippet}")
                    return 3
        if sys.stdout is None:
            # Started with stdout closed: print would silently discard the result.
            warn("stdout is closed; the result was not written")
            return 3
        show(text, sys.stdout, escape=escape_result)
        return 0
    elif body.get("ok") is not False or body.get("error") is None:
        warn(f"unexpected reply from {URL}: {snippet}")
        return 3
    warn(f"script error: {body.get('error')}\n{body.get('stack', '')}")
    return 1


def main(argv=None) -> int:
    try:
        # Reply text may hold characters the console or a pipe's code page cannot encode.
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError, ValueError):
        pass

    # The docstring is None when Python runs with -OO.
    description = __doc__.splitlines()[0] if __doc__ else None
    ap = argparse.ArgumentParser(description=description)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("file", nargs="?", type=Path)
    g.add_argument("--code")
    g.add_argument("--ping", action="store_true")
    ap.add_argument("--timeout", type=timeout_seconds, default=600,
                    help=f"seconds, above 0 and at most {MAX_TIMEOUT} (default 600)")
    ap.add_argument("--raw", action="store_true",
                    help="print the result unescaped, for a program that reads it")
    a = ap.parse_args(argv)

    try:
        token_file = TOKEN_FILE or Path(os.environ.get("ZOTERO_SCRIPT_BRIDGE_TOKEN_FILE")
                                        or Path.home() / ".zotero-script-bridge" / "token")
    except (RuntimeError, KeyError) as e:
        # Path.home() raises RuntimeError when no home folder can be found, and on some
        # Python versions KeyError when the user is missing from the password database.
        warn(f"cannot locate the token file: {e!r}")
        return 2
    try:
        token_bytes = read_capped(token_file, MAX_TOKEN_BYTES)
        if len(token_bytes) > MAX_TOKEN_BYTES:
            raise OSError(f"larger than {MAX_TOKEN_BYTES} bytes")
        token = token_bytes.decode("utf-8").strip()
    except FileNotFoundError:
        warn(f"no token file at {token_file}: is the plugin installed and Zotero running?")
        return 2
    except (OSError, UnicodeDecodeError) as e:
        warn(f"cannot read token file {token_file}: {e}")
        return 2
    # A header value must encode as latin-1 and http.client rejects a line break in it.
    # The plugin's token is hex, so anything else (or nothing) is a damaged file.
    if not token or not (token.isascii() and token.isprintable()):
        warn(f"token file {token_file} does not hold a token (empty, non-ASCII or control characters)")
        return 2

    if a.ping:
        code = "return Zotero.version;"
    elif a.code is not None:
        code = a.code
    else:
        try:
            script_bytes = read_capped(a.file, MAX_SCRIPT_BYTES)
            # Over the limit the bridge refuses the script whatever it says, so a cut in the
            # middle of a character is not worth an error of its own.
            code = script_bytes.decode("utf-8", "strict" if len(script_bytes) <= MAX_SCRIPT_BYTES else "replace")
        except (OSError, UnicodeDecodeError) as e:
            warn(f"cannot read script file {a.file}: {e}")
            return 2
    try:
        data = code.encode("utf-8")
    except UnicodeEncodeError as e:
        # A lone surrogate can arrive through the command line.
        warn(f"the script text cannot be encoded as UTF-8: {e.reason} at position {e.start}")
        return 2

    req = urllib.request.Request(
        URL,
        data=data,
        method="POST",
        headers={"Content-Type": "text/plain", "X-Script-Bridge-Token": token},
    )
    # --timeout reaches the socket as a limit on each wait, not on the exchange; the
    # watchdog is what bounds the whole of it.
    watchdog = threading.Timer(a.timeout + WATCHDOG_GRACE, watchdog_expired, args=(a.timeout,))
    watchdog.daemon = True
    watchdog.start()
    try:
        return run_request(req, a.timeout, escape_result=not a.raw)
    finally:
        watchdog.cancel()


def discard_stdout():
    """Point stdout at devnull so the flush at interpreter exit cannot fail again."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        return
    try:
        null = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(null, fd)
    except OSError:
        pass
    finally:
        os.close(null)


def cli() -> int:
    """Run main and turn an interrupt, a failed stdout write or a client fault into an exit code."""
    try:
        code = main()
        if sys.stdout is not None:
            sys.stdout.flush()
        return code
    except KeyboardInterrupt:
        warn("interrupted")
        return 130
    except BrokenPipeError:
        discard_stdout()
        return 3
    except OSError as e:
        # warn() swallows a stderr failure, so what reaches here is a stdout write or
        # flush: disk full, a bad handle, anything other than a closed pipe.
        discard_stdout()
        warn(f"cannot write the result: {e}")
        return 3
    except Exception as e:  # MemoryError is an Exception too
        # Last resort: a fault in the client must not look like "the script threw" (1).
        warn(f"unexpected failure in the client; the script may have run: {e!r}")
        return 3


if __name__ == "__main__":
    sys.exit(cli())
