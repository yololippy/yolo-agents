import Foundation
import Testing
@testable import YoloAgentsSDK

private actor StreamRecorder: SSETransport {
    private let connection: SSEConnection
    private(set) var requests: [URLRequest] = []
    init(_ connection: SSEConnection) { self.connection = connection }
    func open(_ request: URLRequest) async throws -> SSEConnection {
        requests.append(request); return connection
    }
}
private final class StreamFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func set() { lock.withLock { value = true } }
    var isSet: Bool { lock.withLock { value } }
}
private actor ReceivedEvents {
    private(set) var events: [ServerEvent] = []
    func append(_ event: ServerEvent) { events.append(event) }
}
private struct StreamTestTimeout: Error {}
private func waitFor(_ check: @Sendable () async -> Bool) async throws {
    let deadline = ContinuousClock.now.advanced(by: .seconds(2))
    while !(await check()) {
        guard ContinuousClock.now < deadline else { throw StreamTestTimeout() }
        try await Task.sleep(for: .milliseconds(10))
    }
}
private func streamEvent(_ seq: Int64, text: String = "你好") -> ServerEvent {
    ServerEvent(seq: seq, type: "assistant.delta", conversationID: UUID(),
                payload: EventPayload(runID: UUID(), delta: text), createdAt: Date(timeIntervalSince1970: 1_700_000_000))
}
private func eventFrame(_ event: ServerEvent, id: Int64? = nil, name: String? = nil) throws -> Data {
    let json = String(decoding: try WireJSON.encoder().encode(event), as: UTF8.self)
    return Data("id: \(id ?? event.seq)\nevent: \(name ?? event.type)\ndata: \(json)\n\n".utf8)
}
private func finiteChunks(_ data: Data) -> AsyncThrowingStream<Data, Error> {
    AsyncThrowingStream { continuation in continuation.yield(data); continuation.finish() }
}

@Test func sseParserHandlesByteSplitsUnicodeBOMCommentsAndLineEndings() throws {
    let input = "\u{feff}: heartbeat\r\nretry: 2000\r\nid: 42\r\nevent: assistant.delta\rdata: {\rdata: \"delta\": \"你好🌏\"\rdata: }\r\r"
    var parser = SSEParser()
    var frames: [SSEFrame] = []
    for byte in input.utf8 { frames += try parser.append(Data([byte])) }
    #expect(frames == [SSEFrame(id: "42", event: "assistant.delta", data: "{\n\"delta\": \"你好🌏\"\n}")])
}

@Test func sseParserDoesNotDispatchTruncatedFrameOrHeartbeat() throws {
    var parser = SSEParser()
    #expect(try parser.append(Data(": heartbeat\n\nid: 1\ndata: partial\n".utf8)).isEmpty)
    #expect(try parser.append(Data("\n".utf8)).count == 1)
    #expect(try parser.append(Data("id: 2\ndata: unfinished".utf8)).isEmpty)
}

@Test func sseParserBoundsMemoryAndRejectsInvalidUTF8() {
    var parser = SSEParser(maximumBytes: 16)
    #expect(throws: (any Error).self) { try parser.append(Data(repeating: 65, count: 17)) }
    var other = SSEParser()
    #expect(throws: (any Error).self) { try other.append(Data([0xff, 10])) }
    var multiline = SSEParser(maximumBytes: 20)
    #expect(throws: (any Error).self) { try multiline.append(Data("data: 1234567890\ndata: 1234567890\n\n".utf8)) }
}

