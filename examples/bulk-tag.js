/*
 * Add, remove or rename one tag on the items of one collection.
 *
 * Writes:      YES, when DRY_RUN is false. With DRY_RUN true (the default) it
 *              changes nothing and returns the items it would change.
 * Edit:        COLLECTION_NAME and TAG; ACTION ("add", "remove" or "rename");
 *              NEW_TAG for "rename". Leave DRY_RUN true until you have read the
 *              dry result, then set it to false.
 * Status:      Live-checked 2026-11-10 on Zotero 10.0.8 (a dry run, then real
 *              add, rename and remove runs on throwaway items)
 *
 * It works one item at a time and only on items in the collection. It never
 * touches the library-wide tag list, so the same tag on an item outside the
 * collection is left alone. Zotero has no trash for tags: a tag you remove is
 * gone from that item, and the result lists each item's tags as they were, so
 * you can add them back.
 *
 * "rename" adds NEW_TAG and removes TAG on each item in scope; it does not
 * rename the tag for the whole library.
 *
 * Before you set DRY_RUN to false: back up the Zotero data folder, and read the
 * dry result. Each item is checked again just before it is changed.
 *
 * The bridge runs this file as the body of an async function with `Zotero` in
 * scope; whatever it returns is printed by the client.
 */

// ---- Edit these -----------------------------------------------------------
const DRY_RUN = true;
const ACTION = "add";                 // "add", "remove" or "rename"
const COLLECTION_NAME = "CHANGE ME";
const TAG = "CHANGE ME";              // the tag to add or remove, or the old name for "rename"
const NEW_TAG = "CHANGE ME";          // only used by "rename"
const INCLUDE_SUBCOLLECTIONS = false; // true also covers the collection's sub-collections
const MAX_CHANGES = 200;              // a real run refuses to change more items than this
// ---------------------------------------------------------------------------

const PLACEHOLDER = "CHANGE ME";
function required(name, value) {
  if (typeof value !== "string" || value.trim() === "" || value.trim() === PLACEHOLDER) {
    throw new Error(`${name} is empty or still "${PLACEHOLDER}". Edit it at the top of this script. Nothing was read or changed.`);
  }
  return value.trim();
}
if (!["add", "remove", "rename"].includes(ACTION)) {
  throw new Error(`ACTION must be "add", "remove" or "rename", not ${JSON.stringify(ACTION)}. Nothing was read or changed.`);
}
const wanted = required("COLLECTION_NAME", COLLECTION_NAME);
const tag = required("TAG", TAG);
const newTag = ACTION === "rename" ? required("NEW_TAG", NEW_TAG) : null;
if (ACTION === "rename" && newTag === tag) {
  throw new Error("NEW_TAG is the same as TAG. Nothing was read or changed.");
}

// Two collections can share a name; stopping is safer than tagging in the wrong one.
// The second argument, true, makes the search include sub-collections.
const matches = Zotero.Collections.getByLibrary(Zotero.Libraries.userLibraryID, true)
  .filter(c => !c.deleted && c.name === wanted);
if (matches.length !== 1) {
  throw new Error(`${matches.length} collections are named "${wanted}"; expected exactly one. Nothing was changed.`);
}
const collection = matches[0];

const collections = [collection];
if (INCLUDE_SUBCOLLECTIONS) {
  for (const d of collection.getDescendents(false, "collection")) {
    collections.push(Zotero.Collections.get(d.id));
  }
}

// An item can sit in several of the collections; handle it once.
const scope = new Map();
for (const c of collections) {
  for (const item of c.getChildItems(false, false)) {
    if (item.isRegularItem() && !item.deleted) scope.set(item.id, item);
  }
}

// What each action needs to be true of an item before it applies.
function needsChange(item) {
  if (ACTION === "add") return !item.hasTag(tag);
  return item.hasTag(tag); // "remove" and "rename" both act on items that carry TAG
}
const inScope = item => !item.deleted && collections.some(c => item.inCollection(c.id));

const planned = [...scope.values()].filter(needsChange);
if (!DRY_RUN && planned.length > MAX_CHANGES) {
  throw new Error(`${planned.length} items would change, more than MAX_CHANGES (${MAX_CHANGES}). Nothing was changed. Raise MAX_CHANGES on purpose or narrow the collection.`);
}

const result = {
  dryRun: DRY_RUN,
  action: ACTION,
  tag,
  newTag,
  collection: wanted,
  includesSubcollections: INCLUDE_SUBCOLLECTIONS,
  itemsInScope: scope.size,
  itemsToChange: planned.length,
  changes: [],
  skipped: [],
};

for (const item of planned) {
  const change = {
    key: item.key,
    title: item.getDisplayTitle(),
    tagsBefore: item.getTags().map(t => ({ tag: t.tag, type: t.type || 0 })),
  };
  if (!DRY_RUN) {
    // The item may have changed since the scan; re-check before touching it.
    if (!inScope(item) || !needsChange(item)) {
      result.skipped.push({ key: item.key, reason: "changed since the scan; left alone" });
      continue;
    }
    if (ACTION === "add") {
      item.addTag(tag, 0);
    } else if (ACTION === "remove") {
      item.removeTag(tag);
    } else {
      const type = item.getTagType(tag) || 0; // keep a manual or automatic tag what it was
      item.removeTag(tag);
      item.addTag(newTag, type);
    }
    await item.saveTx();
    change.tagsAfter = item.getTags().map(t => ({ tag: t.tag, type: t.type || 0 }));
  }
  result.changes.push(change);
}
return result;
