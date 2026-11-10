/*
 * Move or copy the items of one collection into another collection.
 *
 * Writes:      YES, when DRY_RUN is false. With DRY_RUN true (the default) it
 *              changes nothing and returns the items it would move or copy.
 * Edit:        SOURCE_COLLECTION and TARGET_COLLECTION (exact names); MODE
 *              ("copy" or "move"). Leave DRY_RUN true until you have read the
 *              dry result, then set it to false.
 * Status:      Live-checked 2026-11-10 on Zotero 10.0.8 (a dry run, then real
 *              copy and move runs on throwaway items)
 *
 * A Zotero item can sit in several collections, so "copy" adds the item to the
 * target and "move" also takes it out of the source. No item is duplicated or
 * deleted: the item, its attachments and its notes stay where they are in the
 * library. Only the items directly in the source collection are used, not those
 * in its sub-collections. The result lists, for each item, the collections it
 * was in before, so a move can be undone by hand.
 *
 * Before you set DRY_RUN to false: back up the Zotero data folder, and read the
 * dry result. Each item is checked again just before it is changed.
 *
 * The bridge runs this file as the body of an async function with `Zotero` in
 * scope; whatever it returns is printed by the client.
 */

// ---- Edit these -----------------------------------------------------------
const DRY_RUN = true;
const MODE = "copy";                    // "copy" or "move"
const SOURCE_COLLECTION = "CHANGE ME";
const TARGET_COLLECTION = "CHANGE ME";
const MAX_CHANGES = 200;                // a real run refuses to change more items than this
// ---------------------------------------------------------------------------

const PLACEHOLDER = "CHANGE ME";
function required(name, value) {
  if (typeof value !== "string" || value.trim() === "" || value.trim() === PLACEHOLDER) {
    throw new Error(`${name} is empty or still "${PLACEHOLDER}". Edit it at the top of this script. Nothing was read or changed.`);
  }
  return value.trim();
}
if (!["copy", "move"].includes(MODE)) {
  throw new Error(`MODE must be "copy" or "move", not ${JSON.stringify(MODE)}. Nothing was read or changed.`);
}
const sourceName = required("SOURCE_COLLECTION", SOURCE_COLLECTION);
const targetName = required("TARGET_COLLECTION", TARGET_COLLECTION);

// Two collections can share a name; stopping is safer than moving into the wrong one.
// The second argument, true, makes the search include sub-collections.
const all = Zotero.Collections.getByLibrary(Zotero.Libraries.userLibraryID, true).filter(c => !c.deleted);
function find(name) {
  const matches = all.filter(c => c.name === name);
  if (matches.length !== 1) {
    throw new Error(`${matches.length} collections are named "${name}"; expected exactly one. Nothing was changed.`);
  }
  return matches[0];
}
const source = find(sourceName);
const target = find(targetName);
if (source.id === target.id) {
  throw new Error("The source and the target are the same collection. Nothing was changed.");
}

const collectionNames = item => item.getCollections().map(id => {
  const c = Zotero.Collections.get(id);
  return { key: c.key, name: c.name };
});
// "move" must also take the item out of the source, even if it is already in the target.
const needsChange = item =>
  !item.deleted && item.inCollection(source.id) && (MODE === "move" || !item.inCollection(target.id));

const inSource = source.getChildItems(false, false).filter(item => !item.deleted);
const planned = inSource.filter(needsChange);
if (!DRY_RUN && planned.length > MAX_CHANGES) {
  throw new Error(`${planned.length} items would change, more than MAX_CHANGES (${MAX_CHANGES}). Nothing was changed. Raise MAX_CHANGES on purpose or narrow the source.`);
}

const result = {
  dryRun: DRY_RUN,
  mode: MODE,
  source: sourceName,
  target: targetName,
  itemsInSource: inSource.length,
  itemsToChange: planned.length,
  changes: [],
  skipped: [],
};

for (const item of planned) {
  const change = {
    key: item.key,
    title: item.getDisplayTitle(),
    collectionsBefore: collectionNames(item),
  };
  if (!DRY_RUN) {
    // The item may have changed since the scan; re-check before touching it.
    if (!needsChange(item)) {
      result.skipped.push({ key: item.key, reason: "changed since the scan; left alone" });
      continue;
    }
    item.addToCollection(target.id);
    if (MODE === "move") item.removeFromCollection(source.id);
    await item.saveTx();
    change.collectionsAfter = collectionNames(item);
  }
  result.changes.push(change);
}
return result;
