// swift-tools-version:5.9
import PackageDescription

// SonarApp — native macOS menu-bar app for Sonar.
//
// Hermetic/offline: the only dependencies are system frameworks (AppKit,
// WebKit, Foundation, CoreServices), so `swift build -c release` needs no
// network and emits .build/release/SonarApp. app/build-app.sh wraps that
// binary into app/build/Sonar.app.
let package = Package(
    name: "SonarApp",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "SonarApp", targets: ["SonarApp"])
    ],
    targets: [
        .executableTarget(
            name: "SonarApp",
            path: "Sources/SonarApp"
        )
    ]
)
