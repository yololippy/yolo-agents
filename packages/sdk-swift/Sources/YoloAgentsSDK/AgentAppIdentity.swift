import Foundation

/// Supplied by an app's private entry point. The shared layer owns no Agent branding.
public struct AgentAppIdentity: Equatable, Sendable {
    public let agentID: String
    public let appName: String
    public let keychainService: String
    public let defaultServiceAddress: String
    public let allowsLegacyServer: Bool

    public init(agentID: String, appName: String, keychainService: String,
                defaultServiceAddress: String, allowsLegacyServer: Bool = false) {
        self.agentID = agentID; self.appName = appName; self.keychainService = keychainService
        self.defaultServiceAddress = defaultServiceAddress; self.allowsLegacyServer = allowsLegacyServer
    }
    public func validate(_ health: Health, serverID: UUID? = nil) throws {
        guard health.apiVersion == 1, serverID == nil || health.serverID == serverID else { throw ClientError.incompatibleServer }
        if let id = health.agentID {
            guard id == agentID else { throw ClientError.agentIdentityMismatch }
        } else if !allowsLegacyServer { throw ClientError.agentIdentityUnavailable }
    }
    public func validate(_ connection: PairedConnection) throws {
        if let id = connection.agentID {
            guard id == agentID else { throw ClientError.agentIdentityMismatch }
        } else if !allowsLegacyServer { throw ClientError.agentIdentityUnavailable }
    }
}
