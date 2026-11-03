import SwiftUI
private typealias ComposerViewState<Value> = SwiftUI.State<Value>

/// One native editor with a stable identity, outside the message scroll view.
/// Connectivity controls sending; it must not disable first-responder editing.
struct ChatComposer: View {
    let conversationID: UUID
    @Binding var text: String
    let isEditable: Bool
    let canSend: Bool
    let online: Bool
    let configuration: AgentComposerConfiguration
    let dismissRequest: Int
    let onSend: () -> Void
    @ComposerViewState private var focusRequest = 0
    @ComposerViewState private var editorHeight: CGFloat = 40
    private var placeholder: String {
        isEditable ? (online ? configuration.placeholder : configuration.offlinePlaceholder) :
            configuration.archivedPlaceholder
    }
    private var normalizedText: String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    private var sendEnabled: Bool { canSend && !normalizedText.isEmpty }

    var body: some View {
        HStack(alignment: .bottom, spacing: 6) {
            #if os(macOS)
            MacMessageEditor(text: $text, measuredHeight: $editorHeight,
                             placeholder: placeholder, isEditable: isEditable,
                             canSubmit: canSend, focusRequest: focusRequest,
                             dismissRequest: dismissRequest, onSubmit: onSend)
                .frame(height: editorHeight)
            #else
            IOSMessageEditor(text: $text, measuredHeight: $editorHeight,
                             placeholder: placeholder, isEditable: isEditable,
                             focusRequest: focusRequest, dismissRequest: dismissRequest)
                .frame(height: editorHeight)
            #endif

            Button {
                onSend()
                focusRequest += 1
            } label: {
                Image(systemName: "arrow.up")
                    .font(.system(size: 16, weight: .bold))
                    .foregroundStyle(sendEnabled ? Color.white : Color.secondary)
                    .frame(width: 36, height: 36)
                    .background(sendEnabled ? Color.accentColor : Color.secondary.opacity(0.16), in: Circle())
                    .frame(width: 44, height: 44)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("发送消息")
            .accessibilityIdentifier("chat.send")
            .disabled(!sendEnabled)
        }
        .padding(.horizontal, 6).padding(.vertical, 5)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.10), lineWidth: 0.75)
        }
        .shadow(color: .black.opacity(0.08), radius: 8, y: 2)
        .contentShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .simultaneousGesture(TapGesture().onEnded { if isEditable { focusRequest += 1 } })
        .padding(.horizontal, 12).padding(.top, 8).padding(.bottom, 7)
        .onChange(of: conversationID) { _, _ in editorHeight = 40 }
    }
}
