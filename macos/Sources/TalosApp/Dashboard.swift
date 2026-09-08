import SwiftUI

struct DashboardView: View {
    @ObservedObject var session: Session
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("YOUR WORK, WITHIN REACH").font(.system(size: 10, weight: .semibold)).tracking(2.4).foregroundStyle(Palette.bronze)
                        Text("Welcome back.").font(.system(size: 36, weight: .medium, design: .rounded)).tracking(-1)
                        Text("One place for your agent, conversations and connections.")
                            .font(.system(size: 14)).foregroundStyle(Palette.muted)
                    }
                    Spacer()
                    Button(session.busy ? "Return to chat" : "Open chat") { session.start() }
                        .buttonStyle(BronzeButtonStyle()).controlSize(.large).foregroundStyle(Palette.background)
                }
                HStack(spacing: 14) {
                    metric("CURRENT SESSION", value: session.busy ? "Running" : "Ready", detail: session.busy ? session.activity : "Start whenever you need it", icon: "waveform")
                    metric("SAVED EXCHANGES", value: session.archive.available ? "\(session.archive.total)" : "—", detail: "Stored on this Mac", icon: "clock.arrow.circlepath")
                }
                VStack(alignment: .leading, spacing: 16) {
                    HStack {
                        Label("Model connection", systemImage: "sparkle").foregroundStyle(Palette.bronze)
                        Spacer()
                        Button("Manage") { session.page = .connections }.buttonStyle(.plain)
                    }.font(.system(size: 13, weight: .medium))
                    Text(session.status.model).font(.system(size: 22, weight: .medium, design: .monospaced))
                    Text("\(session.status.provider) · Connection saved")
                        .font(.system(size: 12)).foregroundStyle(Palette.muted)
                }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Palette.panel, in: RoundedRectangle(cornerRadius: 14))
                HStack {
                    Text("Recent conversations").font(.system(size: 17, weight: .medium))
                    Spacer()
                    Button("View history") { session.page = .history; session.refresh() }.buttonStyle(.plain).foregroundStyle(Palette.bronze)
                }
                if session.archive.turns.isEmpty {
                    Text("Your completed exchanges will appear here after your first conversation.")
                        .font(.system(size: 13)).foregroundStyle(Palette.muted).padding(.vertical, 10)
                } else {
                    ForEach(session.archive.turns.prefix(3)) { turn in
                        Button { session.page = .history } label: {
                            HStack(spacing: 15) {
                                Image(systemName: "bubble.left").foregroundStyle(Palette.bronze)
                                Text(turn.asked).lineLimit(1).font(.system(size: 14))
                                Spacer()
                                Text(turn.date, style: .date).font(.system(size: 11)).foregroundStyle(Palette.muted)
                                Image(systemName: "arrow.up.right").font(.system(size: 11)).foregroundStyle(Palette.muted)
                            }.padding(18).background(Palette.panel.opacity(0.6), in: RoundedRectangle(cornerRadius: 10))
                        }.buttonStyle(.plain).accessibilityLabel("Saved conversation: \(turn.asked.prefix(80))")
                    }
                }
                HStack(spacing: 22) {
                    Button { session.page = .computer } label: { Label("Open your Computer", systemImage: "desktopcomputer") }
                    Button { session.page = .connections } label: { Label("Add a connection", systemImage: "plus.circle") }
                }.buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(Palette.bronze).padding(.top, 5)
            }.padding(42)
        }
    }
    private func metric(_ title: String, value: String, detail: String, icon: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text(title).font(.system(size: 10, weight: .semibold)).tracking(1.3); Spacer(); Image(systemName: icon) }
                .foregroundStyle(Palette.muted)
            Text(value).font(.system(size: 29, weight: .medium, design: .rounded))
            Text(detail).font(.system(size: 11)).foregroundStyle(Palette.muted).lineLimit(1)
        }.padding(22).frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.panel, in: RoundedRectangle(cornerRadius: 14))
    }
}

struct HistoryView: View {
    @ObservedObject var session: Session
    @State private var query = ""
    @State private var selected: Int?
    var filtered: [SavedTurn] {
        session.archive.turns.filter { query.isEmpty || ($0.asked + " " + $0.answered).localizedCaseInsensitiveContains(query) }
    }
    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Your history").font(.system(size: 24, weight: .medium, design: .rounded))
                EntryField(placeholder: "Search recent exchanges", text: $query).frame(height: 28)
                Text("Latest 100 exchanges · \(session.archive.total) saved")
                    .font(.system(size: 11)).foregroundStyle(Palette.muted)
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(filtered) { turn in
                            Button { selected = turn.id } label: {
                                VStack(alignment: .leading, spacing: 8) {
                                    Text(turn.asked).font(.system(size: 13, weight: .medium)).lineLimit(2)
                                    Text(turn.date, style: .date).font(.system(size: 11)).foregroundStyle(Palette.muted)
                                }.padding(14).frame(maxWidth: .infinity, alignment: .leading)
                                    .background(selected == turn.id ? Palette.bronze.opacity(0.12) : Palette.panel,
                                                in: RoundedRectangle(cornerRadius: 9))
                            }.buttonStyle(.plain).accessibilityLabel("Read saved exchange: \(turn.asked.prefix(80))")
                        }
                    }
                }
            }.padding(26).frame(minWidth: 225, idealWidth: 285, maxWidth: 350)
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    if !session.archive.available {
                        Text("The archive could not be read. Your saved files have not been changed.")
                    } else if let turn = session.archive.turns.first(where: { $0.id == selected }) {
                        Text(turn.date.formatted(date: .abbreviated, time: .shortened)).font(.system(size: 12)).foregroundStyle(Palette.muted)
                        Text("YOU").font(.system(size: 11, weight: .semibold)).tracking(2).foregroundStyle(Palette.bronze)
                        Text(turn.asked).font(.system(size: 15)).textSelection(.enabled)
                        Divider()
                        Text("TALOS").font(.system(size: 11, weight: .semibold)).tracking(2).foregroundStyle(Palette.bronze)
                        Text(turn.answered).font(.system(size: 15)).lineSpacing(5).textSelection(.enabled)
                        Text("Saved archive. Use session_search in chat to find older exchanges. Starting a new session keeps this archive.")
                            .font(.system(size: 11)).foregroundStyle(Palette.muted).padding(.top, 14)
                    } else {
                        Image(systemName: "clock.arrow.circlepath").font(.system(size: 36, weight: .light)).foregroundStyle(Palette.bronze)
                        Text(session.archive.turns.isEmpty ? "Your story starts here." : "Pick up the thread.")
                            .font(.system(size: 26, weight: .medium, design: .rounded))
                        Text("Select a saved exchange to read it. This view only shows conversations from this Mac profile.")
                            .font(.system(size: 14)).foregroundStyle(Palette.muted).lineSpacing(4)
                    }
                }.padding(35).frame(maxWidth: .infinity, alignment: .leading)
            }.frame(minWidth: 340)
        }
    }
}
