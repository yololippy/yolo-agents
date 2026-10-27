import Foundation

public struct SSEFrame: Equatable, Sendable {
    public let id: String?
    public let event: String
    public let data: String
}

/// Incremental UTF-8 framing. A partial frame at EOF is deliberately not dispatched;
/// reconnecting from the last committed event recovers it without inventing a cursor.
public struct SSEParser: Sendable {
    private var line = Data()
    private var dataLines: [String] = []
    private var event = ""
    private var lastID: String?
    private var skipLF = false
    private var firstLine = true
    private var frameBytes = 0
    private let maximumBytes: Int
    public init(maximumBytes: Int = 1_048_576) { self.maximumBytes = maximumBytes }

    public mutating func append(_ chunk: Data) throws -> [SSEFrame] {
        var frames: [SSEFrame] = []
        for byte in chunk {
            if skipLF {
                skipLF = false
                if byte == 10 { continue }
            }
            if byte == 10 || byte == 13 {
                if let frame = try finishLine() { frames.append(frame) }
                skipLF = byte == 13
            } else {
                line.append(byte)
                guard line.count <= maximumBytes else { throw ClientError.invalidEvent("SSE line too large") }
            }
        }
        return frames
    }
    private mutating func finishLine() throws -> SSEFrame? {
        guard var text = String(data: line, encoding: .utf8) else { throw ClientError.invalidEvent("SSE UTF-8") }
        line.removeAll(keepingCapacity: true)
        if firstLine { firstLine = false; if text.hasPrefix("\u{feff}") { text.removeFirst() } }
        if text.isEmpty {
            defer { dataLines.removeAll(keepingCapacity: true); event = ""; frameBytes = 0 }
            guard !dataLines.isEmpty else { return nil }
            return SSEFrame(id: lastID, event: event, data: dataLines.joined(separator: "\n"))
        }
        if text.hasPrefix(":") { return nil }
        let pieces = text.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        let field = String(pieces[0])
        var value = pieces.count == 2 ? String(pieces[1]) : ""
        if value.hasPrefix(" ") { value.removeFirst() }
        switch field {
        case "data":
            frameBytes += value.utf8.count + 1
            guard frameBytes <= maximumBytes else { throw ClientError.invalidEvent("SSE frame too large") }
            dataLines.append(value)
        case "id": if !value.contains("\0") { lastID = value }
        case "event": event = value
        default: break // retry hints and extension fields do not represent business events.
        }
        return nil
    }
}

public struct SSEConnection: Sendable {
    public let status: Int
    public let contentType: String?
    public let chunks: AsyncThrowingStream<Data, Error>
    public let cancel: @Sendable () -> Void
    public init(status: Int, contentType: String?, chunks: AsyncThrowingStream<Data, Error>,
                cancel: @escaping @Sendable () -> Void = {}) {
        self.status = status; self.contentType = contentType; self.chunks = chunks; self.cancel = cancel
    }
}

public protocol SSETransport: Sendable {
    func open(_ request: URLRequest) async throws -> SSEConnection
}

public struct ServerEventStream: AsyncSequence, Sendable {
    public typealias Element = ServerEvent
    public typealias AsyncIterator = AsyncThrowingStream<ServerEvent, Error>.Iterator
    private let events: AsyncThrowingStream<ServerEvent, Error>
    private let close: @Sendable () -> Void
    init(events: AsyncThrowingStream<ServerEvent, Error>, close: @escaping @Sendable () -> Void) {
        self.events = events; self.close = close
    }
    public func makeAsyncIterator() -> AsyncIterator { events.makeAsyncIterator() }
    public func cancel() { close() }
}

