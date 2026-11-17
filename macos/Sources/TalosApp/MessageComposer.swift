import AppKit
import SwiftUI

/// Five visible lines, soft wrapping and scrolling. Enter sends one whole message;
/// Shift-Enter inserts a newline. Pasted text is always plain text.
struct MessageComposer: NSViewRepresentable {
    let placeholder: String
    @Binding var text: String
    var onSubmit: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: NSScrollView, context: Context) -> CGSize? {
        // NSTextView's growing document must not drive the outer SwiftUI layout.
        CGSize(width: proposal.width ?? 500, height: 112)
    }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        let editor = Editor(frame: NSRect(x: 0, y: 0, width: 500, height: 112))
        editor.isRichText = false
        editor.isAutomaticQuoteSubstitutionEnabled = false
        editor.isAutomaticDashSubstitutionEnabled = false
        editor.isAutomaticTextReplacementEnabled = false
        editor.drawsBackground = false
        editor.font = .systemFont(ofSize: 14)
        editor.textColor = .textColor
        editor.insertionPointColor = .textColor
        editor.textContainerInset = NSSize(width: 10, height: 10)
        editor.isVerticallyResizable = true
        editor.isHorizontallyResizable = false
        editor.autoresizingMask = [.width]
        editor.textContainer?.widthTracksTextView = true
        editor.textContainer?.containerSize = NSSize(width: 500, height: CGFloat.greatestFiniteMagnitude)
        editor.minSize = NSSize(width: 0, height: 112)
        editor.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        editor.delegate = context.coordinator
        editor.changed = { context.coordinator.parent.text = $0 }
        editor.submitted = { context.coordinator.parent.onSubmit() }
        editor.placeholder = placeholder
        editor.setAccessibilityLabel(placeholder)
        scroll.documentView = editor
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let editor = scroll.documentView as? Editor else { return }
        editor.placeholder = placeholder
        editor.setAccessibilityLabel(placeholder)
        if editor.string != text { editor.string = text }
        editor.needsDisplay = true
    }

    final class Editor: NSTextView {
        var changed: ((String) -> Void)?
        var submitted: (() -> Void)?
        var placeholder = ""
        override func keyDown(with event: NSEvent) {
            if [36, 76].contains(event.keyCode), !event.modifierFlags.contains(.shift),
               !hasMarkedText() {
                submitted?()
            } else { super.keyDown(with: event) }
        }
        override func setAccessibilityValue(_ value: Any?) {
            guard let value = value as? String else { return }
            string = value
            changed?(value)
            needsDisplay = true
        }
        override func draw(_ dirtyRect: NSRect) {
            super.draw(dirtyRect)
            if string.isEmpty {
                (placeholder as NSString).draw(at: NSPoint(x: 15, y: 10), withAttributes: [
                    .font: font ?? NSFont.systemFont(ofSize: 14), .foregroundColor: NSColor.placeholderTextColor
                ])
            }
        }
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: MessageComposer
        init(_ parent: MessageComposer) { self.parent = parent }
        func textDidChange(_ notification: Notification) {
            if let editor = notification.object as? Editor {
                parent.text = editor.string
                editor.needsDisplay = true
            }
        }
    }
}
