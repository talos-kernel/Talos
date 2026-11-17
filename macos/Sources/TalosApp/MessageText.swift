import SwiftUI
import AppKit

struct MessageBlock: Identifiable, Equatable {
    enum Kind: Equatable { case prose, heading, code }
    let id: Int
    let kind: Kind
    let text: String
    var language: String = ""

    static func parse(_ text: String) -> [MessageBlock] {
        var blocks: [MessageBlock] = []
        var code: [String]? = nil
        var language = ""
        for line in text.components(separatedBy: "\n") {
            if line.hasPrefix("```") {
                if let lines = code {
                    blocks.append(.init(id: blocks.count, kind: .code, text: lines.joined(separator: "\n"), language: language))
                    code = nil
                } else { code = []; language = String(line.dropFirst(3)).trimmingCharacters(in: .whitespaces) }
            } else if code != nil { code?.append(line) }
            else if !line.trimmingCharacters(in: .whitespaces).isEmpty {
                let heading = line.hasPrefix("#") && line.contains(" ")
                let value = heading ? String(line.drop(while: { $0 == "#" || $0 == " " })) : line
                blocks.append(.init(id: blocks.count, kind: heading ? .heading : .prose, text: value))
            }
        }
        if let lines = code { blocks.append(.init(id: blocks.count, kind: .code, text: lines.joined(separator: "\n"), language: language)) }
        return blocks
    }
}

struct MessageText: View {
    let text: String
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(MessageBlock.parse(text)) { block in
                if block.kind == .code {
                    VStack(alignment: .leading, spacing: 0) {
                        HStack {
                            Text(block.language.isEmpty ? "Code" : block.language).font(.caption.weight(.medium))
                            Spacer()
                            Button {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(block.text, forType: .string)
                            } label: { Label("Copy", systemImage: "doc.on.doc") }
                                .buttonStyle(.plain).font(.caption).accessibilityLabel("Copy code")
                        }.foregroundStyle(Palette.muted).padding(.horizontal, 14).padding(.vertical, 10)
                        Divider().overlay(Palette.line)
                        ScrollView(.horizontal) {
                            Text(block.text).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                                .padding(14).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }.background(Palette.panel, in: RoundedRectangle(cornerRadius: 10))
                        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Palette.line))
                } else {
                    Text(inline(block.text.hasPrefix("- ") ? "• " + block.text.dropFirst(2) : block.text))
                        .font(block.kind == .heading ? .headline : .body).lineSpacing(5)
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        // A generated URL is display content. Opening a web link requires a click;
        // file:, shell: and other application schemes never become launch authority.
        .environment(\.openURL, OpenURLAction { url in
            guard ["https", "http"].contains(url.scheme?.lowercased() ?? ""),
                  url.user == nil, url.password == nil else { return .discarded }
            return .systemAction
        })
    }

    private func inline(_ value: String) -> AttributedString {
        (try? AttributedString(markdown: value, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(value)
    }
}

struct ToolStepView: View {
    let item: LiveItem
    var body: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 8) {
                if let text = item.text, !text.isEmpty { Text(text).textSelection(.enabled) }
                if let preview = item.preview, !preview.isEmpty {
                    Text(preview).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                }
            }.font(.callout).foregroundStyle(Palette.muted).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 8)
        } label: {
            HStack(spacing: 10) {
                Image(systemName: item.status == "running" ? "circle.dotted" : item.status == "done" ? "checkmark" : "exclamationmark.circle")
                    .font(.system(size: 12)).foregroundStyle(Palette.bronze).frame(width: 16)
                Text((item.tool ?? "Tool").replacingOccurrences(of: "_", with: " ").capitalized)
                    .font(.system(size: 13, weight: .medium))
                Spacer()
                Text(item.status == "needs_human" ? "Needs approval" : (item.status ?? "").capitalized)
                    .font(.caption).foregroundStyle(Palette.muted)
                if let duration = item.duration {
                    Text(String(format: "%.1fs", duration)).font(.caption.monospacedDigit()).foregroundStyle(Palette.muted)
                }
            }
        }.padding(12).background(Palette.panel, in: RoundedRectangle(cornerRadius: 10))
    }
}

