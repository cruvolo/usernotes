# How usernotes are encoded and stored in the subreddit wiki

This describes the on-wiki storage format used by reddit Moderator Toolbox's
"User Notes" module, as implemented in `../reddit-moderator-toolbox`. It is the
format any external tool (including this project's CLI) must read and write.

Sources (line numbers as of toolbox v7.0.0-beta.17):

| What | Where |
| --- | --- |
| Read / inflate | `extension/data/modules/usernotes.js:1211-1356` |
| Write / deflate | `extension/data/modules/usernotes.js:1359-1470` |
| Constant pools | `extension/data/modules/usernotes.js:1473-1489` |
| Link squashing | `extension/data/modules/usernotes.js:1491-1543` |
| Schema constants, default note types | `extension/data/tbcore.js:23-26`, `:116-124` |
| zlib helpers | `extension/data/tbhelpers.js:768-794` |
| Wiki API read/write | `extension/data/tbapi.ts:263-420` |

## 1. Location and transport

The whole note store is a single wiki page: **`/r/<subreddit>/wiki/usernotes`**.

* **Read** — `GET /r/<sub>/wiki/usernotes.json`, take `data.content_md` (the raw
  page text) and `JSON.parse()` it. Errors are normalised to `NO_WIKI_PAGE`
  when the page is missing/disabled, empty, *or unparseable*, and to
  `WIKI_PAGE_UNKNOWN` for anything else (`tbapi.ts:369-420`).
* **Write** — `POST /r/<sub>/api/wiki/edit` with `content` = the JSON text,
  `page=usernotes`, `reason`, `uh` = the modhash. The JSON is written minified
  (`JSON.stringify` with no indentation — "not indenting saves precious bytes",
  `tbapi.ts:307-310`). The revision reason is `"<reason>" via toolbox`
  (`tbapi.ts:301-305`). Immediately afterwards toolbox re-applies page settings:
  `POST /r/<sub>/wiki/settings/` with `listed=true`, `permlevel=2` (mod only)
  (`tbapi.ts:331-353`).
* A write that reddit rejects with **HTTP 413** is reported as
  **"usernotes full"** (`usernotes.js:1386-1387`); toolbox does not hardcode the
  size limit — the practical cap is reddit's per-page wiki content limit. This
  is the reason the store is compressed at all.
* Reads are cached in browser storage (`Utils.noteCache` / `Utils.noNotes`),
  and saves re-read with `forceSkipCache = true` before writing
  (`usernotes.js:1218-1231`, `:611`).

## 2. Top-level page document (schema v6 — the version Toolbox writes today)

```json
{
  "ver": 6,
  "constants": {
    "users": ["creesch", "geo1088"],
    "warnings": ["abusewarn", "gooduser", null, "ban"]
  },
  "blob": "eJyrVkpPzTc0sLBQsqpWyitWsoquVipRsjI0NbU0NjQwNzXQUcpTslLyLFEvVkhUKE5NLkotUSjJV0gtSy2qzM9LVdJRKgcq11HKBZK1sbW1AKKCF4Q="
}
```

* `ver` — schema version (`tbcore.js:23-26`: current/default write version **6**,
  minimum readable **4**, deprecated warning at **4**, max readable **6**).
* `constants` — two "constant pools" (§5). They stay in plaintext, so note
  authors and types are readable/indexable without decompressing the blob.
* `blob` — the entire per-user note map, zlib-deflated and base64-encoded (§3).

`ver` is bumped to 6 on every toolbox save (`usernotes.js:1363-1365`), and
`deflateNotes` additionally refuses to downgrade
(`usernotes.js:1423`), so in practice Toolbox only ever writes v6 today.

## 3. The `blob` encoding — exact byte format

`deflateNotes()` builds the deflated structure, then `compressBlob()` does
(`usernotes.js:1410-1417`):

```
blob = base64( zlib_deflate( utf8_bytes( JSON.stringify(users) ) ) )
```

and reading is the exact reverse (`usernotes.js:1308-1315`):

```
users = JSON.parse( utf8_decode( zlib_inflate( base64_decode(blob) ) ) )
```

Details that matter for a reimplementation:

* The compression is **zlib-wrapped DEFLATE (RFC 1950)**, not raw deflate and not
  gzip. Toolbox uses `pako` (`pako@^0.2.6`) with `new pako.Deflate({to: 'string'})`
  and default options → level `Z_DEFAULT_COMPRESSION` (= 6), `windowBits` 15
  (`tbhelpers.js:787-794`, `:773-780`).
* The base64 is produced with `btoa()` over the binary string pako emits
  (Latin-1, i.e. plain byte→char), and read with `atob()`. No line breaks, no
  URL-safe alphabet, standard `+/=` alphabet with padding.
* Python equivalents are exact:
  `base64.b64encode(zlib.compress(json.dumps(users, separators=(',',':')).encode(), 6))`
  produces byte-identical output to toolbox's pako path (verified locally against
  `libs/pako.min.js` v0.2.5), and
  `json.loads(zlib.decompress(base64.b64decode(blob)).decode('utf-8'))` reads it.
