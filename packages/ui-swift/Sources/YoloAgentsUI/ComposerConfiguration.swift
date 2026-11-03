import Foundation

/// Agent-neutral copy for the shared chat composer. Agent apps can customize
/// wording without forking input behavior, keyboard handling, or visual state.
public struct AgentComposerConfiguration: Equatable, Sendable {
    public let placeholder: String
    public let offlinePlaceholder: String
    public let archivedPlaceholder: String

    public init(placeholder: String = "输入消息…",
                offlinePlaceholder: String = "离线草稿，连接恢复后可发送",
                archivedPlaceholder: String = "此会话已归档") {
        self.placeholder = placeholder
        self.offlinePlaceholder = offlinePlaceholder
        self.archivedPlaceholder = archivedPlaceholder
    }

    public static let standard = AgentComposerConfiguration()
}