public struct URLSessionSSETransport: SSETransport {
    public init() {}
    public func open(_ request: URLRequest) async throws -> SSEConnection {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.urlCredentialStorage = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 3_600
        let session = URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
        let bytes: URLSession.AsyncBytes
        let response: URLResponse
        do {
            (bytes, response) = try await withTaskCancellationHandler {
                try await session.bytes(for: request)
            } onCancel: { session.invalidateAndCancel() }
        } catch { session.invalidateAndCancel(); throw error }
        guard let response = response as? HTTPURLResponse else {
            session.invalidateAndCancel(); throw ClientError.nonHTTPResponse
        }
        let chunks = AsyncThrowingStream<Data, Error>(bufferingPolicy: .bufferingOldest(256)) { continuation in
            let producer = Task {
                defer { session.invalidateAndCancel() }
                do {
                    var chunk = Data()
                    for try await byte in bytes {
                        try Task.checkCancellation()
                        chunk.append(byte)
                        if byte == 10 || byte == 13 || chunk.count >= 4_096 {
                            switch continuation.yield(chunk) {
                            case .enqueued: chunk.removeAll(keepingCapacity: true)
                            case .dropped: throw ClientError.streamOverflow
                            case .terminated: return
                            @unknown default: throw ClientError.streamOverflow
                            }
                        }
                    }
                    if !chunk.isEmpty {
                        if case .dropped = continuation.yield(chunk) { throw ClientError.streamOverflow }
                    }
                    continuation.finish()
                } catch { continuation.finish(throwing: error) }
            }
            continuation.onTermination = { @Sendable _ in producer.cancel(); session.invalidateAndCancel() }
        }
        return SSEConnection(status: response.statusCode, contentType: response.value(forHTTPHeaderField: "Content-Type"),
                             chunks: chunks, cancel: { session.invalidateAndCancel() })
    }
}

extension APIClient {
    /// Opens authenticated SSE from the caller's last committed global cursor. This
    /// consumes bytes as they arrive, without waiting for the HTTP response to finish.
    public func eventStream(after cursor: Int64) async throws -> ServerEventStream {
        guard cursor >= 0 else { throw ClientError.invalidEventPage }
        var request = try makeRequest("GET", path: "events", query: [
            URLQueryItem(name: "after", value: String(cursor)),
            URLQueryItem(name: "stream", value: "true"),
        ])
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue(String(cursor), forHTTPHeaderField: "Last-Event-ID")
        let connection = try await streamTransport.open(request)
        do { try Task.checkCancellation() } catch { connection.cancel(); throw error }
        guard (200...299).contains(connection.status) else {
            defer { connection.cancel() }
            var body = Data()
            for try await chunk in connection.chunks {
                body.append(chunk)
                if body.count > 65_536 { break }
            }
            throw Self.responseError(status: connection.status, data: body)
        }
        guard connection.contentType?.split(separator: ";", maxSplits: 1).first?
            .trimmingCharacters(in: .whitespaces).lowercased() == "text/event-stream" else {
            connection.cancel(); throw ClientError.invalidEvent("SSE Content-Type")
        }
        let events = AsyncThrowingStream<ServerEvent, Error>(bufferingPolicy: .bufferingOldest(256)) { continuation in
            let producer = Task {
                defer { connection.cancel() }
                do {
                    var parser = SSEParser()
                    for try await chunk in connection.chunks {
                        try Task.checkCancellation()
                        for frame in try parser.append(chunk) {
                            let event = try WireJSON.decoder().decode(ServerEvent.self, from: Data(frame.data.utf8))
                            guard let id = frame.id, Int64(id) == event.seq, event.seq > 0,
                                  frame.event.isEmpty || frame.event == event.type else {
                                throw ClientError.invalidEvent("SSE envelope")
                            }
                            switch continuation.yield(event) {
                            case .enqueued: break
                            case .dropped: throw ClientError.streamOverflow
                            case .terminated: return
                            @unknown default: throw ClientError.streamOverflow
                            }
                        }
                    }
                    continuation.finish()
                } catch { continuation.finish(throwing: error) }
            }
            continuation.onTermination = { @Sendable _ in producer.cancel(); connection.cancel() }
        }
        return ServerEventStream(events: events, close: connection.cancel)
    }
}
