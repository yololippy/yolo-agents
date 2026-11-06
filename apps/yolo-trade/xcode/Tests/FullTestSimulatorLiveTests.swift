import Foundation
import Testing
import YoloTradeClient

private let simulatorProbeEnvironmentIsConfigured = {
    let environment = ProcessInfo.processInfo.environment
    return environment["YOLO_TRADE_SIMULATOR_PROBE_ENDPOINT"]?.isEmpty == false &&
        environment["YOLO_TRADE_SIMULATOR_PAIRING_CODE"]?.isEmpty == false &&
        environment["YOLO_TRADE_SIMULATOR_PROBE_GRANT"]?.isEmpty == false &&
        environment["YOLO_TRADE_SIMULATOR_ALLOW_INSECURE_LOCAL_HTTP"]?.isEmpty == false
}()

/// Opt-in production acceptance. The Mac administrator creates a five-minute,
/// single-use session; this test pairs and revokes only its temporary iOS identity.
@Test(.enabled(if: simulatorProbeEnvironmentIsConfigured,
               "The full-test runner supplies a short-lived Simulator probe session."))
func yoloTradeSimulatorProductionModelRoundTrip() async throws {
    let environment = ProcessInfo.processInfo.environment
    guard let endpoint = environment["YOLO_TRADE_SIMULATOR_PROBE_ENDPOINT"],
          let pairingCode = environment["YOLO_TRADE_SIMULATOR_PAIRING_CODE"],
          let grant = environment["YOLO_TRADE_SIMULATOR_PROBE_GRANT"],
          let insecure = environment["YOLO_TRADE_SIMULATOR_ALLOW_INSECURE_LOCAL_HTTP"] else { return }
    let result = try await YoloTradeFullTestAcceptance.runSimulator(session: .init(
        endpoint: endpoint,
        pairingCode: pairingCode,
        grant: grant,
        expiresIn: 300,
        allowInsecureLocalHTTP: insecure == "1"
    ))
    #expect(result.status == "passed")
    #expect(result.agentID == "yolo-trade")
    #expect(result.iosClientOnline)
    #expect(result.modelCatalogAvailable)
    #expect(result.modelRoundtripPassed)
    #expect(!result.businessToolsEnabled)
    #expect(!result.conversationSaved)
    #expect(!result.model.isEmpty)
}
