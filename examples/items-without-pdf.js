/*
 * List the items in one collection that have no PDF attachment.
 *
 * Writes:      no, read-only.
 * Edit:        COLLECTION_NAME (the collection's exact name), and if you like
 *              INCLUDE_SUBCOLLECTIONS and REQUIRE_FILE_ON_DISK.
 * Status:      Live-checked 2026-11-10 on Zotero 10.0.8
 *
 * The bridge runs this file as the body of an async function with `Zotero` in
 * scope; whatever it returns is printed by the client.
 */

// ---- Edit these -----------------------------------------------------------
const COLLECTION_NAME = "CHANGE ME";
const INCLUDE_SUBCOLLECTIONS = false; // true also checks the collection's sub-collections
const REQUIRE_FILE_ON_DISK = false;   // true: a PDF record whose file is missing counts as no PDF
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
    // Only regular items (articles, books, ...) carry attachments as children.
    if (item.isRegularItem() && !item.deleted) items.set(item.id, item);
  }
}

const without = [];
for (const item of items.values()) {
  let hasPdf = false;
  for (const attachment of Zotero.Items.get(item.getAttachments())) {
    // A trashed attachment is not part of the item any more.
    if (attachment.deleted || attachment.attachmentContentType !== "application/pdf") continue;
    if (!REQUIRE_FILE_ON_DISK || await attachment.fileExists()) {
      hasPdf = true;
      break;
    }
  }
  if (!hasPdf) {
    without.push({
      key: item.key,
      itemType: Zotero.ItemTypes.getName(item.itemTypeID),
      title: item.getDisplayTitle(),
      year: Zotero.Date.strToDate(item.getField("date")).year || null,
      attachments: item.getAttachments().length,
    });
  }
}

return {
  collection: wanted,
  includesSubcollections: INCLUDE_SUBCOLLECTIONS,
  itemsChecked: items.size,
  withoutPdf: without.length,
  listed: without.slice(0, MAX_LISTED),
  listCutAt: without.length > MAX_LISTED ? MAX_LISTED : null,
};
