# Design collection integration

Keep authored collections outside this public repository. Derived corpus, indexes, model files, and verification logs belong in ignored local output. Preserve upstream licenses. Never change publication tiers or broaden `publish.files` during indexing.

The native engine owns extraction and ranking. Python owns canonical entry grouping, facets, provenance, snapshots, and public projection. Keep desktop and web on this one adapter. Private network access must be authenticated by the embedding application's owner session and a distinct origin credential; public requests must always use the public projection.

No new test files without user approval. Run existing engine tests and direct stdio/runtime checks. Report pending, skipped, failed, and partially read files honestly.
