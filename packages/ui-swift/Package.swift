// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "YoloAgentsUI",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [
        .library(name: "YoloAgentsUI", targets: ["YoloAgentsUI"]),
    ],
    dependencies: [
        .package(path: "../sdk-swift"),
    ],
    targets: [
        .target(
            name: "YoloAgentsUI",
            dependencies: [
                .product(name: "YoloAgentsSDK", package: "sdk-swift"),
            ]
        ),
        .testTarget(name: "YoloAgentsUITests", dependencies: [
            .target(name: "YoloAgentsUI"),
            .product(name: "YoloAgentsSDK", package: "sdk-swift"),
        ]),
    ]
)
