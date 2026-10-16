#!/usr/bin/env python3
"""Generate neutral speech/media with installed tools and exercise a disposable native index.

Run on macOS: python3 scripts/testbed/check_speech.py --binary /path/to/digup
Requires existing say, ffmpeg, mlx_whisper, cached Whisper and DigUp models. Downloads nothing.
All fixtures and index output live in a TemporaryDirectory and are removed on exit.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import signal
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--keep", action="store_true", help="retain only this task-owned temporary media/index for adapter review")
    args = parser.parse_args()
    ffmpeg = shutil.which("ffmpeg")
    say = shutil.which("say")
    if not ffmpeg or not say:
        parser.error("existing ffmpeg and macOS say are required")
    environment = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    environment.pop("X_API_REFRESH_TOKEN", None)
    environment.pop("X_MATTER_ALLOW_OAUTH_REFRESH", None)

    def run(command, timeout=60, accepted=(0,)):
        result = subprocess.run([str(x) for x in command], capture_output=True, text=True,
                                timeout=timeout, env=environment)
        if result.returncode not in accepted:
            raise RuntimeError(f"{Path(str(command[0])).name} exited {result.returncode}")
        return result

    cache = Path.home() / "Library/Caches" if sys.platform == "darwin" else Path(tempfile.gettempdir())
    temporary_context = contextlib.nullcontext(tempfile.mkdtemp(prefix="digup-speech-matrix-", dir=cache)) if args.keep else \
        tempfile.TemporaryDirectory(prefix="digup-speech-matrix-", dir=cache)
    with temporary_context as temporary:
        root = Path(temporary).resolve()
        media = root / "media"
        media.mkdir()

        def convert(*arguments):
            return run([ffmpeg, "-v", "error", "-nostdin", "-y", *arguments])

        def speech(name, words):
            aiff = root / (name + ".aiff")
            wav = media / (name + ".wav")
            run([say, "-v", "Samantha", "-r", "150", "-o", aiff, words])
            convert("-i", aiff, "-ac", "1", "-ar", "16000", wav)
            return wav

        plain = speech("plain", "The silver compass points north. The orange lantern is beside the window.")
        short = speech("short", "Amber.")
        second = speech("second", "A violet kettle rests beside the garden gate.")
        tail = speech("boundary-words", "The cobalt anchor rests beside the tower.")
        convert("-i", tail, "-af", "adelay=29000:all=1", media / "boundary-tail.wav")
        tail.unlink()
        convert("-i", plain, "-vn", "-c:a", "aac", media / "audio-only.mp4")
        convert("-i", plain, "-af", "volume=0.002", media / "quiet.wav")
        convert("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "3", media / "silence.wav")
        (media / "corrupt.wav").write_bytes(b"RIFF truncated neutral fixture")
        convert("-f", "lavfi", "-i", "color=black:s=320x192:r=1:d=42", "-itsoffset", "33", "-i", plain,
                "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                media / "delayed.mp4")
        convert("-f", "lavfi", "-i", "color=black:s=320x192:r=1:d=10", "-i", plain, "-itsoffset", "1", "-i", second,
                "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", media / "multitrack.mp4")
        captions = media / "captions.srt"
        captions.write_text("1\n00:00:01,200 --> 00:00:03,400\nA copper telescope faces east.\n\n", encoding="utf8")
        convert("-f", "lavfi", "-i", "color=black:s=320x192:r=1:d=5", "-i", captions,
                "-map", "0:v", "-map", "1:s", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:s", "mov_text",
                media / "captioned.mp4")
        second.unlink()  # exercised as the second stream, not indexed twice
        index = root / "index"
        started = time.monotonic()
        indexed = run([args.binary.resolve(), "index", media, "--index", index], timeout=900, accepted=(0, 2))
        elapsed = round(time.monotonic() - started, 2)
        db = sqlite3.connect(index / "index.sqlite")
        records = {}
        for file_id, path, state, detail, info in db.execute("SELECT id,path,state,detail,info FROM files"):
            passages = [dict(start=start, end=end, text=text, vector_bytes=length)
                        for start, end, text, length in db.execute("""
                            SELECT loc,loc_end,coalesce(fts.original,fts.body),length(vec)
                            FROM segments JOIN fts ON segments.id=fts.rowid
                            WHERE file=? AND kind='audio' AND modality='text' ORDER BY loc
                            """, (file_id,))]
            records[Path(path).name] = dict(state=state, detail=detail, info=json.loads(info or "{}"), passages=passages)

        def contains(name, word):
            return any(word in p["text"].lower() for p in records[name]["passages"])

        checks = {
            "standalone_speech": contains("plain.wav", "compass"),
            "short_speech_under_two_seconds": contains("short.wav", "amber") and
                all(p["end"] < 2 for p in records["short.wav"]["passages"]),
            "quiet_speech_below_semantic_gate": contains("quiet.wav", "compass"),
            "audio_only_mp4": contains("audio-only.mp4", "compass"),
            "phrase_crossing_thirty_second_boundary": contains("boundary-tail.wav", "cobalt")
                and contains("boundary-tail.wav", "anchor"),
            "speech_after_thirty_seconds": any(p["start"] >= 32 and "compass" in p["text"].lower()
                                                for p in records["delayed.mp4"]["passages"]),
            "all_audio_tracks": contains("multitrack.mp4", "compass") and contains("multitrack.mp4", "kettle")
                and records["multitrack.mp4"]["info"].get("transcript_streams") == "2",
            "silence_has_no_transcript": not records["silence.wav"]["passages"] and
                records["silence.wav"]["info"].get("transcript_status") == "no_speech",
            "corrupt_is_failed": records["corrupt.wav"]["state"] == "failed",
            "explicit_captions": contains("captions.srt", "telescope"),
            "embedded_captions": contains("captioned.mp4", "telescope"),
            "text_has_vectors": all(p["vector_bytes"] == 1536 for r in records.values() for p in r["passages"]),
        }
        request = "\n".join(json.dumps(item) for item in [
            dict(id=1, op="search", q="silver compass", meaning=False, keywords=True, limit=30),
            dict(id=2, op="search", q="a violet kettle rests beside the garden gate", meaning=True, keywords=False, limit=30),
            dict(id=3, op="search", q="silver compass points north", meaning=True, keywords=False, limit=30)]) + "\n"
        queried = subprocess.run([str(args.binary.resolve()), "search-service", str(media), "--index", str(index)],
                                 input=request, text=True, capture_output=True, timeout=60, env=environment, check=True)
        results = [json.loads(line) for line in queried.stdout.splitlines() if line.startswith("{")]
        search = next((item for item in results if item.get("id") == 1), {})
        checks["spoken_only_keyword_search"] = any(Path(hit.get("path", "")).name == "delayed.mp4"
            and hit.get("segment") == "audio" and hit.get("loc", 0) >= 32
            and hit.get("loc_end", 0) > hit.get("loc", 0) and "compass" in hit.get("excerpt", "").lower()
            for hit in search.get("hits", []))
        meaning = next((item for item in results if item.get("id") == 2), {})
        checks["spoken_only_semantic_search"] = meaning.get("meaning") is True and any(
            Path(hit.get("path", "")).name == "multitrack.mp4" and hit.get("segment") == "audio"
            and "kettle" in hit.get("excerpt", "").lower()
            for hit in meaning.get("hits", []))
        repeated_phrase = next((item for item in results if item.get("id") == 3), {})
        before = dict(db.execute("SELECT path,indexed_at FROM files"))
        run([args.binary.resolve(), "index", media, "--index", index], timeout=45, accepted=(0, 2))
        after = dict(db.execute("SELECT path,indexed_at FROM files"))
        checks["unchanged_media_does_not_repeat_asr"] = before == after

        # A fake slow ASR with a child decoder checks lifetime only; actual ASR above supplies the quality evidence.
        slow = root / "slow-whisper"
        slow.write_text(f"#!{sys.executable}\nimport os,subprocess,sys,time\n"
            "payload=bytearray(320<<20) if os.environ.get('DIGUP_TEST_ALLOCATE') else None\n"
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
            "open(os.environ['DIGUP_TEST_CHILD_PIDS'],'w').write(str(os.getpid())+' '+str(child.pid))\n"
            "child.wait()\n", encoding="utf8")
        slow.chmod(0o700)
        cancel_media = root / "cancel-media"
        cancel_media.mkdir()
        shutil.copy2(short, cancel_media / "neutral.wav")

        def alive(pid):
            status = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
            return bool(status.stdout.strip()) and not status.stdout.strip().startswith("Z")

        for label, ending in [("parent_sigkill", signal.SIGKILL), ("parent_sigterm", signal.SIGTERM)]:
            pids_path = root / (label + ".pids")
            cancel_environment = dict(environment, DIGUP_WHISPER_BIN=str(slow), DIGUP_TEST_CHILD_PIDS=str(pids_path))
            with (root / (label + ".log")).open("w") as log:
                parent = subprocess.Popen([str(args.binary.resolve()), "index", str(cancel_media), "--index", str(root / label)],
                                          stdout=log, stderr=log, env=cancel_environment)
                deadline = time.monotonic() + 15
                while not pids_path.exists() and parent.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not pids_path.exists():
                    parent.kill(); parent.wait(timeout=5)
                    checks[label + "_cleans_children"] = False
                    continue
                child_pids = [int(value) for value in pids_path.read_text().split()]
                os.kill(parent.pid, ending)
                parent.wait(timeout=5)
                deadline = time.monotonic() + 5
                while any(alive(pid) for pid in child_pids) and time.monotonic() < deadline:
                    time.sleep(0.05)
                checks[label + "_cleans_children"] = not any(alive(pid) for pid in child_pids)
                for pid in child_pids:
                    if alive(pid):
                        os.kill(pid, signal.SIGKILL)
        for label, seconds, megabytes, allocate in [("watchdog_time", "1", "4096", False),
                                                    ("watchdog_memory", "15", "256", True)]:
            pids_path = root / (label + ".pids")
            cancel_environment = dict(environment, DIGUP_WHISPER_BIN=str(slow), DIGUP_TEST_CHILD_PIDS=str(pids_path))
            if allocate:
                cancel_environment["DIGUP_TEST_ALLOCATE"] = "1"
            log_path = root / (label + ".log")
            with log_path.open("w") as log:
                parent = subprocess.Popen([str(args.binary.resolve()), "index-service", str(cancel_media),
                    "--index", str(root / label), "--linger", "0", "--time-limit", seconds,
                    "--memory-limit", megabytes], stdin=subprocess.PIPE, stdout=log, stderr=log, env=cancel_environment)
                try:
                    code = parent.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    parent.kill(); code = parent.wait(timeout=5)
                finally:
                    parent.stdin.close()
            child_pids = [int(value) for value in pids_path.read_text().split()] if pids_path.exists() else []
            deadline = time.monotonic() + 5
            while any(alive(pid) for pid in child_pids) and time.monotonic() < deadline:
                time.sleep(0.05)
            expected = "needed more than" if allocate else "took more than"
            checks[label + "_cleans_children"] = code == 75 and bool(child_pids) and \
                expected in log_path.read_text() and not any(alive(pid) for pid in child_pids)
            for pid in child_pids:
                if alive(pid):
                    os.kill(pid, signal.SIGKILL)
        print(json.dumps(dict(elapsed_seconds=elapsed, index_exit=indexed.returncode, checks=checks,
                              media=records, keyword_hit_count=len(search.get("hits", [])),
                              semantic_hit_count=len(meaning.get("hits", [])),
                              repeated_phrase_semantic=dict(encoder_ready=repeated_phrase.get("meaning") is True,
                                  hit_count=len(repeated_phrase.get("hits", []))),
                              retained=str(root) if args.keep else None), indent=2))
        if not all(checks.values()):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
