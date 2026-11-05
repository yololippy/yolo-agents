import Foundation

/// Supplied by an app's private entry point. The shared layer owns no Agent branding.
public struct AgentAppIdentity: Equatable, Sendable {
    public let agentID: String
    public let appName: String
    public let keychainService: String
    public let defaultServiceAddress: String

    public init(agentID: String, appName: String, keychainService: String,
                defaultServiceAddress: String) {
        self.agentID = agentID; self.appName = appName; self.keychainService = keychainService
        self.defaultServiceAddress = defaultServiceAddress
    }
    public func validate(_ health: Health, serverID: UUID? = nil) throws {
        guard health.apiVersion == 1, serverID == nil || health.serverID == serverID else { throw ClientError.incompatibleServer }
        guard let id = health.agentID else { throw ClientError.agentIdentityUnavailable }
        guard id == agentID else { throw ClientError.agentIdentityMismatch }
    }
    public func validate(_ connection: PairedConnection) throws {
        guard let id = connection.agentID else { throw ClientError.agentIdentityUnavailable }
        guard id == agentID else { throw ClientError.agentIdentityMismatch }
    }
}
