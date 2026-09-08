import AppKit
import SwiftUI
import SwiftTerm
import Darwin

struct RuntimeStatus: Decodable {
    var configured = false
    var provider = ""
    var model = ""
    var claude_available = false
    var hermes_available = false
    var telegram_configured = false
    var workspace = ""
}

struct SavedTurn: Decodable, Identifiable {
    var id: Int
    var ts: Double
    var asked: String
    var answered: String
    var date: Date { Date(timeIntervalSince1970: ts) }
}

struct Archive: Decodable {
    var turns: [SavedTurn] = []
    var total = 0
    var available = true
}

// Terminal escape sequences are untrusted output, not clipboard or launch authority.
final class GuardedTerminal: LocalProcessTerminalView {
    override func requestOpenLink(source: TerminalView, link: String, params: [String: String]) {
        guard let url = URL(string: link), ["https", "http"].contains(url.scheme?.lowercased() ?? ""),
              url.user == nil, url.password == nil else { return }
        NSWorkspace.shared.open(url)
    }
}

// SwiftTerm's local view reads the clipboard by default. Proxy only its PTY plumbing;
// OSC 52 must never turn a model answer into clipboard access.
@MainActor final class TerminalFence: NSObject, @preconcurrency TerminalViewDelegate {
    weak var view: GuardedTerminal?
    init(_ view: GuardedTerminal) { self.view = view }
    func clipboardRead(source: TerminalView) -> Data? { nil }
    func clipboardCopy(source: TerminalView, content: Data) {}
    func bell(source: TerminalView) {}
    func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) {
        view?.sizeChanged(source: source, newCols: newCols, newRows: newRows)
    }
    func setTerminalTitle(source: TerminalView, title: String) {}
    func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}
    func send(source: TerminalView, data: ArraySlice<UInt8>) { view?.send(source: source, data: data) }
    func scrolled(source: TerminalView, position: Double) { view?.scrolled(source: source, position: position) }
    func rangeChanged(source: TerminalView, startY: Int, endY: Int) {}
    func requestOpenLink(source: TerminalView, link: String, params: [String: String]) {
        view?.requestOpenLink(source: source, link: link, params: params)
    }
}

@MainActor final class Session: NSObject, ObservableObject, @preconcurrency LocalProcessTerminalViewDelegate {
    @Published var status = RuntimeStatus()
    @Published var archive = Archive()
    @Published var busy = false
    @Published var loaded = false
    @Published var error = ""
    @Published var activity = "Ready when you are"
    @Published var hasTerminal = false
    @Published var currentAction = ""
    @Published var page: Page = .home
    let terminal = GuardedTerminal(frame: .zero)
    private var fence: TerminalFence?
    private var exitHandled = false

    enum Page: String, CaseIterable { case home = "Dashboard", chat = "Chat", history = "History", connections = "Connections", computer = "Computer" }

    override init() {
        super.init()
        terminal.processDelegate = self
        fence = TerminalFence(terminal)
        terminal.terminalDelegate = fence
        terminal.font = .monospacedSystemFont(ofSize: 14, weight: .regular)
        terminal.nativeBackgroundColor = NSColor(srgbRed: 0.055, green: 0.062, blue: 0.066, alpha: 1)
        terminal.nativeForegroundColor = NSColor(srgbRed: 0.89, green: 0.88, blue: 0.84, alpha: 1)
        terminal.setAccessibilityLabel("Talos conversation and setup")
    }

    var resources: URL { Bundle.main.resourceURL! }
    var python: URL { resources.appendingPathComponent("python/bin/python3") }
    var runtime: URL { resources.appendingPathComponent("desktop_runtime.py") }

