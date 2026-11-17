import SwiftUI

struct CoreUpdate: Decodable {
    let current: String
    let latest: String
    let available: Bool
    let notes: String
}

struct UpdatesView: View {
    @ObservedObject var session: Session
    private var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "—"
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Text("Keep Talos up to date").font(.largeTitle.weight(.medium))
                Text("Mac app \(appVersion) · Talos core \(session.status.version)")
                    .font(.body.monospacedDigit()).foregroundStyle(Palette.muted)
                VStack(alignment: .leading, spacing: 16) {
                    if session.checkingUpdates {
                        HStack { ProgressView().controlSize(.small); Text("Checking the official release…") }
                    } else if !session.updateError.isEmpty {
                        Label("Update check failed", systemImage: "exclamationmark.circle")
                        Text(session.updateError).foregroundStyle(Palette.muted).textSelection(.enabled)
                    } else if let info = session.updateInfo {
                        Label(info.available ? "Talos \(info.latest) is available" : "Your Talos core is up to date",
                              systemImage: info.available ? "arrow.down.circle" : "checkmark.circle")
                            .font(.headline)
                        Text("Installed: \(info.current) · Published: \(info.latest)").foregroundStyle(Palette.muted)
                        if info.available {
                            Button("Install update") { session.start("update") }
                                .buttonStyle(BronzeButtonStyle()).disabled(session.busy)
                            if session.busy {
                                Text("End the current session before installing. Your conversation stays in History.")
                                    .font(.callout).foregroundStyle(Palette.muted)
                            }
                        }
                    } else {
                        Text("Check for a newer Talos core.").font(.headline)
                    }
                    Button("Check for updates") { session.checkForUpdates() }
                        .disabled(session.checkingUpdates).accessibilityLabel("Check for updates")
                }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Palette.panel, in: RoundedRectangle(cornerRadius: 14))
                Text("Updates verify the release signature and checksum, then run the tests before switching. Your history, model connections and workspace stay in place.")
                    .font(.callout).foregroundStyle(Palette.muted).lineSpacing(4)
                    .fixedSize(horizontal: false, vertical: true)
                Text("This updates the Talos core. Mac app interface updates are distributed separately.")
                    .font(.caption).foregroundStyle(Palette.muted)
                if let info = session.updateInfo {
                    Text("Release notes").font(.title2)
                    MessageText(text: info.notes)
                }
            }.frame(maxWidth: .infinity, alignment: .leading).padding(40)
        }
    }
}
