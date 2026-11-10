/*
 * Find duplicate items inside one collection and merge each set into its
 * oldest item. The other items go to the Zotero trash, where they can be
 * restored; nothing is deleted for good.
 *
 * Writes:      YES, when DRY_RUN is false. With DRY_RUN true (the default) it
 *              changes nothing and returns the sets it would merge.
 * Edit:        COLLECTION_NAME (the collection's exact name). Leave DRY_RUN
 *              true until you have read the dry result, then set it to false.
 * Status:      Live-checked 2026-11-10 on Zotero 10.0.8 (a dry run, then a
 *              real run on throwaway items)
 *
 * Two items are duplicates when they have the same item type and the same
 * title once case, accents and punctuation are ignored, and they do not
 * disagree: two different DOIs, or two different years, mean two different
 * works. The oldest item (by date added) is kept; Zotero's merge moves the
 * others' attachments, notes, tags and collections onto it. In the live check
 * the kept item kept its own DOI; whether an empty field on the kept item is
 * filled from another item was not tested. The result lists the full contents
 * of every item in every set before it is merged, so a field that was dropped
 * can be read back, and each trashed item can be restored from the trash.
 *
 * Not exercised: the fallback to Zotero.Items.merge (Zotero 7 to 9).
 *
 * Before you set DRY_RUN to false: back up the Zotero data folder, and read the
 * dry result. Each set is checked again just before it is merged.
 *
 * The bridge runs this file as the body of an async function with `Zotero` in
 * scope; whatever it returns is printed by the client.
 */

// ---- Edit these -----------------------------------------------------------
const DRY_RUN = true;
const COLLECTION_NAME = "CHANGE ME";
const INCLUDE_SUBCOLLECTIONS = false; // true also looks in the collection's sub-collections
const MAX_SETS = 25;                  // sets merged per run; run again for the rest
// ---------------------------------------------------------------------------

const PLACEHOLDER = "CHANGE ME";
if (typeof COLLECTION_NAME !== "string" || COLLECTION_NAME.trim() === "" || COLLECTION_NAME.trim() === PLACEHOLDER) {
  throw new Error(`COLLECTION_NAME is empty or still "${PLACEHOLDER}". Edit it at the top of this script. Nothing was read or changed.`);
}

// Two collections can share a name; stopping is safer than merging in the wrong one.
const wanted = COLLECTION_NAME.trim();
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

// An item can sit in several of the collections; look at it once.
const scope = new Map();
for (const c of collections) {
  for (const item of c.getChildItems(false, false)) {
    if (item.isRegularItem() && !item.deleted) scope.set(item.id, item);
  }
}

function normalisedTitle(item) {
  return item.getDisplayTitle()
    .normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}
const doiOf = item => item.getField("DOI").trim().toLowerCase().replace(/^https?:\/\/(dx\.)?doi\.org\//, "");
const yearOf = item => Zotero.Date.strToDate(item.getField("date")).year || "";

function sameWork(a, b) {
  if (a.itemTypeID !== b.itemTypeID) return false;
  const title = normalisedTitle(a);
  if (title === "" || title !== normalisedTitle(b)) return false;
  if (doiOf(a) && doiOf(b) && doiOf(a) !== doiOf(b)) return false;
  if (yearOf(a) && yearOf(b) && yearOf(a) !== yearOf(b)) return false;
  return true;
}

// Oldest first, so the first member of a set is the one that is kept.
const ordered = [...scope.values()].sort((a, b) => (a.dateAdded < b.dateAdded ? -1 : a.dateAdded > b.dateAdded ? 1 : a.id - b.id));
const sets = [];
for (const item of ordered) {
  const home = sets.find(set => sameWork(set[0], item));
  if (home) home.push(item); else sets.push([item]);
}
const duplicateSets = sets.filter(set => set.length > 1);

// Everything needed to put an item back by hand: all fields, tags, collections
// (as keys), relations, plus how many children it had.
function snapshot(item) {
  return { key: item.key, dateAdded: item.dateAdded, children: item.numChildren(true), json: item.toJSON() };
}

const result = {
  dryRun: DRY_RUN,
  collection: wanted,
  includesSubcollections: INCLUDE_SUBCOLLECTIONS,
  itemsChecked: scope.size,
  setsFound: duplicateSets.length,
  sets: [],
  skipped: [],
};

// Zotero 10 moved the merge into a module; older versions have it on Zotero.Items.
let mergeItems;
try {
  mergeItems = ChromeUtils.importESModule("chrome://zotero/content/mergeItems.mjs").mergeItems;
} catch (e) {
  mergeItems = (kept, others) => Zotero.Items.merge(kept, others);
}

for (const set of duplicateSets.slice(0, MAX_SETS)) {
  const [kept, ...others] = set;
  const entry = {
    keep: kept.key,
    title: kept.getDisplayTitle(),
    trash: others.map(o => o.key),
    before: set.map(snapshot),
  };
  if (!DRY_RUN) {
    // The state may have changed since the scan (or since the dry run was read).
    // Also still in the scoped collection(s): an item moved out since the scan is not ours to merge.
    const stillDuplicates = set.every(i => !i.deleted && collections.some(c => i.inCollection(c.id)))
      && others.every(o => sameWork(kept, o));
    if (!stillDuplicates) {
      result.skipped.push({ keep: kept.key, trash: entry.trash, reason: "changed since the scan; left alone" });
      continue;
    }
    try {
      await mergeItems(kept, others);
    } catch (e) {
      // Stop at the first failure: a second failure on top of the first is harder to untangle.
      entry.error = String(e);
      result.sets.push(entry);
      result.stoppedEarly = true;
      break;
    }
    entry.merged = true;
    entry.nowInTrash = others.map(o => o.deleted);
  }
  result.sets.push(entry);
}
result.setsNotProcessed = Math.max(0, duplicateSets.length - MAX_SETS);
return result;
