// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "YoloAgentsUI",
    platforms: [.macOS(.v14), .iOS("26.0")],
    products: [
        .library(name: "YoloAgentsUI", targets: ["YoloAgentsUI"]),
    ],
    dependencies: [
        .package(path: "../sdk-swift"),
        // v0.7.0 depends on revision-pinned highlighting/math packages, so
        // SwiftPM requires pinning this package by the tag's commit as well.
        .package(
            url: "https://github.com/microsoft/SwiftStreamingMarkdown",
            revision: "5f7c04e0558df6146f90d482edb62cb456986bda"
        ),
    ],
    targets: [
        .target(
            name: "YoloAgentsUI",
            dependencies: [
                .product(name: "YoloAgentsSDK", package: "sdk-swift"),
                .product(name: "SwiftStreamingMarkdown", package: "SwiftStreamingMarkdown"),
            ]
        ),
        .testTarget(name: "YoloAgentsUITests", dependencies: [
            .target(name: "YoloAgentsUI"),
            .product(name: "YoloAgentsSDK", package: "sdk-swift"),
        ]),
    ]
)
