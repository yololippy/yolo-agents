// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "YoloBeniClient",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [.library(name: "YoloBeniClient", targets: ["YoloBeniClient"])],
    dependencies: [
        .package(path: "../../../packages/sdk-swift"),
        .package(path: "../../../packages/ui-swift"),
    ],
    targets: [
        .target(name: "YoloBeniClient", dependencies: [
            .product(name: "YoloAgentsSDK", package: "sdk-swift"),
            .product(name: "YoloAgentsUI", package: "ui-swift"),
        ]),
        .testTarget(name: "YoloBeniClientTests", dependencies: ["YoloBeniClient"]),
    ]
)
