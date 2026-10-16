import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// Timed text from complete audio streams and text subtitles. No models are fetched by this reader.
enum SpeechExtractor {
    struct Passage: Sendable, Equatable {
        let start: Double
        let end: Double
        let text: String
    }

    struct Result: Sendable {
        var passages: [Passage] = []
        var info: [String: String] = [:]
        var degraded: Bool {
            ["transcript_status", "captions_status"].contains {
                ["unavailable", "failed", "partial"].contains(info[$0] ?? "")
            }
        }
    }

    struct Configuration: Sendable {
        let ffmpeg: String?
        let ffprobe: String?
        let whisper: String?
        let model: URL?
        let identity: String

        init(environment: [String: String] = ProcessInfo.processInfo.environment) {
            ffmpeg = MediaTool.find("ffmpeg", override: environment["DIGUP_FFMPEG_BIN"], environment: environment)
            ffprobe = MediaTool.find("ffprobe", override: environment["DIGUP_FFPROBE_BIN"], environment: environment)
            whisper = MediaTool.find("mlx_whisper", override: environment["DIGUP_WHISPER_BIN"]
                                     ?? environment["MLX_WHISPER_BIN"], environment: environment)
            let chosen = environment["DIGUP_WHISPER_MODEL"] ?? environment["MLX_WHISPER_MODEL"]
                ?? "mlx-community/whisper-large-v3-turbo"
            model = Self.cachedModel(chosen, environment: environment)
            let stamp = model.map { directory in
                ["config.json", "weights.safetensors", "weights.npz"].map { name in
                    let attributes = try? FileManager.default.attributesOfItem(atPath: directory.appendingPathComponent(name).path)
                    return "\(name):\(attributes?[.size] ?? 0):\(attributes?[.modificationDate] ?? "missing")"
                }.joined(separator: ";")
            } ?? "uncached"
            let tools = [ffmpeg, ffprobe, whisper].map { path in
                guard let path else { return "missing" }
                let attributes = try? FileManager.default.attributesOfItem(atPath: path)
                return "\(path):\(attributes?[.modificationDate] ?? "missing")"
            }.joined(separator: "|")
            identity = "speech1|\(tools)|\(chosen)|\(model?.path ?? "uncached")|\(stamp)"
        }

        static func cachedModel(_ chosen: String, environment: [String: String]) -> URL? {
            let manager = FileManager.default
            func complete(_ folder: URL) -> Bool {
                manager.fileExists(atPath: folder.appendingPathComponent("config.json").path)
                    && ["weights.safetensors", "weights.npz"].contains {
                        manager.fileExists(atPath: folder.appendingPathComponent($0).path)
                    }
            }
            let direct = URL(fileURLWithPath: (chosen as NSString).expandingTildeInPath)
            if complete(direct) { return direct }
            guard !chosen.hasPrefix("/"), !chosen.hasPrefix("."), chosen.split(separator: "/").count == 2 else { return nil }
            let home = environment["HF_HOME"].map { URL(fileURLWithPath: $0) }
                ?? manager.homeDirectoryForCurrentUser.appendingPathComponent(".cache/huggingface")
            let hub = environment["HF_HUB_CACHE"].map { URL(fileURLWithPath: $0) }
                ?? home.appendingPathComponent("hub")
            let repo = hub.appendingPathComponent("models--" + chosen.replacingOccurrences(of: "/", with: "--"))
            if let revision = try? String(contentsOf: repo.appendingPathComponent("refs/main"), encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines),
               !revision.isEmpty, !revision.contains("/"), !revision.contains("..") {
                let folder = repo.appendingPathComponent("snapshots/" + revision)
                if complete(folder) { return folder }
            }
            let snapshots = (try? manager.contentsOfDirectory(at: repo.appendingPathComponent("snapshots"),
                                                               includingPropertiesForKeys: nil)) ?? []
            return snapshots.sorted { $0.path < $1.path }.first(where: complete)
        }
    }

    static let maxDuration = 1800.0
    static let maxTracks = 4
    static let maxTextBytes = 8 << 20

