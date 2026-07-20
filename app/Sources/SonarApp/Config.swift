import Foundation

/// Resolved paths, ports and URLs — read from the SAME `SONAR_*` env the
/// daemons + scripts/sonar.sh use, with matching defaults.
///
/// Immutable value type: `Config.load()` snapshots the environment once at
/// launch and everything downstream reads these fields.
struct Config {
    /// ~/.sonar (SONAR_HOME).
    let home: URL
    /// SONAR_HOME/run — where the daemons drop pidfiles + notes.url.
    let runDir: URL
    /// SONAR_HOME/run/notes.url — the single "raise the Notes window" signal.
    let notesUrlFile: URL
    /// SONAR_NOTES_PORT (default 8771).
    let notesPort: Int
    /// http://127.0.0.1:<notesPort>/ — the Notes UI served by NotesServer.
    let notesURL: URL
    /// SONAR_HARNESS_URL (default http://127.0.0.1:8787).
    let harnessURL: URL
    /// <harnessURL>/health.
    let healthURL: URL
    /// SONAR_VAULT_PATH (default ~/Documents/Obsidian Vault).
    let vaultPath: String
    /// SONAR_OLLAMA_URL (default http://127.0.0.1:11434).
    let ollamaURL: String
    /// Repo root, used to find voice/ for spawning `python -m notes`.
    let repoRoot: URL
    /// repoRoot/voice — cwd for the spawned notes backend.
    let voiceDir: URL
    /// Absolute path to `uv`, resolved for a GUI app's minimal PATH (nil if
    /// none of the well-known locations exist).
    let uvPath: String?

    static func load() -> Config {
        let env = ProcessInfo.processInfo.environment

        func value(_ key: String) -> String? {
            guard let v = env[key], !v.isEmpty else { return nil }
            return v
        }

        let homePath = value("SONAR_HOME") ?? (NSHomeDirectory() + "/.sonar")
        let home = URL(fileURLWithPath: homePath, isDirectory: true)
        let runDir = home.appendingPathComponent("run", isDirectory: true)
        let notesUrlFile = runDir.appendingPathComponent("notes.url", isDirectory: false)

        let notesPort = value("SONAR_NOTES_PORT").flatMap { Int($0) } ?? 8771
        let notesURL = URL(string: "http://127.0.0.1:\(notesPort)/")
            ?? URL(string: "http://127.0.0.1:8771/")!

        let harnessString = value("SONAR_HARNESS_URL") ?? "http://127.0.0.1:8787"
        let harnessURL = URL(string: harnessString)
            ?? URL(string: "http://127.0.0.1:8787")!
        let healthURL = harnessURL.appendingPathComponent("health")

        let vaultPath = value("SONAR_VAULT_PATH")
            ?? (NSHomeDirectory() + "/Documents/Obsidian Vault")
        let ollamaURL = value("SONAR_OLLAMA_URL") ?? "http://127.0.0.1:11434"

        let repoRoot = Config.resolveRepoRoot(env: env)
        let voiceDir = repoRoot.appendingPathComponent("voice", isDirectory: true)
        let uvPath = Config.resolveUV(env: env)

        return Config(
            home: home,
            runDir: runDir,
            notesUrlFile: notesUrlFile,
            notesPort: notesPort,
            notesURL: notesURL,
            harnessURL: harnessURL,
            healthURL: healthURL,
            vaultPath: vaultPath,
            ollamaURL: ollamaURL,
            repoRoot: repoRoot,
            voiceDir: voiceDir,
            uvPath: uvPath
        )
    }

    /// Find the repo root by preferring an explicit override, then walking up
    /// from the bundle/executable looking for a `voice/notes` marker.
    private static func resolveRepoRoot(env: [String: String]) -> URL {
        let fm = FileManager.default
        func hasVoiceNotes(_ dir: URL) -> Bool {
            fm.fileExists(atPath: dir.appendingPathComponent("voice/notes").path)
        }

        if let override = env["SONAR_REPO_ROOT"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }

        // Walk up from the bundle location (…/app/build/Sonar.app -> repo root)
        // and from the executable, whichever resolves the marker first.
        var candidate = Bundle.main.bundleURL.resolvingSymlinksInPath()
        for _ in 0..<8 {
            if hasVoiceNotes(candidate) { return candidate }
            let parent = candidate.deletingLastPathComponent()
            if parent == candidate { break }
            candidate = parent
        }

        var execDir = URL(fileURLWithPath: CommandLine.arguments.first ?? "")
            .resolvingSymlinksInPath()
            .deletingLastPathComponent()
        for _ in 0..<8 {
            if hasVoiceNotes(execDir) { return execDir }
            let parent = execDir.deletingLastPathComponent()
            if parent == execDir { break }
            execDir = parent
        }

        // Fall back to the current working directory.
        return URL(fileURLWithPath: fm.currentDirectoryPath, isDirectory: true)
    }

    /// Resolve an absolute `uv` path. A GUI-launched .app inherits a minimal
    /// PATH (no ~/.local/bin, no Homebrew), so probe the well-known locations.
    private static func resolveUV(env: [String: String]) -> String? {
        let fm = FileManager.default
        var candidates: [String] = []
        if let override = env["SONAR_UV"], !override.isEmpty {
            candidates.append(override)
        }
        let home = NSHomeDirectory()
        candidates.append(contentsOf: [
            home + "/.local/bin/uv",
            "/opt/homebrew/bin/uv",
            "/usr/local/bin/uv",
            "/usr/bin/uv",
        ])
        for path in candidates where fm.isExecutableFile(atPath: path) {
            return path
        }
        return nil
    }
}
