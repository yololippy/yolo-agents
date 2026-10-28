// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "TradingAgentClient",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [.library(name: "TradingAgentClient", targets: ["TradingAgentClient"])],
    dependencies: [
        .package(path: "../../../packages/sdk-swift"),
        .package(path: "../../../packages/ui-swift"),
    ],
    targets: [
        .target(name: "TradingAgentClient", dependencies: [
            .product(name: "YoloAgentsSDK", package: "sdk-swift"),
            .product(name: "YoloAgentsUI", package: "ui-swift"),
        ]),
        .testTarget(name: "TradingAgentClientTests", dependencies: ["TradingAgentClient"]),
    ]
)
