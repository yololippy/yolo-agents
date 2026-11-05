// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "YoloTradeClient",
    platforms: [.macOS(.v14), .iOS("26.0")],
    products: [.library(name: "YoloTradeClient", targets: ["YoloTradeClient"])],
    dependencies: [
        .package(path: "../../../packages/sdk-swift"),
        .package(path: "../../../packages/ui-swift"),
    ],
    targets: [
        .target(name: "YoloTradeClient", dependencies: [
            .product(name: "YoloAgentsSDK", package: "sdk-swift"),
            .product(name: "YoloAgentsUI", package: "ui-swift"),
        ]),
        .testTarget(name: "YoloTradeClientTests", dependencies: ["YoloTradeClient"]),
    ]
)