* The JSON inside the blob is UTF-8 with non-ASCII characters left raw:
  `JSON.stringify` does not emit `\uXXXX` escapes. In Python, use
  `json.dumps(..., ensure_ascii=False)` for byte-identical output (`ensure_ascii=True`
  still reads fine, it just produces different bytes).
* Note text is inside the blob, so it is stored as raw UTF-8 JSON — the base64
  alphabet cannot collide with HTML escaping.

## 4. The decoded users object

```json
{
  "geo1088": {
    "ns": [
      {"t": 1559310750, "n": "It's a secret to everyone", "w": 1, "m": 1},
      {"t": 1559310836, "n": "All your usernote are belong to us", "w": 2, "m": 0, "l": "l,bfgb5y,eldfyai"}
    ]
  },
  "creesch": {
    "ns": [{"t": 1559310623, "n": "heh", "w": 0, "m": 1, "l": null}]
  }
}
```

* Top-level keys are **reddit usernames, exactly as cased** (no `u/` prefix).
  Faulty third-party writers sometimes store a lower-cased key: on read Toolbox
  merges notes found under `name.toLowerCase()` into the canonical key, and on
  the next save it deletes the lower-cased entry, writing everything back under
  the canonical name (`usernotes.js:73-98`, `:675-679`).
* Each user object has exactly one key, `ns`, an array of note objects. Order is
  preserved as stored; Toolbox prepends new notes (`unshift`, `usernotes.js:671`)
  so `ns[0]` is normally the newest note, but a reader should sort by `t` rather
  than assume it (`getUser()` sorts descending, `usernotes.js:94`).
* Notes live per user; deleting a user's last note removes the user entry
  entirely (`usernotes.js:661-664`), and toolbox prunes such entries when saving.

Note object fields:

| Key | Type | Meaning |
| --- | --- | --- |
| `n` | string | Note text, stored verbatim (no encoding applied on write, `usernotes.js:1451`). |
| `t` | number | Creation time. **Seconds** since the Unix epoch for v5/v6; milliseconds for v4 (`usernotes.js:1350-1355`, `:1460-1469`). `0` is used for "no time" (deleted/unknown). |
| `m` | number | Index into `constants.users` — the moderator who wrote the note. |
| `w` | number | Index into `constants.warnings` — the note type key, or an index whose value is `null` when the note has no type. |
| `l` | string \| null | Squashed link (§6); `""`, `null`, or absent when there is no usable link. |

There is **no note id field**: Toolbox identifies a note by its timestamp
(seconds precision) when deleting, so two notes with the same second are
ambiguous (`usernotes.js:454-460`, `:653`).

## 5. Constant pools (`constants`)

`_constManager` (`usernotes.js:1473-1489`) is an intern table: `create(pool, value)`
returns the existing index of `value` or appends it and returns the new last
index; `get(pool, index)` reads back `pool[index]`.

* `constants.users` — moderator usernames (the *author* of notes), not the
  notees.
* `constants.warnings` — note **type keys** (e.g. `spamwatch`, `ban`), which are
  the `key` values from the subreddit config's `usernoteColors` (§7), not the
  display text.

Behaviour worth knowing:

* Toolbox **rebuilds both pools from scratch on every save**, in first-seen
  order over the users object and their `ns` arrays (`usernotes.js:1421-1445`);
  it does not preserve the pools it read. Indices are therefore valid only for
  the revision they were read from — never cache an index across revisions.
* A note created with no type makes `create('warnings', undefined)` return
  index 0 and push `undefined`, which serialises as **`null`** in JSON. So
  `constants.warnings` may legitimately contain `null`, and `w` may point at it.
* `get()` of an out-of-range or null index yields `undefined`/`null`, which
  Toolbox treats as "no type" and renders as `none` (`usernotes.js:315-325`,
  `:1069-1071`).
* Older schemas pooled usernames differently (§8).

## 6. Link squashing (`l`)

On write, `_squashPermalink` (`usernotes.js:1491-1517`) reduces a permalink:

| Stored form | Meaning | Expanded by Toolbox to |
| --- | --- | --- |
| `l,<submission_id>` | link to a submission | `/r/<sub>/comments/<submission_id>/` |
| `l,<submission_id>,<comment_id>` | link to a comment | `/r/<sub>/comments/<submission_id>/-/<comment_id>/` |
| `m,<thread_id>` | old modmail thread | `/r/<sub>/message/messages/<thread_id>` |
| full `https://mod.reddit.com/...` URL | new modmail | used as-is |
| `""` | anything unrecognised, or no link | (nothing) |

`_unsquashPermalink` (`usernotes.js:1519-1543`) does the reverse; it returns
`""` for forms it does not recognise, so a reader that wants robust links should
also accept arbitrary full URLs (Toolbox's own docs recommend that for legacy
support). The regexes are deliberately loose: the comment match is
`/\/comments\/(\w+)\/(?:[^/]+\/(?:(\w+))?)?/` and the modmail match is
`/\/messages\/(\w+)/`.

