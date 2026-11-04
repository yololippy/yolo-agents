import SwiftUI
import YoloAgentsSDK
#if os(macOS)
import AppKit
#elseif os(iOS)
import UIKit
#endif

private actor MediaImageCache {
    struct Key: Hashable, Sendable {
        let serverID: UUID
        let mediaID: UUID
    }

    static let shared = MediaImageCache()
    private var values: [Key: Data] = [:]
    private var order: [Key] = []
    private var byteCount = 0
    private let limit = 40 * 1024 * 1024

    func data(for key: Key) -> Data? { values[key] }

    func insert(_ data: Data, for key: Key) {
        if let old = values[key] { byteCount -= old.count }
        values[key] = data
        order.removeAll { $0 == key }
        order.append(key)
        byteCount += data.count
        while byteCount > limit, let oldest = order.first {
            order.removeFirst()
            if let removed = values.removeValue(forKey: oldest) { byteCount -= removed.count }
        }
    }
}

struct DraftImageThumbnail: View {
    let image: DraftImage
    let onRemove: () -> Void

    var body: some View {
        ZStack(alignment: .topTrailing) {
            platformImage(image.data)
                .aspectRatio(contentMode: .fill)
                .frame(width: 76, height: 76)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            Button(action: onRemove) {
                Image(systemName: "xmark.circle.fill")
                    .symbolRenderingMode(.palette)
                    .foregroundStyle(.white, .black.opacity(0.65))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("移除图片")
            .offset(x: 5, y: -5)
        }
        .padding(.top, 5).padding(.trailing, 5)
    }
}

struct MessageMediaGallery: View {
    let media: [MessageMedia]
    @ObservedObject var model: ChatModel

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 135, maximum: 280), spacing: 8)], spacing: 8) {
            ForEach(media.filter { $0.kind == "image" }) { item in
                MessageMediaTile(media: item, model: model)
            }
        }
    }
}

private struct MessageMediaTile: View {
    let media: MessageMedia
    @ObservedObject var model: ChatModel
    @State private var data: Data?
    @State private var failed = false
    @State private var reload = 0

    var body: some View {
        Group {
            if let data {
                platformImage(data)
                    .aspectRatio(contentMode: .fit)
                    .frame(maxWidth: 280, maxHeight: 280)
            } else if failed {
                Button { reload += 1 } label: {
                    VStack(spacing: 7) {
                        Image(systemName: "arrow.clockwise")
                        Text("重新加载图片").font(.caption)
                    }
                    .frame(maxWidth: .infinity, minHeight: 110)
                }
                .buttonStyle(.plain)
            } else {
                ProgressView().frame(maxWidth: .infinity, minHeight: 110)
            }
        }
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .accessibilityLabel(media.altText ?? media.filename ?? "聊天图片")
        .task(id: reload) { await load() }
    }

    private func load() async {
        failed = false
        guard let serverID = model.connection?.pairing.serverID else {
            failed = true
            return
        }
        let key = MediaImageCache.Key(serverID: serverID, mediaID: media.id)
        if let cached = await MediaImageCache.shared.data(for: key) {
            data = cached
            return
        }
        do {
            let loaded = try await model.mediaData(media)
            guard decodePlatformImage(loaded) else { throw ClientError.invalidMediaResponse }
            await MediaImageCache.shared.insert(loaded, for: key)
            data = loaded
        } catch {
            failed = true
        }
    }
}

@ViewBuilder
private func platformImage(_ data: Data) -> some View {
    #if os(macOS)
    if let image = NSImage(data: data) {
        Image(nsImage: image).resizable()
    } else {
        Color.secondary.opacity(0.1).overlay { Image(systemName: "photo.badge.exclamationmark") }
    }
    #elseif os(iOS)
    if let image = UIImage(data: data) {
        Image(uiImage: image).resizable()
    } else {
        Color.secondary.opacity(0.1).overlay { Image(systemName: "photo.badge.exclamationmark") }
    }
    #endif
}

private func decodePlatformImage(_ data: Data) -> Bool {
    #if os(macOS)
    NSImage(data: data) != nil
    #elseif os(iOS)
    UIImage(data: data) != nil
    #endif
}
