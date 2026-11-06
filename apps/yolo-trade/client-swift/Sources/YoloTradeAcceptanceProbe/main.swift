import Darwin
import Foundation
import YoloTradeClient

@main
struct YoloTradeAcceptanceProbe {
    static func main() async {
        do {
            let result = try await YoloTradeFullTestAcceptance.runStored()
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            print(String(decoding: try encoder.encode(result), as: UTF8.self))
        } catch {
            FileHandle.standardError.write(Data("YoloTrade 验收探针未通过。\n".utf8))
            exit(1)
        }
    }
}
