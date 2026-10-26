// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "YoloAgentsSDK",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [
        .library(name: "YoloAgentsSDK", targets: ["YoloAgentsSDK"]),
        .executable(name: "YoloAgentsProbe", targets: ["YoloAgentsProbe"]),
    ],
    targets: [
        .target(name: "YoloAgentsSDK"),
        .executableTarget(name: "YoloAgentsProbe", dependencies: ["YoloAgentsSDK"]),
        .testTarget(name: "YoloAgentsSDKTests", dependencies: ["YoloAgentsSDK"]),
    ]
)
