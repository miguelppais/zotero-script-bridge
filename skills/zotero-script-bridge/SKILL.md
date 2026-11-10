---
name: zotero-script-bridge
description: Run JavaScript inside a live Zotero through the Zotero Script Bridge plugin, to read or organise a library (file, move, merge, tag, trash). Use when a task needs the running Zotero app's own API, when an agent must check that the bridge works, read an exit code from the bridge client, or write to a Zotero library safely.
---

# Zotero Script Bridge

The user is responsible for their library in every case. These safeguards reduce risk but do not guarantee safety.

This is plain-language advice, not legal advice. The warranty disclaimer in the `LICENSE` file (MIT) is the operative text.

Install this skill with `npx skills add miguelppais/zotero-script-bridge`, which installs this folder, the client included, for the agent you choose. Or copy the repository's `skills/zotero-script-bridge/` folder into your agent's skills folder by hand.

## What the bridge is

The Zotero Script Bridge plugin adds one endpoint to Zotero's local server. A script sent to it runs inside the running Zotero app, as the body of an async function with `Zotero` in scope, like Tools > Developer > Run JavaScript. `await` works. `return` a JSON-serialisable value to get it back.

**A script has full access.** It can read and change everything in every library, and anything else Zotero's privileged scope reaches. Treat every script as if it could do all of that, and read a script before you run it.

## Check the bridge

The client is one Python file, `zotero_script_bridge.py`, in this skill's folder. It is the same file as `client/zotero_script_bridge.py` in the bridge's repository. It needs Python 3.11 or later and no packages, and you can copy the file anywhere. On Windows the Python launcher is `py`.

```bash
python <this skill's folder>/zotero_script_bridge.py --ping
```

This prints Zotero's version. Zotero must be running with the plugin installed. The client reads a token from `~/.zotero-script-bridge/token`. The plugin makes a new token on every Zotero start and removes the file when Zotero closes, so the client reads it afresh on each call, and you never keep a copy.

## Run a script

```bash
python <this skill's folder>/zotero_script_bridge.py script.js
python <this skill's folder>/zotero_script_bridge.py --code "return Zotero.version;"
```

- `--timeout SECONDS` limits the whole exchange (default 600).
- Control characters and zero-width or text-direction characters in the reply are shown as backslash escapes, so a reply cannot disguise text or drive the terminal. `--raw` prints the result unescaped, for a program that reads it. Do not use `--raw` on output you will show to a person or paste into a terminal.
- A reply is JSON: `{ok: true, result}` on success, or `{ok: false, error, stack}` when the script threw. The client prints the result (or the error and stack) and sets the exit code.

### Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | The script returned. | Read the result. |
| 1 | The script threw (the stack is printed), or its result could not be turned into JSON (the message says the script ran). | Read the error. If the message says the script ran, its changes happened: check the library before you run it again. |
| 2 | Bad arguments, or the script file or the token file is missing or unreadable. | Fix the call. With Zotero not running the client normally exits 2 ("no token file"), because the plugin removes the token file when Zotero closes: start Zotero and try again. |
| 3 | Zotero or the plugin is not reachable, or the reply was not the bridge's. When the connection failed after the script was sent, the message says the script may have run. | Do not assume the script did not run. Check the library before you retry anything that writes. |
| 4 | The bridge refused the request (token, origin or host). | Run the call again so the client reads the current token. If it persists, stop and tell the user. |
| 5 | The bridge rejected the request: empty body (400), body over 1 MiB (413) or wrong content type (415). | Fix the script. Nothing ran. |
| 130 | Interrupted with Ctrl+C. | The script may still be running in Zotero. Check before you retry. |

## Read-only scripts

A script that only reads needs no safeguards. Start with one: count a collection, list items, look at a field. Do it before you write anything, to confirm what the script will touch.

## Scripts that write: the required pattern

For any script that changes the library, follow every step. Do not skip one because the change looks small.

1. **Back up first.** Take a database backup, and copy the `storage/` folder yourself if the change could touch stored files.

   ```js
   return await Zotero.DB.backUpDatabase({suffix: "before-my-change", force: true, online: true});
   ```

   This writes `zotero.sqlite.before-my-change.bak` in the Zotero data folder (`Zotero.DataDirectory.dir`) without closing the database. It covers the database only, not `storage/`. Use `backUpDatabase` with `online: true`. The older `backupDatabase` is a deprecated shim that takes an offline backup, which closes the database.
