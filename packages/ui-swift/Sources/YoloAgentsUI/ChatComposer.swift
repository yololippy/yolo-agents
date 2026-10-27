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
    let onSend: () -> Void
    @FocusState private var focused: Bool
    @ComposerViewState private var focusRequest = 0
    private var placeholder: String {
        isEditable ? (online ? "输入消息…" : "离线草稿，连接恢复后可发送") : "此会话已归档"
    }

    var body: some View {
        HStack(alignment: .bottom, spacing: 12) {
            #if os(macOS)
            MacMessageEditor(text: $text, placeholder: placeholder, isEditable: isEditable,
                             canSubmit: canSend, focusRequest: focusRequest, onSubmit: onSend)
                .frame(height: 92)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
            #else
            ZStack(alignment: .topLeading) {
                TextEditor(text: $text)
                    .font(.body)
                    .scrollContentBackground(.hidden)
                    .focused($focused)
                    .frame(height: 76)
                    .disabled(!isEditable)
                    .accessibilityLabel("消息输入")
                    .accessibilityIdentifier("chat.composer")
                if text.isEmpty {
                    Text(placeholder)
                        .font(.body).foregroundStyle(.secondary)
                        .padding(.top, 8).padding(.leading, 5)
                        .allowsHitTesting(false)
                }
            }
            .padding(8)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
            .contentShape(Rectangle())
            .simultaneousGesture(TapGesture().onEnded { if isEditable { focused = true } })
            #endif

            Button {
                onSend()
                #if os(macOS)
                focusRequest += 1
                #else
                focused = isEditable
                #endif
            } label: {
                Image(systemName: "arrow.up.circle.fill").font(.system(size: 30))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("发送消息")
            .accessibilityIdentifier("chat.send")
            .disabled(!canSend || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
        .padding()
        .onChange(of: conversationID) { _, _ in focused = false }
        .onChange(of: isEditable) { _, editable in if !editable { focused = false } }
    }
}
