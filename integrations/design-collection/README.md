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

Python 3.11+ and Git are required. The adapter uses Python's standard library; optional native transcription has its own runtime requirements and may be unavailable independently of semantic audio embedding. `--index` chooses a separate derived directory; the default is `<collection>/.generated/digup`, never the personal DigUp index. `--digup` or `DESIGN_DIGUP_BINARY` selects the compiled fork binary.

Indexing snapshots regular source files, excluding secrets, symlinks, dependencies, and build output. Snapshots use immutable `corpus/<lane>/<entry>/<metadata-or-files>/<sha256>/<relative-name>` identities and atomic capture. Source size, nanosecond mtime, inode, device and nanosecond ctime must agree before and after capture. Writers verify captured bytes against the path's full SHA-256; unchanged source stamps can reuse a verified capture. Query freshness checks use those stamps rather than hashing large videos on every search. A writer lock prevents overlapping builds. The native indexing service resumes completed files, backfills long documents, enforces memory and per-file time limits, and releases its model on exit. Receipts distinguish complete, degraded, failed, and interrupted work. Skipped or unreadable files remain visible in native state counts; they are not silently declared embedded.

`collection.json` version 2 describes desired captures. Preparing it does not attest native text or vectors. The derived `indexed.json` records the source digest, capture identity, manifest revision and source stamp for each processed revision. After every native attempt, including failure or interruption, the writer reconciles `done`/`partial` files that have a content vector or extracted text beyond the filename, and verified terminal `skipped` rows. Skipped paths never enter search or the BM25 text projection, even if native rows retain old segments. Pending, failed, missing and filename-only rows cannot replace a usable revision. The writer commits indexed provenance before collecting corpus revisions that neither map references; multiple prepares can temporarily retain abandoned captures until that reconciliation. These maps are disposable build state, not another authored catalog. `text.json` retains its `entries` shape and records both desired `generation` and `indexedGeneration`.

Before indexing, the adapter requires native `discover --json` version 1. This command calls the same crawler with the actual lane options and does no indexing, estimation, speed measurement or model work. Native discovery owns file eligibility, including code size limits, git rules and project exclusions; Python does not copy those rules. The report names accepted candidates and explicit regular-file policy exclusions. Unknown types, unwalked folders, cloud placeholders, missing files and unreadable roots do not establish an exclusion. A missing decision leaves the source unresolved, retains any last-known private revision and keeps status stale.

Verified discovery exclusions live separately in `indexed.json` under `discovery`: exact desired generation, native binary SHA-256, per-lane report hashes/options and each excluded capture's identity, native metadata and reason. Before committing, the writer verifies the full snapshot SHA-256, contained nonsymlink path, source stamp, manifest and unchanged desired generation. An explicit current exclusion retires the superseded logical fallback without creating a native file row, search evidence or BM25 text. Status compares processed revisions plus verified exclusions with all desired captures and reports `excluded.files` and `excluded.reasons` separately from native `states.skipped`, pending and failed counts. Changing the binary or generation invalidates the exclusion binding until native discovery runs again.

Private code uses the code crawler; selected public code remains in the main/public document lane with `--include-code`. A private code exclusion cannot be applied to its public copy. Publication selection and immediate public revocation still apply independently. After a completed native run, a writer can refresh only discovery/provenance with `with collection.writer(): collection.reconcile(collection.discover())`; this does metadata discovery and derived provenance/garbage collection, without extraction or model work. Run it only as the collection's designated writer.

Status and completed receipts expose `media.transcription` and `media.captions` counts from native per-file outcomes. Transcription outcomes are `transcribed`, `no_speech`, `no_audio`, `unavailable`, `failed`, or `partial`; caption outcomes are `extracted`, `none`, `unavailable`, `failed`, or `partial`. Older indexes and unrecognized outcomes are `not_reported`, never inferred successful from an audio vector. These are counts of indexed snapshot files, which can include separate private/public copies. Native partial media remains visibly degraded; it is not scheduled as unfinished PDF/document backfill. Transcription, captions, decoding and ranking belong to the native engine; the adapter does not synthesize transcript text or hide native failures.

