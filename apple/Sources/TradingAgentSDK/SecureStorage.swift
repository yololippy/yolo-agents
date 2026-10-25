import Foundation
import Security

public protocol SecureStore: Sendable {
    func read(_ key: String) throws -> Data?
    func write(_ data: Data, for key: String) throws
    func remove(_ key: String) throws
}

public struct KeychainFailure: Error, LocalizedError, Sendable {
    public let status: OSStatus
    public var errorDescription: String? { "无法访问本机安全存储（\(status)），请解锁设备后重试。" }
}

public struct KeychainStore: SecureStore {
    private let service: String
    public init(service: String = "com.tradingagent.apple.v1") { self.service = service }
    private func query(_ key: String) -> [CFString: Any] {
        [kSecClass: kSecClassGenericPassword, kSecAttrService: service, kSecAttrAccount: key,
         kSecAttrSynchronizable: false]
    }
    public func read(_ key: String) throws -> Data? {
        var query = query(key)
        query[kSecReturnData] = true
        query[kSecMatchLimit] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw KeychainFailure(status: status) }
        return data
    }
    public func write(_ data: Data, for key: String) throws {
        let attributes: [CFString: Any] = [kSecValueData: data,
            kSecAttrAccessible: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        let status = SecItemUpdate(query(key) as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var item = query(key)
            attributes.forEach { item[$0.key] = $0.value }
            let added = SecItemAdd(item as CFDictionary, nil)
            guard added == errSecSuccess else { throw KeychainFailure(status: added) }
        } else if status != errSecSuccess { throw KeychainFailure(status: status) }
    }
    public func remove(_ key: String) throws {
        let status = SecItemDelete(query(key) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw KeychainFailure(status: status) }
    }
}

public struct PairedConnection: Codable, Equatable, Sendable {
    public let endpoint: ServiceEndpoint
    public let pairing: PairingResponse
    public init(endpoint: ServiceEndpoint, pairing: PairingResponse) {
        self.endpoint = endpoint; self.pairing = pairing
    }
    public var cacheKey: String { "cache.\(pairing.serverID.uuidString).\(pairing.device.id.uuidString)" }
}

public struct ClientRepository: Sendable {
    private let store: any SecureStore
    public init(store: any SecureStore = KeychainStore()) { self.store = store }
    public func connection() throws -> PairedConnection? {
        try store.read("connection").map { try WireJSON.decoder().decode(PairedConnection.self, from: $0) }
    }
    public func saveConnection(_ connection: PairedConnection) throws {
        try store.write(WireJSON.encoder().encode(connection), for: "connection")
    }
    public func state(for connection: PairedConnection) throws -> ClientState {
        guard let data = try store.read(connection.cacheKey) else { return ClientState() }
        var state = try WireJSON.decoder().decode(ClientState.self, from: data)
        state.prepareForLaunch()
        return state
    }
    public func saveState(_ state: ClientState, for connection: PairedConnection) throws {
        try store.write(WireJSON.encoder().encode(state.cacheSnapshot()), for: connection.cacheKey)
    }
    public func forget(_ connection: PairedConnection) throws {
        try store.remove("connection")
        try store.remove(connection.cacheKey)
    }
}