struct ConversationView: View {
    @ObservedObject var session: Session
    var body: some View {
        ScrollViewReader { scroll in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 22) {
                    if session.archive.turns.isEmpty && session.live.items.isEmpty {
                        Text("What would you like to work on?").font(.title2.weight(.medium))
                        Text("Answers, code and live work appear here.").foregroundStyle(Palette.muted)
                    }
                    ForEach(session.archive.turns.prefix(30).reversed().filter {
                        session.live.started == 0 || $0.ts < session.live.started
                    }) { turn in
                        userMessage(turn.asked)
                        assistantMessage(turn.answered)
                    }
                    ForEach(session.live.items) { item in
                        if item.kind == "user" {
                            VStack(alignment: .trailing, spacing: 5) {
                                userMessage(item.text ?? "")
                                if let status = item.status {
                                    Text(["queued": "Queued · starts next", "running": "Working", "ended": "Reply done",
                                          "cancelled": "Cancelled", "refused": "Not queued · see the reply below", "redirected": "Added to the current task",
                                          "failed": "Finished with an issue"][status] ?? status)
                                        .font(.caption).foregroundStyle(Palette.muted)
                                }
                            }
                        }
                        else if item.kind == "assistant" {
                            VStack(alignment: .leading, spacing: 12) {
                                if item.literal == true { Text(item.text ?? "").textSelection(.enabled).lineSpacing(4) }
                                else { assistantMessage(item.text ?? "") }
                                if let buttons = item.buttons, let messageID = item.channel_message_id, !buttons.isEmpty {
                                    VStack(alignment: .leading, spacing: 10) {
                                        ForEach(Array((item.keyboard ?? [buttons]).enumerated()), id: \.offset) { _, row in
                                            LazyVGrid(columns: [GridItem(.adaptive(minimum: 155), alignment: .leading)], alignment: .leading) {
                                                ForEach(row, id: \.self) { button in
                                                    Button(button.label) { session.respond(button, messageID: messageID) }
                                                        .buttonStyle(.bordered).disabled(!session.busy)
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        else if item.kind == "attachment", let path = item.path {
                            Button {
                                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
                            } label: { Label(item.text ?? "File", systemImage: "doc") }
                                .buttonStyle(.bordered).help("Show this file in Finder")
                        }
                        else if item.kind == "tool" { ToolStepView(item: item) }
                        else { MessageText(text: item.text ?? "").foregroundStyle(Palette.muted) }
                    }
                    if session.busy {
                        HStack(spacing: 9) {
                            if session.live.phase == "working" || session.live.phase == "starting" {
                                ProgressView().controlSize(.small)
                            } else {
                                Image(systemName: session.live.phase == "approval" ? "hand.raised" : "circle.fill")
                                    .font(.system(size: 9)).foregroundStyle(Palette.bronze)
                            }
                            Text(session.live.phase == "approval" ? "Choose an approval option above" :
                                    session.live.phase == "question" ? "Waiting for your answer" :
                                    session.live.phase == "ready" ? "Ready for your message" :
                                    session.live.phase == "starting" ? "Opening your session…" : "Talos is working…")
                                .font(.caption).foregroundStyle(Palette.muted)
                        }.padding(.vertical, 8)
                    }
                    Color.clear.frame(height: 1).id("latest")
                }.padding(28).frame(maxWidth: 840, alignment: .leading).frame(maxWidth: .infinity)
            }
            .onAppear { scroll.scrollTo("latest", anchor: .bottom) }
            .onChange(of: session.live.items.last?.id) { _, _ in scroll.scrollTo("latest", anchor: .bottom) }
            .onChange(of: session.archive.turns.first?.id) { _, _ in
                if session.live.items.isEmpty { scroll.scrollTo("latest", anchor: .bottom) }
            }
        }
    }

    private func userMessage(_ text: String) -> some View {
        HStack {
            Spacer(minLength: 65)
            Text(text).font(.body).textSelection(.enabled).lineSpacing(4)
                .padding(16).background(Palette.panel, in: RoundedRectangle(cornerRadius: 14))
        }.padding(.top, 10)
    }

    private func assistantMessage(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("TALOS").font(.system(size: 10, weight: .semibold)).tracking(2).foregroundStyle(Palette.bronze)
            MessageText(text: text)
        }.padding(.vertical, 6)
    }
}
