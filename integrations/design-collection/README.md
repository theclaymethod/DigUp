# Design collections

This fork uses DigUp's native document, PDF, image/OCR, video/frame, audio, and code readers for a file-authored collection. The collection remains canonical; the corpus, SQLite stores, vectors, and receipts are disposable local output. Nothing here uploads a collection.

## Build

Use Swift 6.2 or newer. On macOS, build the Metal runtime first:

```sh
scripts/build-llama.sh
DIGUP_HEADLESS=1 swift build -c release --product digup
DIGUP_HEADLESS=1 swift test -c release --filter DigUpKitTests
```

`DIGUP_HEADLESS=1` omits the optional macOS UI and Sparkle dependency, so engine checks can use an older macOS SDK. Other platforms need the dependencies documented in the main README. Download the pinned EmbeddingGemma 2 models with `digup download-model`; an existing DigUp model installation can be reused read-only. `DIGUP_MODELS` selects an explicit model directory.

## Use

Each `entries/<id>/entry.json` describes a title, summary, tags, platforms, collection, kind, and visibility (`private`, `gallery`, or `portfolio`). Store notes and original files beside it. Public entries must explicitly select every file through `publish.files`; `publish.notes` also requires `notes.md` in that list.

```sh
python3 integrations/design-collection/design_collection.py --root /path/to/design index
python3 integrations/design-collection/design_collection.py --root /path/to/design status
python3 integrations/design-collection/design_collection.py --root /path/to/design search 'ornate loading animation'
python3 integrations/design-collection/design_collection.py --root /path/to/design search 'code: interpolate waveform'
python3 integrations/design-collection/design_collection.py --root /path/to/design search 'shader' --scope public
python3 integrations/design-collection/design_collection.py --root /path/to/design serve
```

Python 3.11+ and Git are required; no Python ML environment is needed. `--index` chooses a separate derived directory; the default is `<collection>/.generated/digup`, never the personal DigUp index. `--digup` or `DESIGN_DIGUP_BINARY` selects the compiled fork binary.

Indexing snapshots regular source files, excluding secrets, symlinks, dependencies, and build output. Snapshots use content hashes and atomic replacement. A writer lock prevents overlapping builds. The native indexing service resumes completed files, backfills long documents, enforces memory and per-file time limits, and releases its model on exit. Receipts distinguish complete, degraded, failed, and interrupted work. Skipped or unreadable files remain visible in native state counts; they are not silently declared embedded.

Queries group native hits by canonical entry while retaining original source paths and page, time, or line locations. Facets and eligible paths apply **before** keyword truncation and semantic calibration. `code:` selects the dedicated code index. Without it, descriptions and media are searched. The native ranker combines meaning and word coverage; `keyword` disables the query encoder. The query helper stays warm and releases its model and vectors after 60 idle seconds.

`Collection.extracted_text(scope=...)` projects the same readers' stored text for browser BM25, without vectors or machine paths. It bounds each entry to 180,000 characters. Audio is embedded semantically; this integration does not invent transcripts. Public search and text export use a separate corpus containing only selected files and public metadata. A changed manifest immediately invalidates that entry's public snapshot until it is rebuilt.

## Service boundary

The adapter's `serve` command accepts JSON lines: `{"id":1,"op":"status"}` or `{"id":2,"op":"search","q":"shader","collection":"...","tag":"...","scope":"all"}`. It opens no network listener. Web and desktop callers should use this same adapter. A web proxy must authenticate the owner before private queries, validate returned IDs against its published catalog, strip machine paths, and fall back to its own BM25 projection when the local machine is unavailable. Never expose DigUp's raw stdio protocol over HTTP.

The fork's native `search-service` ready event advertises `path-scope-v1`. Its optional `paths` request field is an exact allowlist; `[]` returns no hits and omission preserves upstream behavior. The adapter refuses binaries without this capability. Keep private manifests, snapshots, and verification logs outside this public repository.
