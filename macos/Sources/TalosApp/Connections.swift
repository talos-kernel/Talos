import AppKit
import Darwin
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
                HStack {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Found on this Mac").font(.system(size: 19, weight: .medium))
                        Text("Existing CLI accounts are detected automatically. No credentials are imported by this check.")
                            .font(.system(size: 12)).foregroundStyle(Palette.muted)
                    }
                    Spacer()
                    Button { session.refresh() } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                        .buttonStyle(.bordered).accessibilityLabel("Refresh CLI accounts")
                }
                ForEach(session.status.accounts) { account in
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text(account.label).font(.system(size: 16, weight: .medium))
                            Spacer()
                            Label(account.status_label, systemImage: account.status == "signed_in" ? "checkmark.circle.fill"
                                  : account.status == "login_found" ? "key.fill" : "circle.dotted")
                                .font(.system(size: 11, weight: .medium))
                                .foregroundStyle(account.status == "signed_in" || account.status == "login_found"
                                                 ? Palette.bronze : Palette.muted)
                        }
                        Text(account.detail).font(.system(size: 12)).foregroundStyle(Palette.muted).lineSpacing(3)
                        if !account.action.isEmpty {
                            Button(account.action_label) { session.start(account.action) }
                                .buttonStyle(.bordered).disabled(session.busy || !session.loaded)
                        }
                    }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
                        .background(Palette.panel, in: RoundedRectangle(cornerRadius: 14))
                        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.line))
                }
                card("Ollama · on this Mac", icon: "externaldrive", detail: "Choose from the models installed in your local Ollama. No API key or cloud connection needed.") {
                    Button("Connect Ollama") { session.start("ollama") }.buttonStyle(.bordered)
                }
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

enum LocalComputerLink {
    static let relativePath = "Library/Application Support/Talos/computer.url"

    static func validate(_ text: String) -> URL? {
        guard let parts = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme?.lowercased() == "http",
              ["127.0.0.1", "::1", "[::1]"].contains(parts.host ?? ""),
              parts.port == 8830,
              parts.user == nil, parts.password == nil, parts.query == nil,
              parts.path.isEmpty || parts.path == "/",
              let fragment = parts.fragment, fragment.hasPrefix("token="),
              !fragment.dropFirst(6).isEmpty,
              fragment.dropFirst(6).count <= 128,
              fragment.dropFirst(6).allSatisfy({ $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }),
              let url = parts.url else { return nil }
        return url
    }

    static func load(from file: URL? = nil) -> URL? {
        let path = file ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(relativePath)
        var metadata = stat()
        guard lstat(path.path, &metadata) == 0,
              (metadata.st_mode & S_IFMT) == S_IFREG,
              metadata.st_uid == getuid(),
              (metadata.st_mode & 0o077) == 0,
              let data = try? Data(contentsOf: path), data.count <= 2048,
              let text = String(data: data, encoding: .utf8) else { return nil }
        return validate(text)
    }
}

enum ComputerViewBackend: Equatable {
    case omarchy(URL)
    case remote

    static func select(localURL: URL?) -> Self {
        localURL.map(Self.omarchy) ?? .remote
    }
}

struct ComputerView: View {
    @AppStorage("computerURL") private var savedURL = ""
    @State private var address = ""
    @State private var error = ""
    @State private var showGuide = false
    @State private var localURL: URL?

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            Image(systemName: "desktopcomputer").font(.system(size: 40, weight: .light)).foregroundStyle(Palette.bronze)
            Text("Room to do more.").font(.system(size: 34, weight: .medium, design: .rounded)).tracking(-0.9)
            Text(localURL == nil
                 ? "Connect a private Linux Computer, or install the Omarchy desktop on this Mac."
                 : "Use the Omarchy desktop installed on this Mac.")
                .foregroundStyle(Palette.muted).font(.system(size: 15)).lineSpacing(6)
            switch ComputerViewBackend.select(localURL: localURL) {
            case let .omarchy(localURL):
                VStack(alignment: .leading, spacing: 12) {
                    Label("Omarchy Computer · this Mac", systemImage: "shield.lefthalf.filled")
                        .font(.system(size: 16, weight: .medium)).foregroundStyle(Palette.bronze)
                    Text("A visual desktop with outbound NAT and human takeover. It accepts no inbound forwarding and shares no Mac files, clipboard or credentials.")
                        .font(.system(size: 13)).foregroundStyle(Palette.muted).lineSpacing(4)
                    Button("Open Omarchy Computer") { NSWorkspace.shared.open(localURL) }
                        .buttonStyle(BronzeButtonStyle()).controlSize(.large)
                }.padding(24).background(Palette.panel, in: RoundedRectangle(cornerRadius: 14))
                    .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.line))
                Text("Omarchy is the active Computer on this Mac. Remote Computer URLs are not shown or used while this private local link remains valid.")
                    .font(.system(size: 11)).foregroundStyle(Palette.muted).lineSpacing(4)
            case .remote:
                Text("The local Omarchy Computer is not installed or its private link failed validation.")
                    .font(.system(size: 12)).foregroundStyle(Palette.muted)
                VStack(alignment: .leading, spacing: 12) {
                    Text("Talos Computer").font(.system(size: 12, weight: .medium))
                    EntryField(placeholder: "https://your-computer.example", text: $address, onSubmit: openComputer)
                        .frame(height: 30)
                    Button("Open Linux Computer", action: openComputer).buttonStyle(.bordered).controlSize(.large)
                    if !error.isEmpty { Text(error).font(.system(size: 12)).foregroundStyle(Palette.bronze) }
                }.padding(24).background(Palette.panel, in: RoundedRectangle(cornerRadius: 14))
                Text("Already using Talos on another machine? Its /computer command gives you the private link. Sign-in tokens in that link are opened once and are not saved here.")
                    .font(.system(size: 12)).foregroundStyle(Palette.muted).lineSpacing(4)
                Button("Computer setup guide") { showGuide = true }
                    .buttonStyle(.plain).font(.system(size: 13)).foregroundStyle(Palette.bronze)
            }
            Spacer(minLength: 0)
        }.padding(55).frame(maxWidth: 760, maxHeight: .infinity, alignment: .topLeading)
            .onAppear {
                address = savedURL
                localURL = LocalComputerLink.load()
            }
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
