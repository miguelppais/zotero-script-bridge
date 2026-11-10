/*
 * List the items in one collection that have no DOI, no year, or neither.
 *
 * Writes:      no, read-only.
 * Edit:        COLLECTION_NAME (the collection's exact name), and if you like
 *              INCLUDE_SUBCOLLECTIONS, CHECK_DOI and CHECK_YEAR.
 * Status:      Live-checked 2026-11-10 on Zotero 10.0.8
 *
 * An item type with no DOI field (a book, a web page) is not reported for a
 * missing DOI, because there is nowhere to put one. A DOI written in the Extra
 * field as "DOI: ..." counts as present.
 *
 * The bridge runs this file as the body of an async function with `Zotero` in
 * scope; whatever it returns is printed by the client.
 */

// ---- Edit these -----------------------------------------------------------
const COLLECTION_NAME = "CHANGE ME";
const INCLUDE_SUBCOLLECTIONS = false; // true also checks the collection's sub-collections
const CHECK_DOI = true;
const CHECK_YEAR = true;
const MAX_LISTED = 200;               // a long list is cut here, so the reply stays small
// ---------------------------------------------------------------------------

const PLACEHOLDER = "CHANGE ME";
if (typeof COLLECTION_NAME !== "string" || COLLECTION_NAME.trim() === "" || COLLECTION_NAME.trim() === PLACEHOLDER) {
  throw new Error(`COLLECTION_NAME is empty or still "${PLACEHOLDER}". Edit it at the top of this script.`);
}

// Two collections can share a name; stopping is safer than checking the wrong one.
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

// An item can sit in several of the collections; check it once.
const items = new Map();
for (const c of collections) {
  for (const item of c.getChildItems(false, false)) {
    if (item.isRegularItem() && !item.deleted) items.set(item.id, item);
  }
}

const doiFieldID = Zotero.ItemFields.getID("DOI");
const found = [];
for (const item of items.values()) {
  const missing = [];
  if (CHECK_DOI && Zotero.ItemFields.isValidForType(doiFieldID, item.itemTypeID)) {
    const inExtra = /^\s*DOI:\s*\S/im.test(item.getField("extra"));
    if (item.getField("DOI").trim() === "" && !inExtra) missing.push("doi");
  }
  // strToDate understands partial dates such as "2016" or "March 2016".
  const year = Zotero.Date.strToDate(item.getField("date")).year;
  if (CHECK_YEAR && !year) missing.push("year");
  if (missing.length) {
    found.push({
      key: item.key,
      itemType: Zotero.ItemTypes.getName(item.itemTypeID),
      title: item.getDisplayTitle(),
      missing,
    });
  }
}

return {
  collection: wanted,
  includesSubcollections: INCLUDE_SUBCOLLECTIONS,
  itemsChecked: items.size,
  itemsWithAGap: found.length,
  listed: found.slice(0, MAX_LISTED),
  listCutAt: found.length > MAX_LISTED ? MAX_LISTED : null,
};