@Test func authenticatedSSEDeliversBeforeResponseClosesAndCanCancel() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let cancelled = StreamFlag()
    let transport = StreamRecorder(SSEConnection(status: 200, contentType: "text/event-stream; charset=utf-8", chunks: chunks,
        cancel: { cancelled.set(); continuation.finish() }))
    let client = APIClient(endpoint: try ServiceEndpoint("https://example.com"), token: "fixture-token", streamTransport: transport)
    let stream = try await client.eventStream(after: 41)
    defer { stream.cancel() }
    let received = ReceivedEvents()
    let reader = Task { for try await event in stream { await received.append(event) } }
    defer { reader.cancel() }
    let expected = streamEvent(42)
    let bytes = try eventFrame(expected)
    // The connection remains open: a client that buffers the entire response fails here.
    for byte in bytes { continuation.yield(Data([byte])) }
    try await waitFor { await received.events.count == 1 }
    #expect(await received.events == [expected])
    let request = try #require(await transport.requests.first)
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-token")
    #expect(request.value(forHTTPHeaderField: "Accept") == "text/event-stream")
    #expect(request.value(forHTTPHeaderField: "Last-Event-ID") == "41")
    let url = try #require(request.url)
    let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems
    #expect(query?.first { $0.name == "stream" }?.value == "true")
    #expect(query?.first { $0.name == "after" }?.value == "41")
    reader.cancel()
    stream.cancel()
    _ = try? await reader.value
    #expect(cancelled.isSet)
}

@Test func sseUnauthorizedResponsePreservesServerErrorAndCloses() async throws {
    let cancelled = StreamFlag()
    let error = Data("{\"error\":{\"code\":\"device_revoked\",\"message\":\"设备已撤销\"}}".utf8)
    let transport = StreamRecorder(SSEConnection(status: 401, contentType: "application/json", chunks: finiteChunks(error), cancel: { cancelled.set() }))
    let client = APIClient(endpoint: try ServiceEndpoint("https://example.com"), token: "fixture-token", streamTransport: transport)
    do { _ = try await client.eventStream(after: 0); Issue.record("Revoked stream opened") }
    catch let error as ClientError { #expect(error == .http(status: 401, code: "device_revoked", message: "设备已撤销")) }
    #expect(cancelled.isSet)
}

@Test func sseWithoutTokenDoesNotOpenTransport() async throws {
    let transport = StreamRecorder(SSEConnection(status: 200, contentType: "text/event-stream", chunks: finiteChunks(Data())))
    let client = APIClient(endpoint: try ServiceEndpoint("https://example.com"), streamTransport: transport)
    do { _ = try await client.eventStream(after: 0); Issue.record("Unauthenticated stream opened") }
    catch let error as ClientError { #expect(error.requiresPairing) }
    #expect(await transport.requests.isEmpty)
}

@Test func sseRejectsWrongContentTypeAndEnvelope() async throws {
    let wrong = StreamRecorder(SSEConnection(status: 200, contentType: "application/json", chunks: finiteChunks(Data())))
    let endpoint = try ServiceEndpoint("https://example.com")
    do { _ = try await APIClient(endpoint: endpoint, token: "fixture", streamTransport: wrong).eventStream(after: 0); Issue.record("Wrong MIME accepted") }
    catch let error as ClientError { #expect(error == .invalidEvent("SSE Content-Type")) }
    let transport = StreamRecorder(SSEConnection(status: 200, contentType: "text/event-stream",
        chunks: finiteChunks(try eventFrame(streamEvent(8), id: 9))))
    let stream = try await APIClient(endpoint: endpoint, token: "fixture", streamTransport: transport).eventStream(after: 0)
    defer { stream.cancel() }
    do { for try await _ in stream { Issue.record("Mismatched cursor accepted") } }
    catch let error as ClientError { #expect(error == .invalidEvent("SSE envelope")) }
}

@Test func singleEventReducerAndReconnectReplayAreIdempotent() throws {
    let id = UUID(), run = UUID()
    let first = ServerEvent(seq: 5, type: "assistant.delta", conversationID: id,
        payload: EventPayload(runID: run, delta: "你"), createdAt: Date())
    let second = ServerEvent(seq: 9, type: "assistant.delta", conversationID: id,
        payload: EventPayload(runID: run, delta: "好"), createdAt: Date())
    var state = try ClientState().applying(first)
    state = try state.applying(first)
    state = try state.applying(EventsPage(events: [second], nextCursor: 9, hasMore: false))
    state = try state.applying(second)
    #expect(state.cursor == 9)
    #expect(state.partialReplies.first?.content == "你好")
}
