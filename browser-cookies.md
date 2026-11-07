# Retrieving browser cookies (as yt-dlp does it)

This note documents how `yt-dlp` extracts cookies from the user's web browser, and
which of its modules/APIs are practical to reuse in `usernotes`. The CLI will need
an authenticated Reddit session to read/write the wiki, and "use the cookies from my
logged-in browser" is the least surprising way for a moderator to supply one.

Line numbers below refer to the local read-only checkout
`../yt-dlp` at commit `412e7f0a4b0588fe884a02b344ac66a06538475b`
(version `2026.10.26`).

## 1. Summary

yt-dlp does not talk to the browser through an extension or DevTools protocol. It
reads the browser's own on-disk cookie store directly:

* **Firefox** — SQLite database `cookies.sqlite` (`moz_cookies` table); plaintext
  values. Optional Firefox "container" filtering via `containers.json`.
* **Chromium-family** (chrome, chromium, brave, edge, opera, vivaldi, whale) —
  SQLite database `Cookies` in the profile directory. Values are usually encrypted
  by the OS credential store; yt-dlp decrypts them itself (AES-CBC on Linux/macOS,
  AES-GCM or DPAPI on Windows).
* **Safari** — Apple's binary `Cookies.binarycookies` format, parsed by hand.

Everything is normalised into one
`http.cookiejar.MozillaCookieJar` subclass (`YoutubeDLCookieJar`), which can then
produce a `Cookie:` header for a URL. The implementation is pure Python except for
optional `secretstorage` (GNOME keyring) and platform commands/APIs (`security`,
`kwallet-query`, DPAPI via `ctypes`).

All of this lives in one module: **`yt_dlp/cookies.py`** (~1420 lines). The AES
primitives it needs are in **`yt_dlp/aes.py`** (also pure Python, no OpenSSL
dependency).

## 2. Public-ish entry points

| Symbol | Location | What it does |
| --- | --- | --- |
| `SUPPORTED_BROWSERS`, `CHROMIUM_BASED_BROWSERS` | `yt_dlp/cookies.py:49-50` | Set of accepted browser names. |
| `extract_cookies_from_browser(browser_name, profile=None, logger=YDLLogger(), *, keyring=None, container=None)` | `yt_dlp/cookies.py:116` | Dispatcher; returns a `YoutubeDLCookieJar`. This is the simplest thing to call. |
| `load_cookies(cookie_file, browser_specification, ydl)` | `yt_dlp/cookies.py:93` | Combines an optional browser spec and an optional Netscape `cookies.txt`, merging the jars. Wraps failures in `CookieLoadError`. |
| `YoutubeDLCookieJar` | `yt_dlp/cookies.py:1276` | `MozillaCookieJar` subclass: Netscape file load/save, `get_cookie_header(url)`, `get_cookies_for_url(url)`. |
| `LenientSimpleCookie` | `yt_dlp/cookies.py:1165` | Tolerant parser for raw `Cookie:` header strings. |
| `parse_safari_cookies(data, jar=None, logger=...)` | `yt_dlp/cookies.py:723` | Parse binarycookies bytes without touching the filesystem. |
| `CookieLoadError` | `yt_dlp/cookies.py:89` | Error type for the above. |

The CLI option is defined at `yt_dlp/options.py:1547` as:

```
--cookies-from-browser BROWSER[+KEYRING][:PROFILE][::CONTAINER]
```

and parsed into a `(browser_name, profile, keyring, container)` tuple by the regex
at `yt_dlp/__init__.py:396-416`. `YoutubeDL.cookiejar`
(`yt_dlp/YoutubeDL.py:4217`) is a cached property that calls `load_cookies()`.

Note `YDLLogger()` constructed with no arguments is silent (`_YDLLogger.__init__`
in `yt_dlp/utils/_utils.py:5692` only logs when a `ydl` object is attached), so the
extraction functions can be called standalone without a `YoutubeDL` instance.

## 3. Common data flow

