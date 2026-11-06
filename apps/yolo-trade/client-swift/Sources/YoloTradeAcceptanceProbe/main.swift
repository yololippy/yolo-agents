import Darwin
import Foundation
import YoloAgentsSDK
import YoloTradeClient

@main
struct YoloTradeAcceptanceProbe {
    static func main() async {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            let arguments = Array(CommandLine.arguments.dropFirst())
            if arguments.count == 3, arguments[0] == "--simulator-session",
               arguments[1] == "--endpoint" {
                let endpoint = try ServiceEndpoint(arguments[2], allowInsecureLocalHTTP: true)
                let session = try await YoloTradeFullTestAcceptance.issueStoredSimulatorSession(
                    endpointOverride: endpoint
                )
                print(String(decoding: try encoder.encode(session), as: UTF8.self))
            } else if arguments.count == 2, arguments[0] == "--endpoint" {
                let endpoint = try ServiceEndpoint(arguments[1], allowInsecureLocalHTTP: true)
                let result = try await YoloTradeFullTestAcceptance.runStored(
                    endpointOverride: endpoint
                )
                print(String(decoding: try encoder.encode(result), as: UTF8.self))
            } else if arguments.isEmpty {
                let result = try await YoloTradeFullTestAcceptance.runStored()
                print(String(decoding: try encoder.encode(result), as: UTF8.self))
            } else {
                throw ProbeArgumentError()
            }
        } catch let error as ClientError {
            let detail: String
            if case .http(let status, let code, _) = error {
                detail = "HTTP \(status) \(code ?? "unknown")"
            } else {
                detail = "client_error"
            }
            FileHandle.standardError.write(Data("YoloTrade 验收探针未通过：\(detail)。\n".utf8))
            exit(1)
        } catch {
            FileHandle.standardError.write(Data("YoloTrade 验收探针未通过：acceptance_error。\n".utf8))
            exit(1)
        }
    }
}

private struct ProbeArgumentError: Error {}
