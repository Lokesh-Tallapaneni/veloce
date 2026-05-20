# Design: `secure_filename` and `safe_join` (SEC11, SEC12)

## Contract

Two filesystem-safety helpers, derived from the OWASP Path Traversal
cheatsheet and CWE-22:

```python
from veloce import secure_filename, safe_join

# Sanitise a user-supplied filename to a safe basename.
secure_filename("../etc/passwd")    # → "etc_passwd"
secure_filename("résumé.pdf")       # → "resume.pdf"
secure_filename("CON.txt")          # → "_CON.txt"   (Windows reserved)
secure_filename("")                 # → ""           (empty = reject)

# Join `directory` and `*paths`; return None on any escape attempt.
safe_join("/srv/uploads", "file.txt")        # → "/srv/uploads/file.txt"
safe_join("/srv/uploads", "../etc/passwd")   # → None
safe_join("/srv/uploads", "/etc/passwd")     # → None  (absolute arg)
safe_join("/srv/uploads", "file\x00.txt")    # → None  (NUL byte)
```

`secure_filename` returns a basename (no separators); `safe_join` returns
an absolute joined path, or `None`.

## Observable behavior

### `secure_filename(name)`

1. NFKD-normalise unicode, then encode-decode through ASCII — drops combining
   marks and any glyph without an ASCII form.
2. Replace `/`, `\`, and `os.sep` with `_` so each path component visibly
   collapses.
3. Replace any other character outside `[A-Za-z0-9_.\-]` with `_`.
4. Strip leading/trailing `.`, `_`, ` ` so `..`, `...`, `   ` reduce to `""`.
5. Collapse repeated `_` to a single `_`.
6. Prefix `_` if the stem (text before the first `.`) is a Windows
   reserved device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`).

### `safe_join(directory, *paths)`

1. Reject empty `directory`.
2. Reject any `path` component that is absolute (`os.path.isabs`) — if
   `os.path.join` were given one, it would silently discard `directory`.
3. Reject any component containing `\x00` (NUL byte injection).
4. Build the joined path via `os.path.abspath(os.path.join(base, *paths))`,
   which collapses `..` segments.
5. Accept iff the resolved path equals `base` or starts with `base + os.sep`.
   The trailing separator check guards against `base="/srv/a"` accepting
   `/srv/abc` as a child.

## What this does **not** do

- Does **not** resolve symbolic links. A symlink inside `directory` that
  points outside it will pass `safe_join`. Callers that distrust symlinks
  must apply `os.path.realpath` themselves before opening.
- Does **not** check filesystem existence. Path validation only.
- Does **not** strip extensions or content-sniff file types. Callers
  that want to enforce an allow-list of extensions must do so themselves.

## Integration

- `veloce.helpers.send_from_directory` and `send_from_directory_async`
  now use `safe_join` instead of an `os.path.normpath` + prefix check.
  Any escape attempt returns 403 instead of 200 with the wrong file.
- `veloce.contrib.staticfiles.StaticFiles.handle` uses `safe_join` for
  the same reason — fixes a class of path-traversal CVEs that the
  prior prefix check could miss (e.g. `base="/srv/a"` requesting `/srv/abc`
  via a constructed query).

## Hot-path budget

Both helpers are pure-Python and called once per static-file request.
`secure_filename` is dominated by the unicode NFKD pass; `safe_join` by
two `os.path.abspath` calls. Combined cost ≪ 10 µs per call on the test
machine. Static file requests are I/O-bound, so this is negligible.

## Public API

Exported from the top-level `veloce` package:

```python
from veloce import secure_filename, safe_join
```

The function signatures are veloce's own.

## References

- OWASP Path Traversal cheatsheet
- CWE-22 (Improper Limitation of a Pathname to a Restricted Directory)
- `os.path.abspath`, `os.path.isabs` (CPython stdlib)
