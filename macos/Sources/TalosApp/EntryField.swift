import AppKit
import SwiftUI

// Native value changes, including VoiceOver/AXSetValue, must update the binding.
// SwiftUI's field can otherwise display the new text without notifying its model.
struct EntryField: NSViewRepresentable {
    let placeholder: String
    @Binding var text: String
    var onSubmit: () -> Void = {}

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeNSView(context: Context) -> Field {
        let field = Field()
        field.placeholderString = placeholder
        field.setAccessibilityLabel(placeholder)
        field.font = .systemFont(ofSize: 14)
        field.bezelStyle = .roundedBezel
        field.isBezeled = true
        field.isEditable = true
        field.isSelectable = true
        field.delegate = context.coordinator
        field.target = context.coordinator
        field.action = #selector(Coordinator.submit)
        field.changed = { context.coordinator.parent.text = $0 }
        return field
    }
    func updateNSView(_ view: Field, context: Context) {
        context.coordinator.parent = self
        if view.stringValue != text { view.stringValue = text }
    }
    final class Field: NSTextField {
        var changed: ((String) -> Void)?
        override func setAccessibilityValue(_ value: Any?) {
            guard let value = value as? String else { return }
            stringValue = value
            changed?(value)
        }
    }
    final class Coordinator: NSObject, NSTextFieldDelegate {
        var parent: EntryField
        init(_ parent: EntryField) { self.parent = parent }
        func controlTextDidChange(_ notification: Notification) {
            if let field = notification.object as? NSTextField { parent.text = field.stringValue }
        }
        @objc func submit() { parent.onSubmit() }
    }
}