    static func isCaption(_ url: URL) -> Bool { ["srt", "vtt"].contains(url.pathExtension.lowercased()) }

    static func extract(_ url: URL, to folder: URL, configuration: Configuration = Configuration()) -> Result {
        var result = Result(info: ["media_reader": configuration.identity, "transcript_status": "no_audio",
                                  "transcript_segments": "0", "transcript_streams": "0",
                                  "captions_status": "none", "caption_segments": "0"])
        if isCaption(url) {
            do {
                result.passages = try captions(readBounded(url))
                result.info["captions_status"] = "extracted"
                result.info["caption_segments"] = String(result.passages.count)
            } catch {
                result.info["captions_status"] = "failed"
                result.info["caption_error"] = category(error)
            }
            return result
        }
        guard let probe = configuration.ffprobe else {
            result.info["transcript_status"] = "unavailable"
            result.info["transcript_error"] = "ffprobe_missing"
            result.info["captions_status"] = "unavailable"
            result.info["caption_error"] = "ffprobe_missing"
            return result
        }
        do {
            let data = try MediaTool.run(probe, ["-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
                "stream=index,codec_type,codec_name,start_time,duration:format=start_time,duration", "-of", "json", url.path],
                to: folder, timeout: 15)
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let streams = json["streams"] as? [[String: Any]] else { throw Failure("probe_invalid") }
            let format = json["format"] as? [String: Any] ?? [:]
            let origin = number(format["start_time"]) ?? 0
            let audio = streams.filter { $0["codec_type"] as? String == "audio" }
            let subtitles = streams.filter { $0["codec_type"] as? String == "subtitle" }
            result.info["transcript_streams"] = String(audio.count)
            var audioFailed = 0, audioUnavailable = 0, audioLimited = audio.count > maxTracks
            var spoken: [Passage] = []
            for stream in audio.prefix(maxTracks) {
                guard let index = stream["index"] as? Int else { audioFailed += 1; continue }
                guard let ffmpeg = configuration.ffmpeg, let whisper = configuration.whisper,
                      let model = configuration.model else {
                    audioUnavailable += 1
                    result.info["transcript_error"] = configuration.ffmpeg == nil ? "ffmpeg_missing"
                        : configuration.whisper == nil ? "whisper_missing" : "model_not_cached"
                    continue
                }
                let offset = max(0, (number(stream["start_time"]) ?? origin) - origin)
                let duration = number(stream["duration"]) ?? number(format["duration"]) ?? maxDuration + 1
                audioLimited = audioLimited || duration > maxDuration
                let wav = folder.appendingPathComponent("speech-\(index).wav")
                defer { try? FileManager.default.removeItem(at: wav) }
                do {
                    _ = try MediaTool.run(ffmpeg, ["-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file,pipe", "-i", url.path,
                        "-map", "0:\(index)", "-vn", "-t", String(maxDuration), "-ac", "1", "-ar", "16000",
                        "-af", "loudnorm=I=-20:TP=-2:LRA=7", "-c:a", "pcm_s16le", wav.path], to: folder, timeout: 60)
                    let activity = try audioActivity(wav)
                    if !activity.contains(true) { continue }
                    _ = try MediaTool.run(whisper, [wav.path, "--model", model.path, "--output-format", "json",
                        "--output-dir", folder.path, "--output-name", "transcript-\(index)", "--verbose", "False",
                        "--condition-on-previous-text", "False", "--word-timestamps", "True"], to: folder,
                        timeout: min(600, max(60, duration * 2 + 30)), offline: true,
                        extraEnvironment: ["PATH": URL(fileURLWithPath: ffmpeg).deletingLastPathComponent().path
                            + ":" + (ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin")])
                    let output = folder.appendingPathComponent("transcript-\(index).json")
                    defer { try? FileManager.default.removeItem(at: output) }
                    spoken += try transcript(readBounded(output), offset: offset, duration: min(duration, maxDuration), activity: activity)
                } catch {
                    audioFailed += 1
                    result.info["transcript_error"] = category(error)
                }
            }
            result.info["transcript_segments"] = String(spoken.count)
            if audioLimited { result.info["transcript_error"] = audio.count > maxTracks ? "track_limit" : "duration_limit" }
            result.info["transcript_status"] = audio.isEmpty ? "no_audio"
                : audioLimited || ((audioFailed + audioUnavailable > 0) && !spoken.isEmpty) ? "partial"
                : audioFailed > 0 ? "failed" : audioUnavailable > 0 ? "unavailable"
                : spoken.isEmpty ? "no_speech" : "transcribed"
            var captioned: [Passage] = [], captionFailures = 0
            var captionLimited = subtitles.count > maxTracks
            for stream in subtitles.prefix(maxTracks) {
                guard let index = stream["index"] as? Int, let ffmpeg = configuration.ffmpeg else {
                    captionFailures += 1; result.info["caption_error"] = "ffmpeg_missing"; continue
                }
                guard ["subrip", "webvtt", "mov_text", "ass", "ssa", "text"].contains(stream["codec_name"] as? String ?? "") else {
                    captionFailures += 1; result.info["caption_error"] = "subtitle_codec_unsupported"; continue
                }
                let output = folder.appendingPathComponent("captions-\(index).srt")
                defer { try? FileManager.default.removeItem(at: output) }
                do {
                    _ = try MediaTool.run(ffmpeg, ["-v", "error", "-nostdin", "-y", "-copyts", "-start_at_zero",
                        "-protocol_whitelist", "file,pipe",
                        "-i", url.path, "-map", "0:\(index)", "-t", String(maxDuration), "-c:s", "srt", output.path],
                        to: folder, timeout: 30)
                    captioned += try captions(readBounded(output))
                    captionLimited = captionLimited || (number(format["duration"]) ?? 0) > maxDuration
                } catch { captionFailures += 1; result.info["caption_error"] = category(error) }
            }
            if captionLimited { result.info["caption_error"] = subtitles.count > maxTracks ? "track_limit" : "duration_limit" }
            result.info["captions_status"] = subtitles.isEmpty ? "none"
                : captionLimited || (captionFailures > 0 && !captioned.isEmpty) ? "partial"
                : captionFailures > 0 ? "failed" : "extracted"
            result.info["caption_segments"] = String(captioned.count)
            result.passages = (spoken + captioned).sorted { $0.start < $1.start }
        } catch {
            result.info["transcript_status"] = "failed"
            result.info["transcript_error"] = category(error)
            result.info["captions_status"] = "failed"
            result.info["caption_error"] = category(error)
        }
        return result
    }

    static func transcript(_ data: Data, offset: Double = 0, duration: Double, activity: [Bool]? = nil) throws -> [Passage] {
        guard data.count <= maxTextBytes,
              let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let segments = json["segments"] as? [[String: Any]], segments.count <= 20_000 else {
            throw Failure("transcript_invalid")
        }
        var result: [Passage] = []
        for segment in segments {
            guard let start = number(segment["start"]), let end = number(segment["end"]),
                  start >= 0, end > start, start < duration, let raw = segment["text"] as? String else {
                throw Failure("transcript_invalid")
            }
            // Silence hallucinations are not evidence of speech.
            if (number(segment["no_speech_prob"]) ?? 0) > 0.6 { continue }
            var first = start, last = min(end, duration)
            if let activity {
                let lower = max(0, Int(start * 10)), upper = min(activity.count, Int(ceil(last * 10)))
                guard lower < upper else { continue }
                let bounds = lower..<upper
                let voiced = bounds.filter { activity[$0] }
                guard let begin = voiced.first, let finish = voiced.last else { continue }
                first = max(first, Double(begin) / 10)
                last = min(last, Double(finish + 1) / 10)
            }
            let text = clean(raw)
            if !text.isEmpty { result.append(Passage(start: offset + first, end: offset + last, text: text)) }
        }
        return result
    }

    static func captions(_ data: Data) throws -> [Passage] {
        guard data.count <= maxTextBytes, let source = String(data: data, encoding: .utf8) else { throw Failure("captions_invalid") }
        let lines = source.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n")
        var result: [Passage] = [], cursor = 0
        while cursor < lines.count {
            let line = lines[cursor]; cursor += 1
            if line == "NOTE" || line.hasPrefix("NOTE ") || line == "STYLE" || line == "REGION" {
                while cursor < lines.count, !lines[cursor].trimmingCharacters(in: .whitespaces).isEmpty { cursor += 1 }
                continue
            }
            guard line.contains("-->") else { continue }
            let times = line.components(separatedBy: "-->")
            guard times.count == 2, let start = timestamp(times[0]), let end = timestamp(times[1]), end > start else {
                throw Failure("captions_invalid")
            }
            var body: [String] = []
            while cursor < lines.count, !lines[cursor].trimmingCharacters(in: .whitespaces).isEmpty {
                body.append(lines[cursor]); cursor += 1
            }
            let text = clean(body.joined(separator: " ").replacingOccurrences(of: "<[^>]*>", with: "", options: .regularExpression))
            if !text.isEmpty { result.append(Passage(start: start, end: end, text: text)) }
            if result.count > 20_000 { throw Failure("output_limit") }
        }
        guard !result.isEmpty || source.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || source.trimmingCharacters(in: .whitespacesAndNewlines).hasPrefix("WEBVTT") else { throw Failure("captions_invalid") }
        return result
    }

    static func timestamp(_ source: String) -> Double? {
        guard let first = source.split(whereSeparator: \.isWhitespace).first else { return nil }
        let parts = first.replacingOccurrences(of: ",", with: ".").split(separator: ":")
        guard (2...3).contains(parts.count), let seconds = Double(parts.last!), seconds.isFinite, seconds >= 0, seconds < 60,
              let minutes = Double(parts[parts.count - 2]), minutes >= 0, minutes < 60 else { return nil }
        let hours = parts.count == 3 ? Double(parts[0]) : 0
        guard let hours, hours.isFinite, hours >= 0 else { return nil }
        return hours * 3600 + minutes * 60 + seconds
    }

    private static func number(_ value: Any?) -> Double? {
        let value = (value as? NSNumber)?.doubleValue ?? (value as? String).flatMap(Double.init)
        return value.flatMap { $0.isFinite ? $0 : nil }
    }

    /// Only rejects digital silence, well below the semantic window gate. Quiet speech is still sent to ASR.
    static func audioActivity(_ url: URL) throws -> [Bool] {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        guard try handle.read(upToCount: 12)?.prefix(4) == Data("RIFF".utf8) else { throw Failure("audio_invalid") }
        while let chunk = try handle.read(upToCount: 8), chunk.count == 8 {
            let size = chunk.dropFirst(4).enumerated().reduce(UInt32(0)) { $0 | UInt32($1.element) << ($1.offset * 8) }
            if chunk.prefix(4) != Data("data".utf8) {
                try handle.seek(toOffset: handle.offset() + UInt64(size + (size % 2)))
                continue
            }
            guard size <= UInt32(maxDuration * 32000 + 32000) else { throw Failure("duration_limit") }
            var activity: [Bool] = [], left = Int(size)
            while left > 0 {
                let bytes = try handle.read(upToCount: min(3200, left)) ?? Data()
                guard !bytes.isEmpty, bytes.count % 2 == 0 else { throw Failure("audio_invalid") }
                let values = Array(bytes)
                var sum = 0.0
                for index in stride(from: 0, to: values.count, by: 2) {
                    let sample = Int16(bitPattern: UInt16(values[index]) | UInt16(values[index + 1]) << 8)
                    sum += Double(sample) * Double(sample)
                }
                activity.append((sum / Double(bytes.count / 2)).squareRoot() > 0.5)
                left -= bytes.count
            }
            return activity
        }
        throw Failure("audio_invalid")
    }

    private static func clean(_ value: String) -> String {
        value.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    private static func readBounded(_ url: URL) throws -> Data {
        guard let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= maxTextBytes else {
            throw Failure("output_limit")
        }
        return try Data(contentsOf: url)
    }

    struct Failure: Error { let code: String; init(_ code: String) { self.code = code } }
    private static func category(_ error: Error) -> String { (error as? Failure)?.code ?? "media_read_failed" }
}

/// Files, rather than pipes, drain child output; deadlines and byte caps bound untrusted readers without pipe deadlocks.
public enum MediaTool {
    private final class State: @unchecked Sendable {
        let lock = NSLock()
        var supervisor: String?
        var groups = Set<Int32>()
    }
    private static let state = State()

    public static func configureSupervisor(_ executable: String) { state.lock.withLock { state.supervisor = executable } }

    /// Runs inside an owned process group. EOF on the parent's lease also handles SIGKILL and `_exit`.
    public static func supervise(_ arguments: [String]) -> Int32 {
        #if canImport(Darwin)
        guard let executable = arguments.first, getpgrp() == getpid() else { return 2 }
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0)
        let argv = arguments.map { strdup($0) } + [nil]
        let envp = ProcessInfo.processInfo.environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { for pointer in argv + envp { free(pointer) } }
        var pid: pid_t = 0
        let launched = argv.withUnsafeBufferPointer { args in
            envp.withUnsafeBufferPointer { env in
                posix_spawn(&pid, executable, &actions, nil, UnsafeMutablePointer(mutating: args.baseAddress!),
                            UnsafeMutablePointer(mutating: env.baseAddress!))
            }
        }
        guard launched == 0 else { return 127 }
        let group = getpgrp()
        Thread.detachNewThread {
            _ = FileHandle.standardInput.readDataToEndOfFile()
            kill(-group, SIGKILL)
        }
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
        return status & 0x7f == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f)
        #else
        return 2
        #endif
    }

    /// Child RSS supplements the watchdog's parent physical footprint; only owned process groups are measured.
    public static func childMemoryBytes() -> UInt64 {
        #if canImport(Darwin)
        let groups = state.lock.withLock { state.groups }
        var total: UInt64 = 0
        for group in groups {
            let process = Process(), pipe = Pipe()
            process.executableURL = URL(fileURLWithPath: "/bin/ps")
            process.arguments = ["-o", "rss=", "-g", String(group)]
            process.standardOutput = pipe; process.standardError = FileHandle.nullDevice
            if (try? process.run()) == nil { continue }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            total += String(decoding: data, as: UTF8.self).split(whereSeparator: \.isWhitespace)
                .compactMap { UInt64($0) }.reduce(0, +) * 1024
        }
        return total
        #else
        return 0
        #endif
    }
    static func find(_ name: String, override: String?, environment: [String: String]) -> String? {
        let manager = FileManager.default
        if let override { return manager.isExecutableFile(atPath: override) ? override : nil }
        let folders = (environment["PATH"] ?? "").split(separator: ":").map(String.init)
            + [manager.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin").path,
               "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
        return folders.map { $0 + "/" + name }.first { manager.isExecutableFile(atPath: $0) }
    }

    static func run(_ executable: String, _ arguments: [String], to folder: URL, timeout: Double,
                    offline: Bool = false, outputLimit: Int = 4 << 20, extraEnvironment: [String: String] = [:]) throws -> Data {
        let manager = FileManager.default
        let stdout = folder.appendingPathComponent(UUID().uuidString + ".out")
        let stderr = folder.appendingPathComponent(UUID().uuidString + ".err")
        manager.createFile(atPath: stdout.path, contents: nil)
        manager.createFile(atPath: stderr.path, contents: nil)
        defer { try? manager.removeItem(at: stdout); try? manager.removeItem(at: stderr) }
        let out = try FileHandle(forWritingTo: stdout), err = try FileHandle(forWritingTo: stderr)
        defer { try? out.close(); try? err.close() }
        var environment = ProcessInfo.processInfo.environment
        environment.merge(extraEnvironment) { _, replacement in replacement }
        if offline {
            environment["HF_HUB_OFFLINE"] = "1"; environment["TRANSFORMERS_OFFLINE"] = "1"
            environment["HF_HUB_DISABLE_TELEMETRY"] = "1"
        }
        #if canImport(Darwin)
        // A separate process group lets cancellation stop Whisper's decoder children too.
        var actions: posix_spawn_file_actions_t?
        var attributes: posix_spawnattr_t?
        posix_spawn_file_actions_init(&actions)
        posix_spawnattr_init(&attributes)
        defer { posix_spawn_file_actions_destroy(&actions); posix_spawnattr_destroy(&attributes) }
        posix_spawn_file_actions_adddup2(&actions, out.fileDescriptor, STDOUT_FILENO)
        posix_spawn_file_actions_adddup2(&actions, err.fileDescriptor, STDERR_FILENO)
        let supervisor = state.lock.withLock { state.supervisor }
        let lease = Pipe()
        if supervisor != nil {
            posix_spawn_file_actions_adddup2(&actions, lease.fileHandleForReading.fileDescriptor, STDIN_FILENO)
            posix_spawn_file_actions_addclose(&actions, lease.fileHandleForWriting.fileDescriptor)
        } else {
            posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0)
        }
        defer { try? lease.fileHandleForReading.close(); try? lease.fileHandleForWriting.close() }
        posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP))
        posix_spawnattr_setpgroup(&attributes, 0)
        let command = supervisor.map { [$0, "media-tool", executable] + arguments } ?? ([executable] + arguments)
        let argv = command.map { strdup($0) } + [nil]
        let envp = environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { for pointer in argv + envp { free(pointer) } }
        var pid: pid_t = 0
        let launched = argv.withUnsafeBufferPointer { args in
            envp.withUnsafeBufferPointer { env in
                posix_spawn(&pid, command[0], &actions, &attributes,
                            UnsafeMutablePointer(mutating: args.baseAddress!), UnsafeMutablePointer(mutating: env.baseAddress!))
            }
        }
        guard launched == 0 else { throw SpeechExtractor.Failure("tool_start_failed") }
        state.lock.withLock { _ = state.groups.insert(pid) }
        defer { kill(-pid, SIGKILL); state.lock.withLock { _ = state.groups.remove(pid) } }
        var status: Int32 = 0, running = true
        func isRunning() -> Bool {
            if running, waitpid(pid, &status, WNOHANG) == pid { running = false }
            return running
        }
        #else
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardOutput = out; process.standardError = err; process.standardInput = FileHandle.nullDevice
        process.environment = environment
        do { try process.run() } catch { throw SpeechExtractor.Failure("tool_start_failed") }
        func isRunning() -> Bool { process.isRunning }
        #endif
        let deadline = Date().addingTimeInterval(timeout)
        var failure: String?
        while isRunning() {
            if Date() >= deadline { failure = "tool_timeout" }
            for url in [stdout, stderr] {
                let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
                if size > outputLimit { failure = "output_limit" }
            }
            if failure != nil {
                #if canImport(Darwin)
                kill(-pid, SIGTERM)
                let grace = Date().addingTimeInterval(0.5)
                while isRunning(), Date() < grace { Thread.sleep(forTimeInterval: 0.01) }
                kill(-pid, SIGKILL)
                #else
                process.terminate()
                let grace = Date().addingTimeInterval(0.5)
                while process.isRunning, Date() < grace { Thread.sleep(forTimeInterval: 0.01) }
                #if !os(Windows)
                if process.isRunning { kill(process.processIdentifier, SIGKILL) }
                #endif
                #endif
                break
            }
            Thread.sleep(forTimeInterval: 0.02)
        }
        #if canImport(Darwin)
        if running { while waitpid(pid, &status, 0) < 0 && errno == EINTR {} }
        let exitCode = status & 0x7f == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f)
        #else
        process.waitUntilExit()
        let exitCode = process.terminationStatus
        #endif
        if let failure { throw SpeechExtractor.Failure(failure) }
        guard exitCode == 0 else { throw SpeechExtractor.Failure("tool_exit_\(exitCode)") }
        for url in [stdout, stderr] {
            guard (try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0) <= outputLimit else {
                throw SpeechExtractor.Failure("output_limit")
            }
        }
        let data = try Data(contentsOf: stdout)
        return data
    }
}