1. Parse the browser spec into `(browser, profile, keyring, container)`.
2. Locate the newest matching cookie database under the platform's browser
   directories.
3. **Copy the database to a temporary directory before opening it**
   (`_open_database_copy`, `yt_dlp/cookies.py:1112`). Browsers hold SQLite locks on
   the live file; copying also avoids writing to the user's profile. yt-dlp opens
   it read-only in spirit — the copy is deleted with the temp directory.
4. Query the cookie table, build `http.cookiejar.Cookie` objects, and add them to a
   `YoutubeDLCookieJar`.
5. For Chromium, decrypt the `encrypted_value` column first using a platform-specific
   `ChromeCookieDecryptor`.
6. Merge browser jars and/or a Netscape-format file (`_merge_cookie_jars`,
   `yt_dlp/cookies.py:1141`).
7. Call `jar.get_cookie_header(url)` to get the header value to send.

Cookie attribute mapping worth preserving:

* Chromium `expires_utc = 0` means a session cookie → set `expires=None`
  (`_process_chrome_cookie`, `yt_dlp/cookies.py:384-387`).
* Firefox schema ≥ 16 (Firefox 142+) stores expiry in **milliseconds**; yt-dlp
  divides by 1000 (`_extract_firefox_cookies`, `:189-192`).
* Netscape `expires` of `0` is treated as a session cookie on load, and written
  back as `0` on save (`YoutubeDLCookieJar.load/save`, `:1345-1403`).

## 4. Browser-specific mechanics

### Firefox (`_extract_firefox_cookies`, `yt_dlp/cookies.py:127`)

* Paths from `_firefox_browser_dirs()` (`:203`): Windows `%APPDATA%\Mozilla\Firefox\Profiles`
  plus the packaged-app path; macOS `~/Library/Application Support/Firefox/Profiles`;
  Linux `$XDG_CONFIG_HOME/mozilla/firefox` (Firefox 147+), `~/.mozilla/firefox`,
  and the Flatpak/Snap paths. Databases found by globbing for `cookies.sqlite`
  (`_firefox_cookie_dbs`, `:228`).
* Query: `SELECT host, name, value, path, expiry, isSecure FROM moz_cookies`
  (`:182`). No decryption.
* Containers: reads `containers.json` next to the DB and resolves a container name
  (or its `l10nID`) to a `userContextId`, then filters on `originAttributes`
  (`:149-180`).
* Max known schema version is 17 (`:128`).

### Chromium family (`_extract_chrome_cookies`, `yt_dlp/cookies.py:294`)

* Directory and keyring names per browser/platform from
  `_get_chromium_based_browser_settings` (`:234`); Opera has no profiles.
* Finds the newest file named `Cookies` under the profile root (`:316`,
  `_find_files`, `:1129`).
* Reads `meta` key `version`; a value ≥ 24 means decrypted plaintext carries a
  32-byte SHA-256 hash prefix that must be stripped (`:326-328`, and the
  `hash_prefix` argument in the decrypt helpers).
* Query: `SELECT host_key, name, value, encrypted_value, path, expires_utc, secure
  FROM cookies`; the column is `is_secure` in newer schemas, else `secure`
  (`:333-336`).
* A row is encrypted when `value` is empty and `encrypted_value` is non-empty;
  plaintext rows are used as-is (`:377-382`).
* Decryptor selection: `get_cookie_decryptor` (`:430`) by `sys.platform`.

