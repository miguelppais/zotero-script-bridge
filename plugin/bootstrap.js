/* Zotero Script Bridge: POST /script-bridge/execute on Zotero's local server
 * (127.0.0.1:23119) runs the request body as the body of an async function with
 * `Zotero` in scope, the same as Tools > Developer > Run JavaScript, and returns
 * {ok, result} or {ok: false, error, stack} as JSON.
 *
 * Guards, all required, applied in this order:
 * - a request carrying an Origin header is refused, so no web page can reach it.
 *   Zotero's server already drops browser requests and answers preflights without
 *   CORS headers; this check does not rely on that staying true;
 * - the Host header must be 127.0.0.1:<port> or localhost:<port>, so a page whose
 *   hostname was re-pointed at 127.0.0.1 (DNS rebinding) cannot pass. Zotero's
 *   server and its HTTP layer already answer 400 to other hosts, but a non-browser
 *   HTTP/1.0 request with a wrong port or [::1] gets past both and reaches this
 *   check (measured); browsers send HTTP/1.1 or newer and are stopped below it;
 * - the X-Script-Bridge-Token header must equal the token this plugin makes on
 *   every start. The token lives in a variable inside this file's closure and in
 *   ~/.zotero-script-bridge/token, a per-user location so a client in any
 *   repository finds it without knowing the data directory. It is not kept in a
 *   preference, so a script cannot read it back through Zotero.Prefs, and a copy
 *   taken once (a backup, a log, another program on the port) is dead after the
 *   next start. The file is removed at shutdown and at uninstall, but only while
 *   it still holds the token this instance wrote. A crash leaves the file behind;
 *   the rotation at the next start is then the protection.
 *   On macOS and Linux the folder is mode 0700 and the file 0600; on Windows both
 *   keep the ACL inherited from the home folder, which this plugin does not change;
 * - the Content-Type media type must be text/plain, with or without parameters
 *   (415). Zotero's server answers 400 to any other type that is present, before
 *   this code runs, so this check is what refuses a request with no
 *   Content-Type at all (measured on Zotero 10.0.7);
 * - the body must not be empty, and must not exceed MAX_BODY bytes of UTF-8
 *   (413). Zotero's server has already read the whole body into memory before
 *   this code runs, so the limit keeps an oversized script from being compiled
 *   and run; it does not stop the bytes arriving.
 *
 * The bundled client has the port 23119 fixed in it and reads the token file
 * without learning which server wrote it. So startup writes the file only when
 * Zotero's server is listening on 23119; on any other port it logs one error and
 * registers nothing, rather than hand a live token to whatever holds 23119.
 *
 * Everything except the four lifecycle functions lives in the closure below. A
 * script run by the endpoint is compiled in this file's global, so a top-level
 * name would be visible to it, and a sloppy-mode assignment in a script could
 * replace a guard function and break the endpoint. This is robustness, not a
 * security boundary: a script holds `Zotero` and can register endpoints of its own.
 */

// Strict mode makes a failed assignment to, or delete of, Zotero.Server.Endpoints
// throw instead of failing silently. It is not inherited by the scripts the
// endpoint runs: a function made with the AsyncFunction constructor is strict only
// if its own body says so (language rule, checked in node), so scripts written for
// Run JavaScript keep running in sloppy mode.
"use strict";