Queries group native hits by canonical entry while retaining original source paths and page, time, or line locations. Facets and eligible paths apply **before** keyword truncation and semantic calibration. `code:` selects the dedicated code index. Without it, descriptions and media are searched. The native ranker combines meaning and word coverage; `keyword` disables the query encoder. The query helper stays warm and releases its model and vectors after 60 idle seconds. Missing eligible indexed files return a keyword-mode availability notice without starting the helper; blank queries and empty eligibility also avoid encoder startup. A completed hybrid ranking with no hits remains a successful empty result. Public collection facets use `Other`, matching the public projection rather than exposing private canonical collection names. All-scope facets preserve authored names and treat missing or empty names as `Other`.

`Collection.extracted_text(scope=...)` projects the same readers' stored text for browser BM25, without vectors or machine paths. It bounds each entry to 180,000 characters. Native transcript and caption text participates in this projection when present; semantic audio vectors alone provide no keyword text. Explicit `.srt` and `.vtt` files are eligible sources, but adjacent sidecars are not implicitly discovered outside normal source enumeration or `publish.files`. Public search and text export use a separate corpus containing only selected files and public metadata. A changed manifest immediately invalidates that entry's public snapshot until it is rebuilt. Every query rereads canonical eligibility once. Python callers may supply a `records` subset to further restrict that fresh catalog; omitting it uses the current catalog. `include_entries=True` adds result metadata for CLI callers without gallery/source reads, and public scope omits private collection and reuse fields.

Timed native audio segments, including text-modality transcript/caption segments, group into `matches.audio`. Audio and frame locations retain `kind`, `source`, and compatible `seconds`, plus `start`/`end` when supplied by the native hit. Content is bounded to 400 characters. The adapter preserves native ranking order and does not infer word-level timing from segment ranges.

Last-known private snapshot search remains available when a source changes, disappears, or becomes a symlink, or while its prepared replacement is pending. Matched stale evidence has `stale: true`; its entry result also has `stale: true` and a bounded `notice`. The ranking has an aggregate `stale` flag, while its global `notice` stays reserved for search availability so callers need not discard usable old-index results. Status checks source stamps and generated metadata against canonical content using the same comparison. Private text projection deliberately remains snapshot-based; call `status` to check freshness before presenting it as current. Public search and text projection share the stricter eligibility rule: the indexed identity must match the desired capture, current manifest/publication policy and full source stamp. Public file allowlists apply before native ranking and before text export. A prepare-only refresh cannot publish old text under a new source fingerprint, including equal-length edits with preserved mtime or removed public notes.

Legacy version-1 maps or missing indexed provenance require a normal `index` run. Until immutable revisions are indexed, public hits and derived text are withheld; legacy private results remain explicitly stale and status reports `rebuildRequired`. A missing binary or interrupted startup writes a terminal failed/interrupted receipt and leaves usable private revisions resumable. A corrupted immutable capture or symlink is rejected rather than overwritten and trusted in place. Stop the adapter, preserve authored `entries/`, remove only the affected disposable derived index directory, then run `index` to rebuild it. Never delete authored content to repair an index.

Run the adapter regression checks without models:

```sh
python3 -B -m unittest discover -s integrations/design-collection -p 'test_*.py' -v
```

These checks use temporary collections and controlled native hit/SQLite inputs. They verify evidence ranges, null excerpts, stale snapshot reporting, caption selection and public revocation, transcript text projection, and native outcome aggregation; they do not establish ASR quality, codec coverage, or media playback.

## Local speech and captions

Speech recognition currently requires macOS, an already installed `mlx_whisper` executable, FFmpeg/FFprobe, and an already cached Whisper model. The default is `mlx-community/whisper-large-v3-turbo`. The reader accepts a complete local model directory or a complete Hugging Face cache snapshot containing `config.json` and `weights.safetensors` or `weights.npz`. It passes that local directory to Whisper with offline and telemetry-disabled settings; indexing never downloads a speech model or uploads media. Missing tools/model and failed recognition produce explicit unavailable/failed outcomes while usable semantic audio, frames or captions remain searchable.

`DIGUP_WHISPER_BIN` chooses the speech executable (`MLX_WHISPER_BIN` is also accepted). `DIGUP_WHISPER_MODEL` chooses the local model directory or cached repository (`MLX_WHISPER_MODEL` is also accepted). `HF_HUB_CACHE`/`HF_HOME` override the Hugging Face cache location. `DIGUP_FFMPEG_BIN` and `DIGUP_FFPROBE_BIN` choose explicit executables. Discovery also checks PATH, `~/.local/bin`, Homebrew and system locations, and adds the resolved FFmpeg directory to the speech subprocess PATH for Finder launches.