| Platform | Class | Key derivation | Cipher |
| --- | --- | --- | --- |
| Linux | `LinuxChromeCookieDecryptor` (`:438`) | PBKDF2-SHA1, salt `b'saltysalt'`, 1 iteration, 16 bytes; password `b'peanuts'` for v10, OS keyring password for v11 | AES-128-CBC, IV = 16 spaces; falls back to an empty password; tries both keys |
| macOS | `MacChromeCookieDecryptor` (`:494`) | same PBKDF2 but 1003 iterations; password from `security find-generic-password -w -a <name> -s "<name> Safe Storage"` (`_get_mac_keyring_password`, `:995`) | AES-CBC; non-v10 data treated as plaintext |
| Windows | `WindowsChromeCookieDecryptor` (`:528`) | key from the profile's `Local State` JSON: base64 `os_crypt.encrypted_key`, `DPAPI` prefix stripped, decrypted with `CryptUnprotectData` (`_get_windows_v10_key`, `:1013`; `_decrypt_windows_dpapi`, `:1073`) | AES-256-GCM for v10 (12-byte nonce, 16-byte tag); other prefixes DPAPI |

* Linux keyring selection: `_choose_linux_keyring` (`:845`) maps the desktop
  environment (`_get_linux_desktop_environment`, `:775`) to KWallet 4/5/6, GNOME
  keyring, or basic-text. KWallet is read by invoking `kwallet-query`
  (`_get_kwallet_password`, `:915`); GNOME keyring through the optional
  `secretstorage` package over D-Bus (`_get_gnome_keyring_password`, `:958`).
  Basic-text returns no password (`:989-991`).
* Supported keyring names are the members of `_LinuxKeyring` (`:760-772`).
* Windows requires running as the same user account as the browser (DPAPI).

### The keyring guess, and the one place `usernotes` departs from yt-dlp

Which keyring a Chromium store was encrypted with cannot be read out of the
store: it has to be guessed, and gotten right, or the `v11` cookies - the ones
the browser encrypted with a key it keeps in the keyring - are dropped.

* Chromium picks its key store from `--password-store`, or from the desktop
  environment when that is not given. The current implementation is
  `FreedesktopSecretKeyProvider::GetKey()`
  (`components/os_crypt/async/browser/freedesktop_secret_key_provider.cc`):
  KDE 3/4/5/6 use KWallet, **every other desktop - including one it cannot
  name - uses the freedesktop secret service**, the libsecret D-Bus API that
  gnome-keyring answers. The key is a random secret stored under the label
  `<BROWSER> Safe Storage`, and the AES key is PBKDF2-SHA1 over that secret.
