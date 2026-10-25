// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "TradingAgentApple",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [
        .library(name: "TradingAgentSDK", targets: ["TradingAgentSDK"]),
        .library(name: "TradingAgentUI", targets: ["TradingAgentUI"]),
        .executable(name: "TradingAgentMac", targets: ["TradingAgentMac"]),
        .executable(name: "TradingAgentProbe", targets: ["TradingAgentProbe"]),
    ],
    targets: [
        .target(name: "TradingAgentSDK"),
        .target(name: "TradingAgentUI", dependencies: ["TradingAgentSDK"]),
        .executableTarget(name: "TradingAgentMac", dependencies: ["TradingAgentUI"]),
        .executableTarget(name: "TradingAgentProbe", dependencies: ["TradingAgentSDK"]),
        .testTarget(name: "TradingAgentSDKTests", dependencies: ["TradingAgentSDK"]),
    ]
)