    func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        // The signed app is immutable; interpreter caches belong outside it.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        return env
    }

    func refresh() {
        read("status") { data in
            self.loaded = true
            if let result = try? JSONDecoder().decode(RuntimeStatus.self, from: data) {
                self.status = result
                self.error = ""
            }
        }
        read("history") { data in
            if let result = try? JSONDecoder().decode(Archive.self, from: data) { self.archive = result }
        }
    }

    private func read(_ action: String, done: @escaping (Data) -> Void) {
        let executable = python, script = runtime, env = environment()
        // Drain stdout while the child runs. An archive can exceed pipe capacity;
        // waiting for exit before reading would deadlock the whole history view.
        Task {
            let result = await Task.detached { () -> (Data, Int32) in
                let process = Process(), pipe = Pipe()
                process.executableURL = executable
                process.arguments = ["-B", script.path, action]
                process.environment = env
                process.standardOutput = pipe
                process.standardError = pipe
                do {
                    try process.run()
                    let data = pipe.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    return (data, process.terminationStatus)
                } catch { return (Data(error.localizedDescription.utf8), 1) }
            }.value
            if result.1 == 0 { done(result.0) }
            else {
                self.loaded = true
                self.error = String(data: result.0, encoding: .utf8) ?? "The local runtime could not start."
            }
        }
    }

    func start(_ action: String = "chat") {
        guard !busy else { page = .chat; return }
        guard !ProcessInfo.processInfo.environment.keys.contains("TALOS_SANDBOX") else {
            error = "The desktop app cannot start from an agent sandbox."
            return
        }
        error = ""
        exitHandled = false
        busy = true
        currentAction = action
        hasTerminal = true
        page = .chat
        activity = ["quick-claude": "Connecting Claude", "chat": "Your local agent", "model": "Model setup",
                    "telegram": "Telegram setup", "telegram-run": "Telegram is running", "codex": "Connecting Codex", "ollama": "Connecting Ollama", "doctor": "Checking your setup"][action] ?? "Talos"
        if action == "quick-claude" { activity = "Claude · setup and chat" }
        terminal.feed(text: "\u{1b}[2J\u{1b}[H")
        terminal.startProcess(executable: python.path, args: ["-B", runtime.path, action],
                              environment: environment().map { "\($0.key)=\($0.value)" },
                              currentDirectory: resources.path)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
            self.terminal.window?.makeFirstResponder(self.terminal)
        }
    }

    func stop() {
        guard busy else { return }
        let pid = terminal.process.shellPid
        activity = "Stopping session…"
        terminal.send(source: terminal, data: Array("\u{3}".utf8)[...])
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            if self.busy && self.terminal.process.shellPid == pid { self.terminateSession() }
        }
    }

    func terminateSession() {
        // forkpty creates a separate session/group. Signal only that group's
        // live leader, never this app's group or another agent's process.
        let pid = terminal.process.shellPid
        if terminal.process.running && pid > 1 && getpgid(pid) == pid {
            kill(-pid, SIGTERM)
        }
        if terminal.process.running { terminal.terminate() }
    }

    func submit(_ text: String) {
        guard busy && currentAction == "chat" else { return }
        // A single operator message, never terminal escape sequences or a pasted
        // series of implicit commands. Approvals still arrive through the CLI.
        let line = text.unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) }
        let message = String(String.UnicodeScalarView(line)).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty else { return }
        terminal.send(source: terminal, data: Array((String(message.prefix(20000)) + "\r").utf8)[...])
    }

    func processTerminated(source: TerminalView, exitCode: Int32?) {
        guard !exitHandled else { return }
        exitHandled = true
        busy = false
        activity = exitCode == 0 ? "Session finished" : "Session stopped"
        refresh()
    }
    func sizeChanged(source: LocalProcessTerminalView, newCols: Int, newRows: Int) {}
    func setTerminalTitle(source: LocalProcessTerminalView, title: String) {}
    func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}
}

struct TerminalPane: NSViewRepresentable {
    @ObservedObject var session: Session
    func makeNSView(context: Context) -> GuardedTerminal { session.terminal }
    func updateNSView(_ view: GuardedTerminal, context: Context) {}
}