## 7. Note types and colors (stored elsewhere)

The note type key stored in `w` refers to the subreddit's general config page
**`/r/<sub>/wiki/toolbox`**, key `usernoteColors`, an array of
`{key, text, color}` (`config.js:1241`, `usernotes.js:1550-1560`). When that key
is absent, the defaults in `tbcore.js:116-124` apply:

```json
[
  {"key": "gooduser",  "color": "green",   "text": "Good Contributor"},
  {"key": "spamwatch", "color": "fuchsia", "text": "Spam Watch"},
  {"key": "spamwarn",  "color": "purple",  "text": "Spam Warning"},
  {"key": "abusewarn", "color": "orange",  "text": "Abuse Warning"},
  {"key": "ban",       "color": "red",     "text": "Ban"},
  {"key": "permban",   "color": "darkred", "text": "Permanent Ban"},
  {"key": "botban",    "color": "black",   "text": "Bot Ban"}
]
```

Keys are validated as `^[\w-]+$` and must be unique (`config.js:1204-1217`).
A `w` index that resolves to a key with no matching config entry is rendered as
type `none` with no color (`usernotes.js:1562-1570`).

## 8. Schema versions

Toolbox reads v4, v5 and v6 (`notesMinSchema = 4`), warns that v4 is deprecated,
and always writes v6.

* **v6** — as documented above: `{ver, constants, blob}`.
* **v5** — `{ver, constants, users}`: identical, but the users object is inline
  (uncompressed) instead of in `blob` (`usernotes.js:1399-1407`).
  *Caveat:* the official toolbox wiki page
  (`Subreddit Wikis: usernotes`) claims v5 uses a `data` key instead of `users`;
  that does not match the source, which has only ever used `users` for the
  uncompressed form (no `data` key appears anywhere in the module's git history).
  Trust the source.
* **v4** — identical to v5 except each note's `t` is in **milliseconds**
  (`usernotes.js:1350-1355`). Conversion is a length heuristic, applied only when
  `ver >= 5`: on read, a `t` with ≤10 digits is multiplied by 1000 to get
  milliseconds; on write, a `t` with >10 digits is divided by 1000 (truncated) to
  get seconds (`usernotes.js:1460-1469`). So a v4 store is read as-is.
* **v3 and older** — unsupported (`convertNotes` returns `null` →
  "usernotes schema too old to be understood", `usernotes.js:1259-1261`). v3 and
  earlier had the same note field names but `users` was an **array** of
  `{u, ns}` where `u` was an index into the username pool, and note `l` held a
  bare link id (no `l,` prefix).

## 9. Read/write behaviour to mirror

* The parsed page is passed through `purifyObject()` (DOMPurify-based
  sanitisation of string values) before use (`usernotes.js:12`, `:1256`).
* **Note text asymmetry:** toolbox writes `n` verbatim but applies
  `htmlDecode()` to it on read (`usernotes.js:1341`, `tbhelpers.js:764-766`), a
  leftover from the era when reddit HTML-encoded wiki JSON and note text was
  stored HTML-escaped. Consequently a note whose text literally contains an
  entity (say `&amp;`) is written unchanged and read back as `&`. For display
  toolbox then re-escapes (`htmlEncode`) — so a CLI that prints note text should
  decide deliberately whether to mimic this decode. Note text that is not
  entity-bearing is unaffected.
* Note text can contain arbitrary characters (including newlines); nothing
  strips them at storage time. The edit dialog only trims surrounding
  whitespace and rejects empty text (`usernotes.js:568-591`).
* Writing a new page: toolbox uses the skeleton
  `{ver: 6, constants: {}, users: {<user>: {notes: [<note>]}}}` with reason
  `create usernotes config` (`usernotes.js:601-620`), then saves normally.
* Save-reason strings toolbox generates include
  `create new note on user <u>`, `delete note <time> on user <u>`,
  `Updated notes to schema v6`, and prune descriptions
  (`usernotes.js:666-698`, `:1290`).
* Empty/invalid JSON on the page is treated as "no page" — a subsequent note
  save would therefore overwrite a corrupt page rather than fail. A backup tool
  should read the raw `content_md` and never rely on the parsed path for safety.

## 10. Minimal Python round-trip

```python
import base64, json, zlib

def read_notes(content_md: str) -> dict:
    page = json.loads(content_md)
    if page["ver"] == 6:
        users = json.loads(zlib.decompress(base64.b64decode(page["blob"])).decode("utf-8"))
    else:                      # v5 (and v4, whose t is in ms)
        users = page["users"]
    return {"ver": page["ver"], "constants": page["constants"], "users": users}

def write_notes_v6(constants: dict, users: dict) -> str:
    raw = json.dumps(users, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    blob = base64.b64encode(zlib.compress(raw, 6)).decode("ascii")
    return json.dumps({"ver": 6, "constants": constants, "blob": blob},
                      separators=(",", ":"), ensure_ascii=False)
```

Timestamps: for v6/v5 multiply `t` by 1000 for display; for v4 use `t` as-is.
