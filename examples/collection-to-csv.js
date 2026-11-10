/*
 * Return the chosen fields of every item in one collection as CSV text.
 *
 * Writes:      no, read-only. It writes no file either: the CSV is the result,
 *              which the client prints. Redirect it to keep it, for example
 *              `python zotero_script_bridge.py --raw collection-to-csv.js > items.csv`
 *              (py on Windows).
 * Edit:        COLLECTION_NAME (the collection's exact name), and FIELDS.
 * Status:      Live-checked 2026-11-10 on Zotero 10.0.8
 *
 * FIELDS holds Zotero field names ("title", "date", "publicationTitle", "DOI",
 * "url", ...) and the special name "creators" (all authors and editors, last
 * name first, joined with "; "). A name Zotero does not know stops the script;
 * a known field the item type does not have is left empty. The item key and
 * type always come first.
 *
 * The bridge runs this file as the body of an async function with `Zotero` in
 * scope; whatever it returns is printed by the client.
 */

// ---- Edit these -----------------------------------------------------------
const COLLECTION_NAME = "CHANGE ME";
const INCLUDE_SUBCOLLECTIONS = false; // true also exports the collection's sub-collections
const FIELDS = ["title", "creators", "date", "publicationTitle", "DOI", "url"];
// ---------------------------------------------------------------------------

const PLACEHOLDER = "CHANGE ME";
if (typeof COLLECTION_NAME !== "string" || COLLECTION_NAME.trim() === "" || COLLECTION_NAME.trim() === PLACEHOLDER) {
  throw new Error(`COLLECTION_NAME is empty or still "${PLACEHOLDER}". Edit it at the top of this script.`);
}

// A misspelt field name would otherwise give a silent empty column.
for (const f of FIELDS) {
  if (f !== "creators" && !Zotero.ItemFields.getID(f)) throw new Error(`"${f}" is not a Zotero field name.`);
}

// Two collections can share a name; stopping is safer than exporting the wrong one.
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

// An item can sit in several of the collections; export it once.
const items = new Map();
for (const c of collections) {
  for (const item of c.getChildItems(false, false)) {
    if (item.isRegularItem() && !item.deleted) items.set(item.id, item);
  }
}

// A cell that starts with = + - or an at-sign (\x40) is read as a formula by
// spreadsheet programs, so it gets a leading apostrophe; quotes are doubled as
// CSV requires.
function cell(value) {
  let text = String(value ?? "");
  if (/^[=+\-\x40\t\r]/.test(text)) text = "'" + text;
  return /[",\n\r]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
}

function creators(item) {
  // A single-field creator (an organisation) keeps its whole name in lastName.
  return item.getCreators().map(c => [c.lastName, c.firstName].filter(Boolean).join(", ")).join("; ");
}

const rows = [["key", "itemType", ...FIELDS]];
for (const item of items.values()) {
  rows.push([
    item.key,
    Zotero.ItemTypes.getName(item.itemTypeID),
    ...FIELDS.map(f => (f === "creators" ? creators(item) : item.getField(f))),
  ]);
}
// Lines end in \n only: the client adds the final newline when it prints.
return rows.map(row => row.map(cell).join(",")).join("\n");
