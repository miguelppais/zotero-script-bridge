/*
 * Count the items in one collection, by item type, and optionally list them.
 *
 * Writes:      no, read-only.
 * Edit:        COLLECTION_NAME (the collection's exact name), and if you like
 *              INCLUDE_SUBCOLLECTIONS and LIST_ITEMS.
 * Status:      Live-checked 2026-11-10 on Zotero 10.0.8
 *
 * The bridge runs this file as the body of an async function with `Zotero` in
 * scope; whatever it returns is printed by the client.
 */

// ---- Edit these -----------------------------------------------------------
const COLLECTION_NAME = "CHANGE ME";
const INCLUDE_SUBCOLLECTIONS = false; // true also counts the collection's sub-collections
const LIST_ITEMS = false;             // true adds a short line per item to the result
const MAX_LISTED = 200;               // a long list is cut here, so the reply stays small
// ---------------------------------------------------------------------------

const PLACEHOLDER = "CHANGE ME";
if (typeof COLLECTION_NAME !== "string" || COLLECTION_NAME.trim() === "" || COLLECTION_NAME.trim() === PLACEHOLDER) {
  throw new Error(`COLLECTION_NAME is empty or still "${PLACEHOLDER}". Edit it at the top of this script.`);
}

// Two collections can share a name; stopping is safer than counting the wrong one.
const wanted = COLLECTION_NAME.trim();
// The second argument, true, makes the search include sub-collections.
const matches = Zotero.Collections.getByLibrary(Zotero.Libraries.userLibraryID, true)
  .filter(c => !c.deleted && c.name === wanted);
if (matches.length !== 1) {
  throw new Error(`${matches.length} collections are named "${wanted}"; expected exactly one.`);
}
const collection = matches[0];

const collections = [collection];
if (INCLUDE_SUBCOLLECTIONS) {
  for (const d of collection.getDescendents(false, "collection")) {
    collections.push(Zotero.Collections.get(d.id));
  }
}

// An item can sit in several of the collections; count it once.
const items = new Map();
for (const c of collections) {
  for (const item of c.getChildItems(false, false)) {
    if (!item.deleted) items.set(item.id, item);
  }
}

const byType = {};
const regular = [];
for (const item of items.values()) {
  const type = Zotero.ItemTypes.getName(item.itemTypeID);
  byType[type] = (byType[type] || 0) + 1;
  if (item.isRegularItem()) regular.push(item);
}

const result = {
  collection: wanted,
  includesSubcollections: INCLUDE_SUBCOLLECTIONS,
  totalItems: items.size,
  regularItems: regular.length,
  byType,
};

if (LIST_ITEMS) {
  result.listed = regular.slice(0, MAX_LISTED).map(item => ({
    key: item.key,
    title: item.getDisplayTitle(),
    year: Zotero.Date.strToDate(item.getField("date")).year || null,
  }));
  result.listCutAt = regular.length > MAX_LISTED ? MAX_LISTED : null;
}
return result;
