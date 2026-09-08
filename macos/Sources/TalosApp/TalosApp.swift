import AppKit
import SwiftUI

@main struct TalosMacApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var session = Session()

    var body: some Scene {
        Window("Talos", id: "main") {
            MainView(session: session)
                .frame(minWidth: 880, minHeight: 620)
                .preferredColorScheme(.dark)
                .tint(Palette.bronze)
                .task { delegate.session = session; session.refresh() }
        }
        .defaultSize(width: 1120, height: 780)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Open Chat") { session.start() }.keyboardShortcut("n")
            }
            CommandGroup(replacing: .appSettings) {
                Button("Connections…") { session.page = .connections }.keyboardShortcut(",")
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var session: Session?
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { session?.terminateSession() }
    }
}

enum Palette {
    static let background = Color(red: 0.055, green: 0.062, blue: 0.066)
    static let panel = Color(red: 0.083, green: 0.090, blue: 0.094)
    static let line = Color.white.opacity(0.085)
    static let bronze = Color(red: 0.80, green: 0.67, blue: 0.46)
    static let muted = Color(red: 0.57, green: 0.60, blue: 0.60)
}

struct MainView: View {
    @ObservedObject var session: Session
    @State private var message = ""

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Rectangle().fill(Palette.line).frame(width: 1)
            VStack(spacing: 0) {
                header
                Rectangle().fill(Palette.line).frame(height: 1)
                ZStack {
                    if session.hasTerminal {
                        VStack(spacing: 0) {
                            TerminalPane(session: session).padding(22)
                            if session.busy && session.currentAction == "chat" {
                                HStack(spacing: 12) {
                                    EntryField(placeholder: "Message Talos…", text: $message, onSubmit: sendMessage)
                                        .frame(height: 30)
                                    Button("Send", action: sendMessage).buttonStyle(BronzeButtonStyle())
                                        .disabled(message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                                        .accessibilityLabel("Send message")
                                }.padding(16).background(Palette.panel, in: RoundedRectangle(cornerRadius: 12))
                                    .padding(.horizontal, 24).padding(.bottom, 22)
                            }
                        }
                            .opacity(session.page == .chat ? 1 : 0)
                            .allowsHitTesting(session.page == .chat)
                            .accessibilityHidden(session.page != .chat)
                    }
                    if session.page == .home {
                        if session.status.configured { DashboardView(session: session) }
                        else { welcome }
                    }
                    if session.page == .history { HistoryView(session: session) }
                    if session.page == .connections { ConnectionsView(session: session) }
                    if session.page == .computer { ComputerView() }
                    if session.page == .chat && !session.hasTerminal { welcome }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Palette.background)
        .overlay(alignment: .bottom) {
            if !session.error.isEmpty {
                Label(session.error, systemImage: "exclamationmark.circle")
                    .font(.system(size: 13)).textSelection(.enabled)
                    .padding(16).background(Palette.panel, in: RoundedRectangle(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(Palette.bronze.opacity(0.4)))
                    .padding(24).padding(.leading, 210)
            }
        }
    }

    private func sendMessage() {
        session.submit(message)
        message = ""
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 28) {
            HStack(spacing: 10) {
                Image(nsImage: NSImage(contentsOf: Bundle.main.resourceURL!.appendingPathComponent("mark.png")) ?? NSImage())
                    .resizable().scaledToFit().frame(width: 36, height: 36)
                Text("TALOS").font(.system(size: 17, weight: .semibold, design: .rounded)).tracking(3)
            }.padding(.top, 48).padding(.bottom, 8)
            VStack(spacing: 7) {
                nav(.home, icon: "square.grid.2x2")
                nav(.chat, icon: "bubble.left.and.bubble.right")
                nav(.history, icon: "clock.arrow.circlepath")
                nav(.connections, icon: "point.3.connected.trianglepath.dotted")
                nav(.computer, icon: "desktopcomputer")
            }
            Spacer()
            VStack(alignment: .leading, spacing: 13) {
                HStack(spacing: 7) {
                    Circle().fill(session.busy ? Palette.bronze : Color.gray).frame(width: 6, height: 6)
                    Text(session.busy ? "SESSION ACTIVE" : "ON YOUR MAC")
                        .font(.system(size: 10, weight: .medium)).tracking(1.4)
                }.foregroundStyle(Palette.muted)
                Button { openWorkspace() } label: {
                    Label("Your workspace", systemImage: "folder").font(.system(size: 12))
                }.buttonStyle(.plain).foregroundStyle(Palette.muted)
                Link(destination: URL(string: "https://talos-agent.ch/docs/")!) {
                    Label("Help & documentation", systemImage: "questionmark.circle").font(.system(size: 12))
                }.foregroundStyle(Palette.muted)
            }
            .padding(.bottom, 22)
        }
        .padding(.horizontal, 20)
        .frame(width: 220)
        .background(Palette.panel.opacity(0.45))
    }

    private func nav(_ page: Session.Page, icon: String) -> some View {
        Button { session.page = page; session.refresh() } label: {
            HStack(spacing: 11) {
                Image(systemName: icon).font(.system(size: 15)).frame(width: 20)
                Text(page.rawValue).font(.system(size: 13, weight: .medium)).lineLimit(1)
                Spacer()
                if page == .chat && session.busy { Circle().fill(Palette.bronze).frame(width: 5, height: 5) }
            }.padding(.horizontal, 12).padding(.vertical, 12)
                .foregroundStyle(session.page == page ? Palette.bronze : Color.white.opacity(0.62))
                .background(session.page == page ? Palette.bronze.opacity(0.09) : Color.clear,
                            in: RoundedRectangle(cornerRadius: 9))
        }.buttonStyle(.plain).accessibilityLabel(page.rawValue)
    }

    private var header: some View {
        HStack {
            Text(session.page == .chat ? session.activity : session.page.rawValue)
                .font(.system(size: 13, weight: .medium)).foregroundStyle(.white.opacity(0.8))
            Spacer()
            if session.busy {
                Button { session.stop() } label: { Label("End session", systemImage: "stop.circle") }
                    .buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(Palette.muted).accessibilityLabel("End session")
            } else if session.status.configured && session.page != .home {
                Button("Open chat") { session.start() }.buttonStyle(BronzeButtonStyle()).controlSize(.small)
            }
        }.padding(.horizontal, 30).frame(height: 68)
    }

    private var welcome: some View {
        VStack(alignment: .leading, spacing: 26) {
            Spacer(minLength: 20)
            Text("YOUR OWN AGENT").font(.system(size: 11, weight: .semibold)).tracking(3).foregroundStyle(Palette.bronze)
            Text(session.status.configured ? "Make yourself\nat home." : "A little setup.\nA lot of possibility.")
                .font(.system(size: 46, weight: .medium, design: .rounded)).tracking(-1.6).lineSpacing(1)
            Text(session.status.configured
                 ? "Your connection is saved. Open a conversation and tell Talos what you want to get done."
                 : "Connect a model and start talking. Your workspace and history stay here on your Mac.")
                .font(.system(size: 16)).lineSpacing(6).foregroundStyle(Palette.muted).frame(maxWidth: 470, alignment: .leading)
            HStack(spacing: 15) {
                Button {
                    session.start(session.status.configured ? "chat" : (session.status.claude_available ? "quick-claude" : "chat"))
                } label: {
                    HStack(spacing: 20) {
                        Text(session.status.configured ? "Open chat" : (session.status.claude_available ? "Continue with Claude" : "Get started"))
                        Image(systemName: "arrow.right")
                    }.font(.system(size: 14, weight: .semibold)).padding(.horizontal, 18).padding(.vertical, 12)
                }.buttonStyle(BronzeButtonStyle()).foregroundStyle(Palette.background).disabled(!session.loaded)
                    .accessibilityLabel(session.status.configured ? "Open chat" : (session.status.claude_available ? "Continue with Claude" : "Get started"))
                if !session.status.configured && session.status.claude_available {
                    Button("Other connection") { session.start("chat") }
                        .buttonStyle(.plain).foregroundStyle(Palette.muted).font(.system(size: 13))
                }
            }.padding(.top, 5)
            if !session.status.configured {
                Text(session.status.claude_available
                     ? "Uses your Claude login and enables this Mac account. No API key to copy."
                     : "The guided setup helps you choose a model connection.")
                    .font(.system(size: 11)).foregroundStyle(Palette.muted)
            }
            Spacer(minLength: 28)
            HStack(alignment: .top, spacing: 36) {
                feature("Start here", detail: "Chat on this Mac", icon: "bubble.left")
                feature("Add later", detail: "Telegram and more", icon: "paperplane")
                feature("Make room", detail: "Connect a computer", icon: "desktopcomputer")
            }
            .padding(.top, 26).padding(.bottom, 40)
            .overlay(alignment: .top) { Rectangle().fill(Palette.line).frame(height: 1) }
        }.padding(.horizontal, 58).frame(maxWidth: 780, maxHeight: .infinity, alignment: .leading)
    }

    private func feature(_ title: String, detail: String, icon: String) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Image(systemName: icon).font(.system(size: 19)).foregroundStyle(Palette.bronze.opacity(0.75))
            Text(title).font(.system(size: 12, weight: .semibold))
            Text(detail).font(.system(size: 11)).foregroundStyle(Palette.muted)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    private func openWorkspace() {
        guard !session.status.workspace.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: session.status.workspace))
    }
}

struct BronzeButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 13, weight: .semibold))
            .padding(.horizontal, 14).padding(.vertical, 9)
            .foregroundStyle(Palette.background)
            .background(Palette.bronze.opacity(enabled ? (configuration.isPressed ? 0.8 : 1) : 0.4),
                        in: RoundedRectangle(cornerRadius: 8))
    }
}
