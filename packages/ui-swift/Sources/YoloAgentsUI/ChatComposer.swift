import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import YoloAgentsSDK
private typealias ComposerViewState<Value> = SwiftUI.State<Value>

/// One native editor with a stable identity, outside the message scroll view.
/// Connectivity controls sending; it must not disable first-responder editing.
struct ChatComposer: View {
    let conversationID: UUID
    @Binding var text: String
    @Binding var images: [DraftImage]
    let isEditable: Bool
    let canSend: Bool
    let online: Bool
    let configuration: AgentComposerConfiguration
    let dismissRequest: Int
    let onSend: () -> Void
    @ComposerViewState private var focusRequest = 0
    @ComposerViewState private var editorHeight: CGFloat = 40
    @ComposerViewState private var pickerItems: [PhotosPickerItem] = []
    @ComposerViewState private var pickerError: String?
    private var placeholder: String {
        isEditable ? (online ? configuration.placeholder : configuration.offlinePlaceholder) :
            configuration.archivedPlaceholder
    }
    private var normalizedText: String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    private var sendEnabled: Bool { canSend && (!normalizedText.isEmpty || !images.isEmpty) }
    private var remainingImageCount: Int { max(0, MediaLimits.maximumItemsPerMessage - images.count) }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if !images.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(images) { image in
                            DraftImageThumbnail(image: image) {
                                images.removeAll { $0.id == image.id }
                            }
                        }
                    }
                    .padding(.horizontal, 4).padding(.top, 4)
                }
            }
            if let pickerError {
                Text(pickerError).font(.caption).foregroundStyle(.red)
                    .padding(.horizontal, 8)
            }
            HStack(alignment: .bottom, spacing: 4) {
                PhotosPicker(selection: $pickerItems,
                             maxSelectionCount: max(1, remainingImageCount), matching: .images) {
                    Image(systemName: "photo.on.rectangle")
                        .frame(width: 36, height: 36)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("选择图片")
                .accessibilityIdentifier("chat.add-image")
                .disabled(!isEditable || remainingImageCount == 0)

                #if os(macOS)
                MacMessageEditor(text: $text, measuredHeight: $editorHeight,
                                 placeholder: placeholder, isEditable: isEditable,
                                 canSubmit: sendEnabled, focusRequest: focusRequest,
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
        .onChange(of: pickerItems) { _, items in importImages(items) }
    }

    private func importImages(_ items: [PhotosPickerItem]) {
        guard !items.isEmpty else { return }
        pickerError = nil
        Task { @MainActor in
            defer { pickerItems = [] }
            do {
                var additions: [DraftImage] = []
                for item in items.prefix(remainingImageCount) {
                    guard let data = try await item.loadTransferable(type: Data.self) else {
                        throw DraftImageError.unreadable
                    }
                    let mediaType = item.supportedContentTypes
                        .first(where: { $0.conforms(to: .image) })?.preferredMIMEType
                    let prepared = try await Task.detached(priority: .userInitiated) {
                        try DraftImage.preparing(data: data, sourceMediaType: mediaType)
                    }.value
                    additions.append(prepared)
                }
                guard images.reduce(0, { $0 + $1.data.count }) +
                        additions.reduce(0, { $0 + $1.data.count }) <= MediaLimits.maximumBytesPerMessage else {
                    throw DraftImageError.messageTooLarge
                }
                images.append(contentsOf: additions)
            } catch {
                pickerError = error.localizedDescription
            }
        }
    }
}