The native reader decodes complete audio streams independently of the semantic audio window's silence/short-file gate. It normalizes quiet audio, uses timed Whisper segments, and rejects segments that cover only digital silence. Spoken text and explicit `.srt`/`.vtt` or embedded text captions get native FTS rows, text vectors and start/end seconds. Caption files must be explicitly included in the indexed collection/public selection; the reader never opens an adjacent sidecar automatically. Bitmap subtitle codecs are reported unsupported rather than declared extracted.

Each file is capped at four audio tracks and four subtitle tracks, 1,800 seconds per track, and 8 MiB per transcript/caption text output. Limits produce partial coverage. Tool logs are capped at 4 MiB, decoding has a 60-second timeout, and each Whisper run has a 60–600-second timeout based on duration. The adapter's stricter **180-second per-file / 4 GiB** watchdog can stop the run sooner. That memory budget includes parent physical footprint plus owned child-process RSS. Speech runs after releasing the embedding worker, so the two models do not overlap. The macOS CLI's helper leases terminate only their owned process groups on cancellation, watchdog exit or parent death, including SIGKILL. Direct DigUpKit callers and other operating systems do not have that CLI lease or child-memory supervision. Media readers permit local file/pipe protocols only.

Existing audio/video and caption files are queued once when the media reader, resolved tools or cached model configuration changes. An unavailable/failed/partial outcome does not retry unchanged media on every query or indexing pass. After a transient failure, explicitly retry with the native CLI's `--reindex`, change the source, or change the reader/tool/model configuration. Successful transcription is recognition output, not a guarantee that every word or language was recognized correctly.

The opt-in runtime matrix generates neutral speech with the installed macOS `say` voice and videos with FFmpeg, runs the real cached recognizer/native index/helper, and removes its temporary media and indexes by default:

```sh
python3 scripts/testbed/check_speech.py --binary .build/release/digup
```

It covers standalone, short and quiet speech, audio-only MP4, delayed speech and a phrase across 30 seconds, multiple audio tracks, silence, corruption, standalone/embedded captions, native keyword/semantic retrieval, unchanged-source reuse, and child cleanup under cancellation/watchdog limits. `--keep` retains only that task-owned temporary output for adapter inspection. Ordinary `DigUpKitTests` cover parsing, timed text storage, unavailable outcomes, reader invalidation and subprocess bounds without running speech models.

For the complete collection boundary, run the native matrix with `--keep`, then pass the returned `retained` directory's `media` subdirectory to the companion check:

```sh
python3 scripts/testbed/check_speech_adapter.py --binary .build/release/digup --media /path/from/matrix/media
```

This performs a fresh `Collection.index_all()` into a disposable collection, then actual keyword/hybrid timed-video queries and text projection. It verifies exact public file selection, an unselected private caption, source-edit stale flags/public-text revocation, and manifest revocation. It also edits a task-owned caption to an equal-length replacement while restoring mtime: a prepare-only gap retains stale private A and excludes public A/B, a real native update exposes only current B, and an unchanged update preserves native indexing timestamps. It never rewrites native index rows or personal media. Both scripts use a stable task-owned cache path on macOS; `--keep` is optional for the companion check.

## Service boundary

The adapter's `serve` command accepts JSON lines: `{"id":1,"op":"status"}` or `{"id":2,"op":"search","q":"shader","collection":"...","tag":"...","scope":"all"}`. It opens no network listener. Web and desktop callers should use this same adapter. A web proxy must authenticate the owner before private queries, validate returned IDs against its published catalog, strip machine paths, and fall back to its own BM25 projection when the local machine is unavailable. Never expose DigUp's raw stdio protocol over HTTP.

The fork's native `search-service` ready event advertises `path-scope-v1`. Its optional `paths` request field is an exact allowlist; `[]` returns no hits and omission preserves upstream behavior. The adapter refuses binaries without this capability. A terminated or malformed helper stream closes the child before reporting a bounded error; the next request starts a fresh helper. Keep private manifests, snapshots, and verification logs outside this public repository.
