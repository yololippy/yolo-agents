// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "BAgentClient",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [.library(name: "BAgentClient", targets: ["BAgentClient"])],
    dependencies: [
        .package(path: "../../../packages/sdk-swift"),
        .package(path: "../../../packages/ui-swift"),
    ],
    targets: [.target(name: "BAgentClient", dependencies: [
        .product(name: "YoloAgentsSDK", package: "sdk-swift"),
        .product(name: "YoloAgentsUI", package: "ui-swift"),
    ])]
)