2. **Dry pass.** The script first reports what it would change and changes nothing (`const DRY_RUN = true;`). Read that report. Set `DRY_RUN` to `false` only after you have read it.
3. **Guards.** Re-check each item's state just before you change it, and skip it if it no longer matches.
4. **Log before-values.** Return the values as they were for every change, so the user can undo it.
5. **Trash only.** Remove things by moving them to the Zotero trash (`item.deleted = true`, then `saveTx()`). Never erase permanently (`eraseTx`, `erase`), and never empty the trash.
6. **Ask before anything the trash cannot undo.** Tags have no trash, and a file on disk that is erased stays erased. Never call a library-wide tag function (`Zotero.Tags.removeFromLibrary`, `Zotero.Tags.rename`, `Zotero.Tags.purge` and the like). Work item by item, inside a named collection. Ask the user before any other change that cannot be undone.

Keep batches small enough to check by eye, and read the result of each run before the next.

## Zotero sync

The bridge changes the local Zotero database through the running Zotero app, unlike tools that work through the Zotero web API, so it works with cloud sync turned off. If sync is on, local changes sync to the cloud as any edit made in the app would. Because changes sync, a mistake in a batch also reaches the cloud and other devices, so back up and dry-run before any write. How the bridge behaves with sync on has not been tested; this describes how Zotero sync normally treats local edits.

## Ready-made scripts

The bridge's `examples/` folder holds ready-to-run scripts: counting and listing a collection, items without a PDF, items missing a DOI or year, a CSV of fields, merging duplicates, tag changes, moving or copying items between collections, and removing empty collections. Read each file's header first: it says whether the script writes and whether it has been checked on a live library. Keep `DRY_RUN = true` until you have read the dry pass.

## Two single-item snippets

The examples do not cover these two. Each follows the pattern above (dry pass, re-check, before-values). Replace the placeholder keys with item and collection keys you have read from the library. In the Zotero client an item's key is not shown, so read it with a script first.

### Add one item to a collection

Live-checked 2026-11-10 on Zotero 10.0.8, on a throwaway item: the dry pass, the real add, and a second run that reported "already in the collection".

```js
const DRY_RUN = true;
const COLLECTION_KEY = "CHANGE ME";
const ITEM_KEY = "CHANGE ME";

const libraryID = Zotero.Libraries.userLibraryID;
const collection = Zotero.Collections.getByLibraryAndKey(libraryID, COLLECTION_KEY);
const item = Zotero.Items.getByLibraryAndKey(libraryID, ITEM_KEY);
if (!collection) throw new Error("No collection with that key. Nothing was changed.");
if (!item || !item.isRegularItem() || item.deleted) {
  throw new Error("No live top-level item with that key. Nothing was changed.");
}
const before = item.getCollections();   // collection IDs, logged for undo
if (item.inCollection(collection.id)) return { changed: false, reason: "already in the collection", before };
if (DRY_RUN) return { changed: false, dryRun: true, wouldAddTo: collection.name, before };
item.addToCollection(collection.id);
await item.saveTx();
return { changed: true, before, after: item.getCollections() };
```

### Move one item to the trash

Live-checked 2026-11-10 on Zotero 10.0.8, on a throwaway item: the dry pass, the real move to the trash, and a second run that reported "already in the trash". The item was left in the trash.

```js
const DRY_RUN = true;
const ITEM_KEY = "CHANGE ME";

const item = Zotero.Items.getByLibraryAndKey(Zotero.Libraries.userLibraryID, ITEM_KEY);
if (!item) throw new Error("No item with that key. Nothing was changed.");
if (item.deleted) return { changed: false, reason: "already in the trash" };
const before = item.toJSON();           // logged for undo
if (DRY_RUN) return { changed: false, dryRun: true, wouldTrash: before.title || before.key, before };
item.deleted = true;                    // the trash, not erasure; never call eraseTx
await item.saveTx();
return { changed: true, deleted: item.deleted, before };
```

To undo, set `item.deleted = false` and `saveTx()`, or restore the item from the trash in Zotero. Untrashing was not part of the live check.
