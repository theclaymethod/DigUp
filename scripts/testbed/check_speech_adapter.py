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
        manifest = {"title": "Neutral media", "tier": "gallery", "publish": {"files": ["assets/selected.mp4"]}}
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