* yt-dlp's `_get_linux_desktop_environment` (`:775`) mirrors the *older*
  Chromium, which read an unrecognised desktop as basic-text, and
  `_choose_linux_keyring` follows it: `OTHER` (and KDE3, LXQt) means
  `BASICTEXT`, i.e. "no keyring, so no `v11` keys". That is why plain
  `--cookies-from-browser chromium` drops `v11` cookies on desktops neither
  routine knows - MATE, whose `XDG_CURRENT_DESKTOP` is `MATE` and whose
  `DESKTOP_SESSION` from lightdm is `lightdm-xsession`, is one. yt-dlp's answer
  is to name the keyring (`chromium+gnomekeyring`); see
  [yt-dlp#12885](https://github.com/yt-dlp/yt-dlp/issues/12885).
* `usernotes` keeps the guess, but questions it: when it answered basic-text
  and a `v11` cookie turns up (`LinuxChromeCookieDecryptor._v11_key`), that is
  proof a keyring was used after all, so the freedesktop secret service is
  asked before the cookie is given up on. The lookup happens at most once per
  extraction, and only when a `v11` cookie needs it - a store that has none,
  which is what a basic-text browser writes, never touches the keyring at all.
  An explicit `+keyring` is obeyed rather than second-guessed.
* Reading the secret service needs the optional `secretstorage` package
  (`pip install 'usernotes[gnome-keyring]'`). Without it a `v11` cookie is
  reported as undecryptable, with the reason, and `--cookies FILE` still works.
* Three things about *how* the item is read are `usernotes`' own, all of them
  because a keyring that cannot be read is a store whose `v11` cookies cannot
  be decrypted - not a store that cannot be read at all:
  * **A failure is not fatal.** yt-dlp lets anything the secret service raises
    escape, which costs every cookie in the store rather than the ones that
    need the key. Here it is logged and answers "no key", so the `v10` cookies
    - the majority in practice - are still read, and the command line says what
    is missing.
  * **The item is searched for, not walked to.** yt-dlp looks at every item in
    the collection, and secretstorage reads an item's label as it builds the
    item: one path in the keyring that no longer resolves (which secretstorage
    reports as `Item does not exist!`) stops that walk before the item that
    matters, and the browser's cookies go with it. The search is the one
    chromium itself makes, `{xdg:schema:
    chrome_libsecret_os_crypt_password_v2}`, and each returned path is built
    into an item on its own so that a path that is gone costs that item and
    nothing else.
  * **The default collection is read, not created.** secretstorage's
    `get_default_collection` creates a collection when the keyring has none;
    reading cookies has no business writing to a keyring, so the `default`
    alias is read directly where secretstorage offers that, and a keyring
    without one is reported instead.
* Every one of those edits is marked with a `# usernotes:` comment, like the
  rest of the changes to the vendored code.

### Safari (`_extract_safari_cookies`, `yt_dlp/cookies.py:568`)

* macOS in practice. The platform guard also accepts `sys.platform == 'ios'`, but
  the paths it probes are macOS ones. Default path
  `~/Library/Cookies/Cookies.binarycookies`, falling back to the sandboxed
  container path (`:577-584`).
* `parse_safari_cookies` (`:723`) parses the `cook` signature, big-endian page
  sizes, and per-record offsets (`DataParser`, `:598`; header/page/record parsers
  at `:653`, `:661`, `:681`). Expiry is Mac absolute time (epoch 2001-01-01),
  converted at `:649`.
* This is the only format parsed without SQLite.

## 5. Reusing the pieces in `usernotes`

### Option A — depend on yt-dlp as a library

```python
from yt_dlp.cookies import extract_cookies_from_browser

jar = extract_cookies_from_browser(
    'firefox',            # or chrome/brave/edge/...
    profile=None,         # None = newest profile
    keyring=None,         # e.g. 'gnomekeyring' / 'kwallet6' on Linux
    container=None,       # Firefox only; 'none' = no container
)
cookie_header = jar.get_cookie_header('https://www.reddit.com')
```

Pros: upstream keeps tracking browser DB schema and OS crypto changes. Cons:
yt-dlp is a large dependency, and `cookies.py` is internal, not a documented
stable API; its imports reach into `yt_dlp.utils`, `yt_dlp.minicurses`, etc. If we
pin a version we control breakage. `get_cookie_header`/`get_cookies_for_url` and
`extract_cookies_from_browser` have been stable in practice.

### Option B — vendor/adapt the module

Copy `yt_dlp/cookies.py` and `yt_dlp/aes.py` (both Unlicense — public domain; see
`../yt-dlp/LICENSE`) and replace the handful of yt-dlp helpers it imports with
local equivalents. The external surface is small:

* a logger object with `debug/info/warning/error` and optional `progress_bar()`;
* `Popen.run(...)` (thin wrapper over `subprocess`);
* `expand_path`, `is_path_like`, `str_or_none`, `try_call`, `sanitize_url`,
  `normalize_url`, `DownloadError`/`YoutubeDLError`, `write_string`,
  `error_to_str`;
* `MultilinePrinter`/`QuietMultilinePrinter` for the progress bar — a no-op stub is
  fine;
* optional `secretstorage`; stdlib `sqlite3`.

The only real third-party dependency is `secretstorage`, and only for GNOME-keyring
Chromium cookies on Linux. Vendoring keeps our install footprint to "standard
python3 plus one optional package" and lets us trim progress-bar noise. The cost is
maintaining the decryption and browser-location tables ourselves as browsers change.

**Recommendation for this project:** vendor `cookies.py`/`aes.py` behind a small
`usernotes.cookies` facade exposing just what we need — `load_cookie_header(url)`
and maybe `load_cookie_jar()` — so the upstream-derived code is isolated and can be
refreshed from a newer yt-dlp checkout. Reusing the `YoutubeDLCookieJar` class as-is
also gives us Netscape-file import/export for free (`--cookies FILE`), which is
useful for testing without touching a real browser.

### Reddit-specific notes

* We only need cookies whose domain covers `reddit.com`; all extracted cookies are
  returned, so filter with `jar.get_cookies_for_url('https://www.reddit.com')` or
  just build the header for that URL. Reddit's session cookie is typically
  `reddit_session`.
* The wiki API additionally requires a `uh` (modhash) parameter. Toolbox obtains it
  from `/api/me.json` as `data.modhash`
  (`../reddit-moderator-toolbox/extension/data/tbapi.ts:216-219`), not from a
  cookie; the CLI will need to fetch it the same way after authenticating with the
  extracted session cookies.
* Reddit may set cookies on `.reddit.com` and on `old.reddit.com`/`www.reddit.com`.
  `MozillaCookieJar` domain matching handles the leading-dot/subdomain cases, but
  verify with the actual header produced.
* Cookies are bearer credentials: never print the jar or header in normal output,
  never log them at debug level, and never write them to git. Any cache file should
  be mode `0600`. This is the same rule as the existing instruction not to commit
  `docker-claude-agent.sh`.

### Behavior to preserve if adapting

* Copy the database before opening; close cursors/connections; use
  `tempfile.TemporaryDirectory`.
* Prefer the **newest** matching profile/database by mtime (`_newest`, `:1125`).
* Decode DB text columns as bytes (`cursor.connection.text_factory = bytes`) because
  hostnames/values may not be valid UTF-8 in every row; decode per field.
* Treat undecryptable cookies as skipped, not fatal; report a count.
* Session cookies matter for Reddit logins — keep `discard=True` cookies when
  handing the jar to the HTTP layer. yt-dlp's own save path preserves them as
  `expires=0`.
* Chromium `meta.version >= 24` hash-prefix handling and Firefox schema ≥16
  millisecond expiry are easy to get wrong; keep those branches.
* Firefox container support and the `keyring` argument are optional for us but come
  along with the code.

### Caveats

* This is inherently version-fragile: browser vendors change profile layout and
  cookie encryption. yt-dlp tracks it; a fork must be refreshed periodically.
* On Windows, DPAPI decryption only works as the browser's user; on macOS, the
  `security` CLI may prompt for keychain access; on Linux, a locked keyring may
  prompt or fail. Plan for a clear error message and an alternative `--cookies FILE`
  path.
* Chromium can keep the DB locked; the copy-before-open approach is required.
* Safari support is macOS-only and parses a proprietary binary format.
* Extraction reads a *live* user profile — read-only, but privacy-sensitive. Do not
  cache extracted cookies longer than the process needs them.
* Licensing: yt-dlp is Unlicense/public domain, so copying is unencumbered. The
  Reddit Moderator Toolbox is a separate project with its own license; check before
  copying toolbox code (relevant to usernote encoding, not to cookie retrieval).

## 6. Files worth reading in the reference checkout

* `yt_dlp/cookies.py` — everything described above.
* `yt_dlp/aes.py` — `aes_cbc_decrypt_bytes`, `aes_gcm_decrypt_and_verify_bytes`,
  `unpad_pkcs7`, plus the underlying AES implementation.
* `yt_dlp/dependencies/__init__.py:32-52` — optional `secretstorage` and `sqlite3`
  import handling.
* `yt_dlp/utils/_utils.py:5692` — `_YDLLogger`, the minimal logger interface.
* `yt_dlp/utils/_legacy.py:219` — `YoutubeDLCookieProcessor`, urllib integration.
* `yt_dlp/networking/_urllib.py:368`, `_requests.py:305`, `_curlcffi.py:243`,
  `_websockets.py:131` — how the jar is plugged into each HTTP backend.
* `yt_dlp/options.py:1547`, `yt_dlp/__init__.py:396-416` — CLI grammar and parsing.
* `README.md:242` (dependency note) and `README.md:2278` (feature description).
