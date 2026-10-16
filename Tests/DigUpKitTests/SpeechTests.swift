import Foundation
import Testing
@testable import DigUpKit

@Suite final class SpeechTests {
    let folder = FileManager.default.temporaryDirectory.appendingPathComponent("DigUpSpeechTests-\(UUID().uuidString)")

    init() throws { try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true) }
    deinit { try? FileManager.default.removeItem(at: folder) }

    @Test func transcriptKeepsTimesAndOffsetAndRejectsSilenceHallucinations() throws {
        let data = Data("""
            {"segments":[{"start":0.1,"end":1.7,"text":" Quiet orange lantern. ","no_speech_prob":0.01},
                         {"start":2,"end":3,"text":"Thank you for watching","no_speech_prob":0.9}]}
            """.utf8)
        #expect(try SpeechExtractor.transcript(data, offset: 12, duration: 4) == [
            SpeechExtractor.Passage(start: 12.1, end: 13.7, text: "Quiet orange lantern.")])
        #expect(try SpeechExtractor.transcript(data, duration: 4, activity: Array(repeating: false, count: 40)).isEmpty)
        var quiet = [Int16](repeating: 0, count: 16000)
        for index in 8000..<12000 { quiet[index] = index % 2 == 0 ? 2 : -2 }
        let wav = folder.appendingPathComponent("quiet.wav")
        try AudioExtractor.writeWAV(quiet[...], to: wav)
        let activity = try SpeechExtractor.audioActivity(wav)
        #expect(activity.prefix(5).allSatisfy { !$0 })
        #expect(activity[5])
    }

    @Test(arguments: ["{}", "{\"segments\":[{\"start\":2,\"end\":1,\"text\":\"bad\"}]}",
                      "{\"segments\":[{\"start\":-1,\"end\":1,\"text\":\"bad\"}]}"])
    func malformedTranscriptIsNotSuccessfulEmpty(_ source: String) {
        #expect(throws: (any Error).self) { try SpeechExtractor.transcript(Data(source.utf8), duration: 4) }
    }

    @Test func captionsKeepCueTimesAndRemoveMarkup() throws {
        let data = Data("""
            WEBVTT

            cue-one
            00:12.200 --> 00:14.600 align:start
            <v Speaker><b>Silver compass</b></v>
            points north.

            2
            01:02:03,004 --> 01:02:05,006
            Another sentence.

            """.utf8)
        #expect(try SpeechExtractor.captions(data) == [
            .init(start: 12.2, end: 14.6, text: "Silver compass points north."),
            .init(start: 3723.004, end: 3725.006, text: "Another sentence.")])
        #expect(throws: (any Error).self) { try SpeechExtractor.captions(Data("00:99.000 --> 00:01.000\nbad".utf8)) }
        #expect(try SpeechExtractor.captions(Data("WEBVTT neutral\n\nNOTE not a cue --> comment\nignored\n".utf8)).isEmpty)
    }

    @Test func cachedModelRequiresWeightsAndConfiguration() throws {
        let model = folder.appendingPathComponent("local-model")
        try FileManager.default.createDirectory(at: model, withIntermediateDirectories: true)
        try Data("{}".utf8).write(to: model.appendingPathComponent("config.json"))
        #expect(SpeechExtractor.Configuration.cachedModel(model.path, environment: [:]) == nil)
        try Data([1]).write(to: model.appendingPathComponent("weights.safetensors"))
        #expect(SpeechExtractor.Configuration.cachedModel(model.path, environment: [:])?.path == model.path)
        #expect(SpeechExtractor.Configuration.cachedModel("missing/uncached", environment: ["HF_HOME": folder.path]) == nil)
    }

    @Test func explicitCaptionIsSearchableAndDoesNotReadAdjacentFiles() throws {
        let source = folder.appendingPathComponent("source.vtt")
        try Data("WEBVTT\n\n00:03.000 --> 00:05.000\nSilver compass points north.\n".utf8).write(to: source)
        try Data("private adjacent words".utf8).write(to: folder.appendingPathComponent("source.txt"))
        let store = try IndexStore(directory: folder.appendingPathComponent("index"))
        let id = try store.insert(Candidate(path: source.path, kind: .doc, size: 1, mtime: 1, inode: 1, device: 1))
        let indexer = try Indexer(store: store, options: IndexOptions(), makeEmbedder: { FakeEmbedder() })
        #expect(try indexer.run()["done"] == 1)
        var passages: [(Double, Double, String)] = []
        try store.db.query("""
            SELECT loc, loc_end, coalesce(fts.original, fts.body) FROM segments JOIN fts ON segments.id = fts.rowid
            WHERE file = ? AND kind = 'audio' AND modality = 'text'
            """, [.int(id)]) { passages.append(($0.double(0), $0.double(1), $0.text(2) ?? "")) }
        #expect(passages.count == 1)
        #expect(passages.first?.0 == 3)
        #expect(passages.first?.1 == 5)
        #expect((passages.first?.2)?.lowercased() == "silver compass points north.")
        #expect(try store.info(of: id)["captions_status"] == "extracted")
        #expect(try store.info(of: id)["transcript_status"] == "no_audio")
    }

    @Test func degradedMediaIsNotScheduledAsAnUnfinishedDocument() throws {
        let store = try IndexStore(directory: folder.appendingPathComponent("index"))
        let caption = try store.insert(Candidate(path: folder.appendingPathComponent("bad.srt").path,
                                                 kind: .doc, size: 1, mtime: 1, inode: 1, device: 1))
        try store.finish(caption, state: "partial", info: ["media_reader": "speech1", "captions_status": "failed"])
        let audio = try store.insert(Candidate(path: "/sound.wav", kind: .audio, size: 1, mtime: 1, inode: 2, device: 1))
        try store.finish(audio, state: "partial", info: ["transcript_status": "unavailable"])
        #expect(try store.unfinished().isEmpty)
        #expect(try store.unfinishedCount().files == 0)
        #expect(try store.pendingCount() == 0)
    }

    @Test func unavailableToolsHaveExplicitDegradedStatus() {
        let config = SpeechExtractor.Configuration(environment: ["DIGUP_FFPROBE_BIN": "/missing/ffprobe",
            "DIGUP_FFMPEG_BIN": "/missing/ffmpeg", "DIGUP_WHISPER_BIN": "/missing/whisper"])
        let result = SpeechExtractor.extract(folder.appendingPathComponent("sound.wav"), to: folder, configuration: config)
        #expect(result.degraded)
        #expect(result.passages.isEmpty)
        #expect(result.info["transcript_status"] == "unavailable")
        #expect(result.info["transcript_error"] == "ffprobe_missing")
    }

    @Test func readerChangesRequeueMediaOnce() throws {
        let sources = folder.appendingPathComponent("sources")
        try FileManager.default.createDirectory(at: sources, withIntermediateDirectories: true)
        try Data("00:01.000 --> 00:02.000\nSilver compass.\n".utf8).write(to: sources.appendingPathComponent("cue.vtt"))
        let store = try IndexStore(directory: folder.appendingPathComponent("index"))
        let indexer = try Indexer(store: store, options: IndexOptions(), makeEmbedder: { FakeEmbedder() })
        #expect(try indexer.sync([sources]).added == 1)
        try indexer.run()
        let file = try #require(store.files(under: [sources.path]).first)
        var info = try store.info(of: file.id)
        info["media_reader"] = "older-reader"
        try store.finish(file.id, state: "partial", info: info)
        #expect(try indexer.sync([sources]).changed == 1)
        try indexer.run()
        #expect(try indexer.sync([sources]).unchanged == 1)
        #expect(try store.pendingCount() == 0)
    }

    #if !os(Windows)
    @Test func childTimeoutAndOutputLimitAreBounded() throws {
        #expect(throws: (any Error).self) {
            try MediaTool.run("/bin/sleep", ["5"], to: folder, timeout: 0.05)
        }
        #expect(throws: (any Error).self) {
            try MediaTool.run("/usr/bin/yes", [], to: folder, timeout: 2, outputLimit: 1024)
        }
        #expect(try MediaTool.run("/bin/echo", ["ready"], to: folder, timeout: 1) == Data("ready\n".utf8))
    }
    #endif
}
