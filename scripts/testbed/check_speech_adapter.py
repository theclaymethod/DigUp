#!/usr/bin/env python3
"""Exercise actual collection snapshots, native indexing and retrieval using generated speech media.

First run check_speech.py with --keep; pass its retained media directory with --media.
Uses only task-owned copies and a temporary index. Requires the same installed tools/cached models.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--keep", action="store_true", help="retain only this task-owned collection/index")
    args = parser.parse_args()
    for name in ("delayed.mp4", "captions.srt"):
        if not (args.media / name).is_file():
            parser.error("--media must contain generated delayed.mp4 and captions.srt")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    os.environ.pop("X_API_REFRESH_TOKEN", None)
    os.environ.pop("X_MATTER_ALLOW_OAUTH_REFRESH", None)
    module = Path(__file__).resolve().parents[2] / "integrations/design-collection/design_collection.py"
    spec = importlib.util.spec_from_file_location("speech_adapter", module)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    cache = Path.home() / "Library/Caches" if sys.platform == "darwin" else Path(tempfile.gettempdir())
    temporary = contextlib.nullcontext(tempfile.mkdtemp(prefix="digup-speech-adapter-", dir=cache)) if args.keep else \
        tempfile.TemporaryDirectory(prefix="digup-speech-adapter-", dir=cache)
    with temporary as directory:
        root = Path(directory).resolve()
        entry = root / "entries/demo"
        (entry / "assets").mkdir(parents=True)
        shutil.copy2(args.media / "delayed.mp4", entry / "assets/selected.mp4")
        shutil.copy2(args.media / "captions.srt", entry / "assets/unselected.srt")
        revision = entry / "assets/revision.vtt"
        text_a = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nAn indigo marker is drawn.\n"
        text_b = text_a.replace("indigo marker", "orange banner")
        assert len(text_a.encode()) == len(text_b.encode())
        revision.write_text(text_a)
        manifest = {"title": "Neutral media", "tier": "gallery", "publish": {
            "files": ["assets/selected.mp4", "assets/revision.vtt"]}}
        adapter.save(entry / "entry.json", manifest)
        library = adapter.Collection(root, binary=args.binary)
        checks = {}
        try:
            started = time.monotonic()
            with contextlib.redirect_stdout(io.StringIO()):
                receipt = library.index_all()
            elapsed = round(time.monotonic() - started, 2)
            private = library.search(query="silver compass", mode="keyword", scope="all")
            public = library.search(query="silver compass", mode="hybrid", scope="public")
            private_caption = library.search(query="copper telescope", mode="keyword", scope="all")
            public_caption = library.search(query="copper telescope", mode="keyword", scope="public")
            all_text = library.extracted_text(scope="all")
            public_text = library.extracted_text(scope="public")

            def timed(result):
                matches = [row.get("matches", {}).get("audio", {}) for row in result.get("results", [])]
                return any(match.get("location", {}).get("source") == "assets/selected.mp4" and
                           match["location"].get("start", 0) >= 32 and
                           match["location"].get("seconds") == match["location"]["start"] and
                           match["location"].get("end", 0) > match["location"]["start"] and
                           "compass" in match.get("content", "").lower() for match in matches)

            checks["actual_adapter_index_complete"] = receipt["status"] == "complete"
            checks["keyword_private_timed_video"] = timed(private)
            checks["hybrid_public_timed_video"] = timed(public) and public["mode"] == "hybrid" and not public.get("notice")
            checks["unselected_caption_private_search"] = bool(private_caption["results"])
            checks["unselected_caption_public_search_excluded"] = not public_caption["results"]
            checks["private_text_contains_selected_and_unselected"] = "compass" in all_text.get("demo", "").lower() and \
                "telescope" in all_text.get("demo", "").lower()
            checks["public_text_exact_selection"] = "compass" in public_text.get("demo", "").lower() and \
                "telescope" not in public_text.get("demo", "").lower()

            first_state = adapter.read(library.manifest)
            first_paths = {path for path, info in first_state["files"].items()
                           if info["source"] == "assets/revision.vtt"}
            checks["revision_a_native_public_search"] = bool(library.search(
                query="indigo marker", mode="keyword", scope="public")["results"])
            checks["revision_a_native_public_text"] = "indigo marker" in library.extracted_text(scope="public").get("demo", "")
            before = revision.stat()
            revision.write_text(text_b)
            os.utime(revision, ns=(before.st_atime_ns, before.st_mtime_ns))
            with library.writer():
                library.prepare()
            prepared = adapter.read(library.manifest)
            next_paths = {path for path, info in prepared["files"].items()
                          if info["source"] == "assets/revision.vtt"}
            checks["same_size_mtime_new_immutable_identity"] = revision.stat().st_size == before.st_size and \
                revision.stat().st_mtime_ns == before.st_mtime_ns and first_paths.isdisjoint(next_paths)
            gap = library.search(query="indigo marker", mode="keyword", scope="all")
            checks["prepare_only_gap_keeps_private_a_stale"] = bool(gap["results"]) and gap["stale"] and \
                "indigo marker" in library.extracted_text().get("demo", "")
            gap_text = library.extracted_text(scope="public").get("demo", "")
            checks["prepare_only_gap_excludes_public_a_and_unindexed_b"] = not library.search(
                query="indigo marker", mode="keyword", scope="public")["results"] and \
                "indigo marker" not in gap_text and "orange banner" not in gap_text
            checks["old_private_capture_retained_during_gap"] = all(Path(path).is_file() for path in first_paths)
            with contextlib.redirect_stdout(io.StringIO()):
                replacement = library.index_all()
            new = library.search(query="orange banner", mode="keyword", scope="public")
            replaced_text = library.extracted_text(scope="public").get("demo", "")
            checks["native_replacement_b_is_current"] = replacement["status"] == "complete" and \
                bool(new["results"]) and not new["stale"] and "orange banner" in replaced_text
            checks["native_replacement_a_is_retired"] = not library.search(
                query="indigo marker", mode="keyword", scope="public")["results"] and \
                "indigo marker" not in replaced_text and all(not Path(path).exists() for path in first_paths)
            active = set(adapter.read(library.indexed)["files"])
            with sqlite3.connect(library.index / "index.sqlite") as db:
                indexed_before = {path: when for path, when in db.execute("SELECT path,indexed_at FROM files") if path in active}
            with contextlib.redirect_stdout(io.StringIO()):
                unchanged = library.index_all()
            with sqlite3.connect(library.index / "index.sqlite") as db:
                indexed_after = {path: when for path, when in db.execute("SELECT path,indexed_at FROM files") if path in active}
            checks["unchanged_update_preserves_native_revisions"] = unchanged["changed"] == 0 and \
                indexed_before == indexed_after and bool(indexed_after)
            checks["text_projection_records_both_generations"] = adapter.read(library.index / "text.json").get("indexedGeneration") == \
                adapter.read(library.indexed)["generation"]

            with (entry / "assets/selected.mp4").open("ab") as target:
                target.write(b"\0")
            stale = library.search(query="silver compass", mode="keyword", scope="all")
            checks["edited_source_marks_hit_stale"] = bool(stale["results"]) and stale["stale"] and \
                stale["results"][0]["matches"]["audio"]["stale"]
            checks["edited_source_revokes_public_text"] = "compass" not in library.extracted_text(scope="public").get("demo", "").lower()
            manifest["publish"]["files"] = []
            adapter.save(entry / "entry.json", manifest)
            checks["manifest_revocation_removes_public_search"] = not library.search(
                query="silver compass", mode="keyword", scope="public")["results"]
            checks["manifest_revocation_removes_public_text"] = library.extracted_text(scope="public") == {}
            print(json.dumps(dict(elapsed_seconds=elapsed, checks=checks, counts_media=receipt.get("media"),
                                  private=private, public=public, retained=str(root) if args.keep else None), indent=2))
            if not all(checks.values()):
                raise SystemExit(1)
        finally:
            library.close()


if __name__ == "__main__":
    main()
