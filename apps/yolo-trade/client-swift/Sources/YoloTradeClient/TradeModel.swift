import Foundation
import SwiftUI
import YoloAgentsSDK

@MainActor public final class TradeModel: ObservableObject {
    @Published public private(set) var positionsAsOf: Date?
    @Published public private(set) var positions: [TradePosition] = []
    @Published public private(set) var results: [TradeInstrument] = []
    @Published public private(set) var selected: TradeInstrument?
    @Published public private(set) var quote: TradeQuote?
    @Published public private(set) var error: String?
    @Published public private(set) var quoteError: String?
    @Published public private(set) var isSearching = false
    private let client: PaperAPIClient
    private let connection: PairedConnection
    private var searchTask: Task<Void, Never>?
    private var searchGeneration = UUID()
    private var selectionGeneration = UUID()
    private let debounce: Duration
    public init(connection: PairedConnection, transport: any HTTPTransport = URLSessionTransport(), debounce: Duration = .milliseconds(300)) {
        self.connection = connection; self.debounce = debounce
        client = PaperAPIClient(endpoint: connection.endpoint, token: connection.pairing.token, transport: transport)
    }
    private func verifyHost() async throws {
        guard connection.agentID == nil || connection.agentID == YoloTradeProduct.agentID else { throw ClientError.agentIdentityMismatch }
        let health = try await client.health()
        guard health.apiVersion == 1, health.serverID == connection.pairing.serverID else { throw ClientError.incompatibleServer }
        guard health.agentID == nil || health.agentID == YoloTradeProduct.agentID else { throw ClientError.agentIdentityMismatch }
    }
    public func refreshPositions() async {
        do {
            try await verifyHost()
            let response = try await client.positions()
            positions = response.positions; positionsAsOf = response.asOf; error = nil
        }
        catch { self.error = error.localizedDescription }
    }
    public func search(_ text: String) {
        searchTask?.cancel(); let generation = UUID(); searchGeneration = generation
        results = []; error = nil
        let query = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { isSearching = false; return }
        isSearching = true
        searchTask = Task { [weak self] in
            guard let self else { return }
            do {
                try await Task.sleep(for: debounce)
                try await verifyHost()
                let values = try await client.search(query)
                guard generation == searchGeneration, !Task.isCancelled else { return }
                results = values; isSearching = false
            } catch {
                guard generation == searchGeneration, !Task.isCancelled else { return }
                self.error = error.localizedDescription; isSearching = false
            }
        }
    }
    public func select(_ instrument: TradeInstrument) async {
        let generation = UUID(); selectionGeneration = generation
        selected = instrument; quote = nil; quoteError = nil
        do {
            try await verifyHost()
            let value = try await client.quote(instrument.conID)
            guard generation == selectionGeneration else { return }
            quote = value
        } catch { if generation == selectionGeneration { quoteError = error.localizedDescription } }
    }
}