var { startup, shutdown, install, uninstall } = (() => {
	const PATH = "/script-bridge/execute";
	// Only read to clear it: versions up to 0.1.0 kept the token here.
	const OLD_PREF = "extensions.zotero-script-bridge.token";
	// 1 MiB: far above a real batch script, small enough that compiling it is cheap.
	const MAX_BODY = 1024 * 1024;
	const TOKEN_FORMAT = /^[0-9a-f]{64}$/;
	// The port the bundled client connects to.
	const CLIENT_PORT = 23119;

	// The token of the running plugin; null before startup and after shutdown, so a
	// request that somehow arrives then matches nothing.
	let activeToken = null;
	// The last token this instance put in the file. It outlives shutdown on purpose:
	// shutdown and uninstall compare the file with it before deleting, so they never
	// remove a file that another Zotero profile or a newer start has written.
	let writtenToken = null;
	// Bumped by shutdown. Zotero runs shutdown after startup has been called, and
	// startup has awaits in it, so startup compares the value it began with after
	// each await and stops if shutdown ran in between.
	let generation = 0;

	function makeToken() {
		// The platform's CSPRNG; `crypto` is not guaranteed in the bootstrap scope.
		let bytes = Cc["@mozilla.org/security/random-generator;1"]
			.getService(Ci.nsIRandomGenerator).generateRandomBytes(32);
		return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
	}

	// Constant time over equal lengths. Every token is 64 characters, so returning
	// early on a length mismatch tells a caller nothing it does not already know.
	function sameToken(a, b) {
		if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
		let diff = 0;
		for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
		return diff === 0;
	}

	function header(headers, name) {
		if (!headers) return undefined;
		if (typeof headers.get === "function") return headers.get(name);
		let want = name.toLowerCase();
		for (let k of Object.keys(headers)) if (k.toLowerCase() === want) return headers[k];
		return undefined;
	}

	// Logging must never be what stops a reply, a shutdown or a cleanup.
	function log(e) {
		try {
			Zotero.logError(e);
		}
		catch (_) {}
	}

	// The port is what the server is actually listening on (Zotero.Server.port), not
	// the pref and not 23119. The getter throws when the server is not running, so
	// an unreadable port fails closed.
	function serverPort() {
		try {
			return Zotero.Server.port;
		}
		catch (e) {
			return undefined;
		}
	}

	function sameHost(host) {
		if (typeof host !== "string") return false;
		let port = serverPort();
		if (!Number.isInteger(port)) return false;
		host = host.toLowerCase();
		return host === "127.0.0.1:" + port || host === "localhost:" + port;
	}

	// Media type only: parameters such as charset are ignored, case is ignored, and
	// an absent or non-string header is not text/plain.
	function isTextPlain(contentType) {
		if (typeof contentType !== "string") return false;
		return contentType.split(";")[0].trim().toLowerCase() === "text/plain";
	}

	// Counted by hand rather than with TextEncoder: no API to be missing in a scope
	// (so no throw that would refuse every request or skip the check), and a string
	// is never longer in UTF-16 units than in bytes, so a longer string is over the
	// limit without counting. Lone surrogates count 3 bytes, as an encoder writes
	// U+FFFD for them.
	function bodyTooLarge(data) {
		if (data.length > MAX_BODY) return true;
		let bytes = 0;
		for (let i = 0; i < data.length; i++) {
			let c = data.charCodeAt(i);
			if (c < 0x80) bytes += 1;
			else if (c < 0x800) bytes += 2;
			else if (c >= 0xD800 && c <= 0xDBFF && i + 1 < data.length
					&& (data.charCodeAt(i + 1) & 0xFC00) === 0xDC00) {
				bytes += 4;
				i++;
			}
			else bytes += 3;
		}
		return bytes > MAX_BODY;
	}

	// Unix mode bits on macOS and Linux. Skipped on Windows, where IOUtils maps the
	// mode to the read-only attribute at most and leaves the ACL alone (measured: the
	// call succeeds and the file's ACL is unchanged). The file there keeps the ACL it
	// inherits from the home folder, by decision and not by oversight. `false` is
	// honorUmask, so the mode is exact. A failure is logged and rethrown, so the
	// reason reaches the debug output and startup still stops.
	async function ownerOnly(path, mode) {
		if (Services.appinfo.OS === "WINNT") return;
		try {
			await IOUtils.setPermissions(path, mode, false);
		}
		catch (e) {
			log(e);
			throw e;
		}
	}

	function reply(status, obj) {
		return [status, "application/json", JSON.stringify(obj)];
	}

	function tokenLocation() {
		let dir = PathUtils.join(Services.dirsvc.get("Home", Ci.nsIFile).path, ".zotero-script-bridge");
		return { dir, file: PathUtils.join(dir, "token"), tmp: PathUtils.join(dir, "token.tmp") };
	}

	// A leftover pref holds a token that is already dead, so a failure to clear it
	// is logged and does not stop startup or uninstall.
	function clearOldPref() {
		try {
			Zotero.Prefs.clear(OLD_PREF, true);
		}
		catch (e) {
			log(e);
		}
	}

	// Removes the token file only while it still holds the token this instance
	// wrote. A failure is logged and swallowed, so it cannot block a shutdown.
	async function removeTokenFileIfMine() {
		if (writtenToken === null) return;
		try {
			let { file } = tokenLocation();
			if (!(await IOUtils.exists(file))) return;
			if ((await IOUtils.readUTF8(file)) !== writtenToken) return;
			await IOUtils.remove(file, { ignoreAbsent: true });
		}
		catch (e) {
			log(e);
		}
	}

	async function startup({ id, version, rootURI }, reason) {
		const gen = generation;
		let token = makeToken();
		// Fails closed: a token that is not 64 lowercase hex characters (a broken
		// generator, an unexpected return type) must never be written or accepted.
		if (!TOKEN_FORMAT.test(token)) throw new Error("Zotero Script Bridge: generated token has the wrong format");
		clearOldPref();

		// Nothing is written before the server is known to be listening on the
		// client's port. Not an exception: the plugin stays loaded and inert, and the
		// one error says why.
		let port = serverPort();
		if (!Number.isInteger(port)) {
			log(new Error("Zotero Script Bridge: Zotero's server is not running; no token file written, no endpoint registered"));
			return;
		}
		if (port !== CLIENT_PORT) {
			log(new Error("Zotero Script Bridge: custom server port not supported by the bundled client (server port is "
				+ port + ", the client uses " + CLIENT_PORT + "); no token file written, no endpoint registered"));
			return;
		}

		// Rewritten on every start. The folder is restricted before the token is
		// written into it, and the file after, so a folder or file left loose by an
		// earlier version is tightened too. The token goes to a temporary file first
		// and is moved over the real one, so a reader never sees a half-written file
		// and an old file's other hard links never see the new token. A failure here
		// propagates and the endpoint is not registered: better no bridge than a
		// token other accounts can read.
		let { dir, file, tmp } = tokenLocation();
		let moved = false;
		try {
			await IOUtils.makeDirectory(dir, { ignoreExisting: true });
			if (gen !== generation) return;
			await ownerOnly(dir, 0o700);
			if (gen !== generation) return;
			await IOUtils.remove(tmp, { ignoreAbsent: true });
			if (gen !== generation) return;
			await IOUtils.writeUTF8(tmp, token);
			if (gen !== generation) return;
			await ownerOnly(tmp, 0o600);
			if (gen !== generation) return;
			await IOUtils.move(tmp, file);
			moved = true;
			writtenToken = token;
		}
		finally {
			if (!moved) {
				try {
					await IOUtils.remove(tmp, { ignoreAbsent: true });
				}
				catch (e) {
					log(e);
				}
			}
		}
		if (gen !== generation) {
			// Shutdown ran while the file was being written: the file is ours to remove.
			await removeTokenFileIfMine();
			return;
		}

		const AsyncFunction = (async () => {}).constructor;

		const Endpoint = class {
			constructor() {
				this.supportedMethods = ["POST"];
				this.supportedDataTypes = ["text/plain"];
			}

			async init({ headers, data }) {
				if (header(headers, "Origin")) return reply(403, { ok: false, error: "requests from web pages are refused" });
				if (!sameHost(header(headers, "Host"))) {
					return reply(403, { ok: false, error: "Host header is not the loopback address" });
				}
				if (!sameToken(header(headers, "X-Script-Bridge-Token"), activeToken)) {
					return reply(403, { ok: false, error: "missing or wrong token" });
				}
				if (!isTextPlain(header(headers, "Content-Type"))) {
					return reply(415, { ok: false, error: "Content-Type must be text/plain" });
				}
				if (typeof data !== "string" || !data.trim()) return reply(400, { ok: false, error: "empty body" });
				if (bodyTooLarge(data)) return reply(413, { ok: false, error: "request body exceeds " + MAX_BODY + " bytes" });
				let result;
				try {
					result = await new AsyncFunction("Zotero", data)(Zotero);
				}
				catch (e) {
					// A script can throw a value that cannot be turned into text (an
					// object with no prototype, a getter that throws); the reply must
					// still be the plugin's JSON. Conversion and logging are separate
					// tries so a failure in one loses nothing from the other.
					let error = "error could not be converted to text";
					let stack = "";
					try {
						error = String(e);
						stack = e && e.stack ? String(e.stack) : "";
					}
					catch (_) {}
					log(e);
					return reply(500, { ok: false, error, stack });
				}
				try {
					return reply(200, { ok: true, result: result === undefined ? null : result });
				}
				catch (e) {
					// The script has already run and its effects stand; a distinct
					// message lets the caller tell this from a script that threw.
					log(e);
					return reply(500, { ok: false, error: "the script ran, but its result could not be serialised to JSON", stack: "" });
				}
			}
		};

		// A script holding the token could have made the entry a non-configurable
		// property; the assignment then throws in strict mode, or the entry is not
		// ours. Either way the bridge is not running, so no token stays on disk.
		let registered = false;
		try {
			Zotero.Server.Endpoints[PATH] = Endpoint;
			registered = Zotero.Server.Endpoints[PATH] === Endpoint;
		}
		catch (e) {
			log(e);
		}
		if (!registered) {
			await removeTokenFileIfMine();
			throw new Error("Zotero Script Bridge: could not register " + PATH);
		}
		activeToken = token;
		Zotero.debug("Zotero Script Bridge " + version + " listening on " + PATH);
	}

	// Async so the file is gone before the promise settles; Zotero awaits it for a
	// disable, an upgrade and an uninstall (read in its plugins.js), and whether the
	// removal also runs at application quit is checked live. The token and the
	// generation change before anything is awaited or can throw.
	async function shutdown({ id, version, rootURI }, reason) {
		generation++;
		activeToken = null;
		try {
			if (!delete Zotero.Server.Endpoints[PATH]) log(new Error("Zotero Script Bridge: could not remove " + PATH));
		}
		catch (e) {
			log(e);
		}
		await removeTokenFileIfMine();
	}

	function install() {}

	// An upgrade or downgrade is shutdown, uninstall, then the new startup, awaited
	// in that order (read in Zotero's plugins.js); shutdown has already removed the
	// file, and the new startup writes a new one. Nothing to do here then. The
	// folder goes only when empty: anything else in it is not the plugin's to delete.
	async function uninstall({ id, version, rootURI }, reason) {
		if (reason === ADDON_UPGRADE || reason === ADDON_DOWNGRADE) return;
		await removeTokenFileIfMine();
		clearOldPref();
		try {
			let { dir } = tokenLocation();
			if (await IOUtils.exists(dir) && !(await IOUtils.getChildren(dir)).length) await IOUtils.remove(dir);
		}
		catch (e) {
			log(e);
		}
	}

	return { startup, shutdown, install, uninstall };
})();

// Zotero reads these from the scope when it calls a lifecycle method and never
// assigns to them, and it loads the file once per scope (read in its plugins.js),
// so locking them breaks neither. A script then cannot replace shutdown and leave
// the endpoint registered after the plugin is disabled.
for (const name of ["startup", "shutdown", "install", "uninstall"]) {
	try {
		// The value is passed again so the descriptor is complete whatever the
		// global's implementation does with a partial one.
		Object.defineProperty(globalThis, name, { value: globalThis[name], writable: false, configurable: false });
	}
	catch (e) {
		try {
			Zotero.logError(e);
		}
		catch (_) {}
	}
}
