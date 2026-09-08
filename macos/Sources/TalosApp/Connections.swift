import AppKit
import SwiftUI

struct ConnectionsView: View {
    @ObservedObject var session: Session
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                Text("Make it yours.").font(.system(size: 33, weight: .medium, design: .rounded)).tracking(-0.8)
                Text("Start with one model. Add other ways to reach Talos whenever you need them.")
                    .foregroundStyle(Palette.muted).font(.system(size: 14)).lineSpacing(4)
                card("Model connection", icon: "sparkle", detail: session.status.configured
                     ? "\(session.status.provider) · \(session.status.model)"
                     : "Choose an existing login or connect an API account.") {
                    if session.status.claude_available {
                        Button("Use Claude") { session.start("quick-claude") }.buttonStyle(BronzeButtonStyle())
                    }
                    Button(session.status.configured ? "Change connection" : "Choose connection") {
                        session.start(session.status.configured ? "model" : "chat")
                    }.buttonStyle(.bordered)
                }
                card("Codex", icon: "curlybraces", detail: "Use your existing OpenAI OAuth login through an isolated Hermes connection. No API key is copied into Talos.") {
                    Button("Connect Codex") { session.start("codex") }.buttonStyle(.bordered)
                        .disabled(!session.status.hermes_available)
                }
                card("Ollama · on this Mac", icon: "externaldrive", detail: "Choose from the models installed in your local Ollama. No API key or cloud connection needed.") {
                    Button("Connect Ollama") { session.start("ollama") }.buttonStyle(.bordered)
                }
                Text("Antigravity is currently a separate worker, not a selectable main model.")
                    .font(.system(size: 12)).foregroundStyle(Palette.muted)
                card("Telegram", icon: "paperplane", detail: "Use an existing bot that is not running elsewhere, or create a new one. One running agent per bot; a remote agent keeps its current connection.") {
                    Button(session.status.telegram_configured ? "Edit Telegram setup" : "Set up Telegram") {
                        session.start("telegram")
                    }.buttonStyle(.bordered)
                    if session.status.telegram_configured {
                        Button("Start Telegram") { session.start("telegram-run") }.buttonStyle(BronzeButtonStyle())
                    }
                }
                card("Something needs attention?", icon: "stethoscope", detail: "Run the built-in checks for missing tools and configuration. Your settings stay in place.") {
                    Button("Check setup") { session.start("doctor") }.buttonStyle(.bordered)
                }
                if session.busy {
                    Text("Finish the current session before changing its connection.")
                        .font(.system(size: 12)).foregroundStyle(Palette.bronze)
                    Button("Return to session") { session.page = .chat }.buttonStyle(.plain)
                }
            }.padding(45).frame(maxWidth: 840, alignment: .leading)
        }
    }

    private func card<Actions: View>(_ title: String, icon: String, detail: String,
                                     @ViewBuilder actions: () -> Actions) -> some View {
        VStack(alignment: .leading, spacing: 15) {
            Label(title, systemImage: icon).font(.system(size: 16, weight: .medium)).foregroundStyle(Palette.bronze)
            Text(detail).font(.system(size: 13)).foregroundStyle(Palette.muted).lineSpacing(4)
            HStack(spacing: 10) { actions() }.controlSize(.large).disabled(session.busy || !session.loaded)
        }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.panel, in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.line))
    }
}

struct ComputerView: View {
    @AppStorage("computerURL") private var savedURL = ""
    @State private var address = ""
    @State private var error = ""
    @State private var showGuide = false

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            Image(systemName: "desktopcomputer").font(.system(size: 40, weight: .light)).foregroundStyle(Palette.bronze)
            Text("Room to do more.").font(.system(size: 34, weight: .medium, design: .rounded)).tracking(-0.9)
            Text("Open an existing Talos Computer for persistent projects, files and jobs. It can run headless; a graphical desktop is optional.")
                .foregroundStyle(Palette.muted).font(.system(size: 15)).lineSpacing(6)
            VStack(alignment: .leading, spacing: 12) {
                Text("Your private Computer link").font(.system(size: 12, weight: .medium))
                EntryField(placeholder: "https://your-computer.example", text: $address, onSubmit: openComputer)
                    .frame(height: 30)
                Button("Open Computer", action: openComputer).buttonStyle(BronzeButtonStyle()).controlSize(.large)
                if !error.isEmpty { Text(error).font(.system(size: 12)).foregroundStyle(Palette.bronze) }
            }.padding(24).background(Palette.panel, in: RoundedRectangle(cornerRadius: 14))
            Text("Already using Talos on another machine? Its /computer command gives you the private link. Sign-in tokens in that link are opened once and are not saved here.")
                .font(.system(size: 12)).foregroundStyle(Palette.muted).lineSpacing(4)
            Button("Computer setup guide") { showGuide = true }
                .buttonStyle(.plain).font(.system(size: 13)).foregroundStyle(Palette.bronze)
            Text("Computer hosting currently requires an ARM64 Linux machine. This Mac app connects to it.")
                .font(.system(size: 11)).foregroundStyle(Palette.muted)
            Spacer(minLength: 0)
        }.padding(55).frame(maxWidth: 760, maxHeight: .infinity, alignment: .topLeading)
            .onAppear { address = savedURL }
            .sheet(isPresented: $showGuide) {
                VStack(alignment: .leading, spacing: 15) {
                    Text("Computer setup").font(.title2)
                    ScrollView {
                        Text((try? String(contentsOf: Bundle.main.resourceURL!.appendingPathComponent("computer.md"), encoding: .utf8))
                             ?? "The Computer guide could not be loaded.")
                            .font(.system(size: 13, design: .monospaced)).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Button("Done") { showGuide = false }.keyboardShortcut(.defaultAction)
                }.padding(28).frame(width: 700, height: 620)
            }
    }

    private func openComputer() {
        guard var parts = URLComponents(string: address.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme?.lowercased() == "https", let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, let destination = parts.url else {
            error = "Enter a valid private HTTPS Computer link."
            return
        }
        parts.fragment = nil
        parts.query = nil
        savedURL = parts.url?.absoluteString ?? ""
        address = savedURL
        error = ""
        NSWorkspace.shared.open(destination)
    }
}
