import Foundation
import ImageIO
import UniformTypeIdentifiers
import YoloAgentsSDK

public enum DraftImageError: Error, LocalizedError, Equatable, Sendable {
    case unreadable
    case tooLarge
    case messageTooLarge

    public var errorDescription: String? {
        switch self {
        case .unreadable: "无法读取这张图片，请选择 JPEG、PNG、WebP 或 HEIC 图片。"
        case .tooLarge: "图片处理后仍超过 10 MiB，请选择尺寸较小的图片。"
        case .messageTooLarge: "所选图片合计不能超过 20 MiB。"
        }
    }
}

/// A device-local draft. Its bytes are uploaded before the message intent is
/// persisted; only server media IDs enter the durable outgoing queue.
public struct DraftImage: Identifiable, Equatable, Sendable {
    public let id: UUID
    public let data: Data
    public let mediaType: String
    public let filename: String

    public init(id: UUID = UUID(), data: Data, mediaType: String, filename: String) {
        self.id = id; self.data = data; self.mediaType = mediaType; self.filename = filename
    }

    static func preparing(data: Data, sourceMediaType: String?) throws -> DraftImage {
        guard !data.isEmpty,
              let source = CGImageSourceCreateWithData(data as CFData, nil),
              CGImageSourceGetCount(source) > 0,
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int,
              width > 0, height > 0 else { throw DraftImageError.unreadable }

        let detectedType = CGImageSourceGetType(source).flatMap { UTType($0 as String)?.preferredMIMEType }
        let normalizedType = (detectedType ?? sourceMediaType)?.lowercased()
        let withinPixels = width <= MediaLimits.maximumDimension &&
            height <= MediaLimits.maximumDimension &&
            Double(width) * Double(height) <= Double(MediaLimits.maximumPixelCount)
        if let normalizedType, MediaLimits.allowedImageTypes.contains(normalizedType),
           withinPixels, data.count <= MediaLimits.maximumBytesPerItem {
            return DraftImage(data: data, mediaType: normalizedType,
                              filename: Self.filename(for: normalizedType))
        }

        let scale = min(
            1,
            Double(MediaLimits.maximumDimension) / Double(width),
            Double(MediaLimits.maximumDimension) / Double(height),
            sqrt(Double(MediaLimits.maximumPixelCount) / (Double(width) * Double(height)))
        )
        var maximumPixelSize = max(1, Int(floor(Double(max(width, height)) * scale)))
        for _ in 0..<4 {
            let options: [CFString: Any] = [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceShouldCacheImmediately: true,
                kCGImageSourceThumbnailMaxPixelSize: maximumPixelSize,
            ]
            guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
                throw DraftImageError.unreadable
            }
            for quality in [0.90, 0.78, 0.64, 0.50, 0.38] {
                if let encoded = Self.jpegData(image, quality: quality),
                   encoded.count <= MediaLimits.maximumBytesPerItem {
                    return DraftImage(data: encoded, mediaType: "image/jpeg", filename: "image.jpg")
                }
            }
            maximumPixelSize = max(1, Int(Double(maximumPixelSize) * 0.78))
        }
        throw DraftImageError.tooLarge
    }

    private static func jpegData(_ image: CGImage, quality: Double) -> Data? {
        let output = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(
            output, UTType.jpeg.identifier as CFString, 1, nil
        ) else { return nil }
        CGImageDestinationAddImage(destination, image, [
            kCGImageDestinationLossyCompressionQuality: quality,
        ] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { return nil }
        return output as Data
    }

    private static func filename(for mediaType: String) -> String {
        switch mediaType {
        case "image/png": "image.png"
        case "image/webp": "image.webp"
        default: "image.jpg"
        }
    }
}
