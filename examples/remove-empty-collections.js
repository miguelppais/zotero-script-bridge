/*
 * Move the empty collections under one parent collection to the Zotero trash.
 *
 * Writes:      YES, when DRY_RUN is false. With DRY_RUN true (the default) it
 *              changes nothing and returns the collections it would trash.
 * Edit:        PARENT_COLLECTION (the exact name of the collection whose
 *              sub-collections are cleaned). Leave DRY_RUN true until you have
 *              read the dry result, then set it to false.
 * Status:      Live-checked 2026-11-10 on Zotero 10.0.8 (a dry run, then a
 *              real run on throwaway collections)
 *
 * Only sub-collections of PARENT_COLLECTION are considered, never the parent
 * itself and never anything outside it. A collection counts as empty when it
 * holds no items, not even trashed ones, and every collection inside it is
 * empty too; it is then trashed together with those inner collections.
 * Collections go to the Zotero trash, where they can be restored; nothing is
 * deleted for good. The result lists each trashed collection with its path and
 * the collections inside it.
 *
 * Before you set DRY_RUN to false: back up the Zotero data folder, and read the
 * dry result. Each collection is checked again just before it is trashed.
 *
 * The bridge runs this file as the body of an async function with `Zotero` in
 * scope; whatever it returns is printed by the client.
 */

// ---- Edit these -----------------------------------------------------------
const DRY_RUN = true;
const PARENT_COLLECTION = "CHANGE ME";
const MAX_REMOVALS = 50;              // a real run refuses to trash more collections than this
// ---------------------------------------------------------------------------

const PLACEHOLDER = "CHANGE ME";
if (typeof PARENT_COLLECTION !== "string" || PARENT_COLLECTION.trim() === "" || PARENT_COLLECTION.trim() === PLACEHOLDER) {
  throw new Error(`PARENT_COLLECTION is empty or still "${PLACEHOLDER}". Edit it at the top of this script. Nothing was read or changed.`);
}

// Two collections can share a name; stopping is safer than cleaning the wrong one.
// The second argument, true, makes the search include sub-collections.
const wanted = PARENT_COLLECTION.trim();
const matches = Zotero.Collections.getByLibrary(Zotero.Libraries.userLibraryID, true)
  .filter(c => !c.deleted && c.name === wanted);
if (matches.length !== 1) {
  throw new Error(`${matches.length} collections are named "${wanted}"; expected exactly one. Nothing was changed.`);
}
const parent = matches[0];

// Items are counted with trashed ones included: a collection that still holds a
// trashed item is not empty. Child collections already in the trash are ignored.
function isRemovable(collection) {
  if (collection.deleted) return false;
  if (collection.getChildItems(true, true).length > 0) return false;
  return collection.getChildCollections(false, false).every(isRemovable);
}

// The outermost removable collections only: trashing one trashes those inside it.
function findRemovable(collection, path, found) {
  for (const child of collection.getChildCollections(false, false)) {
    const childPath = [...path, child.name];
    if (isRemovable(child)) {
      found.push({ collection: child, path: childPath });
    } else {
      findRemovable(child, childPath, found);
    }
  }
  return found;
}
const candidates = findRemovable(parent, [parent.name], []);

const inside = collection => collection.getDescendents(false, "collection")
  .map(d => ({ key: Zotero.Collections.get(d.id).key, name: d.name }));

if (!DRY_RUN && candidates.length > MAX_REMOVALS) {
  throw new Error(`${candidates.length} outermost empty collections would be trashed (each with the empty collections inside it), more than MAX_REMOVALS (${MAX_REMOVALS}). Nothing was changed. Raise MAX_REMOVALS on purpose or narrow the parent.`);
}

const result = {
  dryRun: DRY_RUN,
  parent: wanted,
  collectionsToTrash: candidates.length,
  changes: [],
  skipped: [],
};

for (const { collection, path } of candidates) {
  const entry = {
    key: collection.key,
    name: collection.name,
    path: path.join(" > "),
    collectionsInside: inside(collection),
  };
  if (!DRY_RUN) {
    // The collection may have gained an item since the scan; re-check before trashing it.
    if (!isRemovable(collection)) {
      result.skipped.push({ key: collection.key, reason: "no longer empty; left alone" });
      continue;
    }
    collection.deleted = true;
    await collection.saveTx();
    entry.nowInTrash = collection.deleted;
  }
  result.changes.push(entry);
}
return result;
