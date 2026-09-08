// swift-tools-version: 6.0
import PackageDescription
let package = Package(
    name: "TalosMac",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "TalosApp", targets: ["TalosApp"])],
    dependencies: [.package(url: "https://github.com/migueldeicaza/SwiftTerm", exact: "1.19.0")],
    targets: [
        .executableTarget(name: "TalosApp", dependencies: ["SwiftTerm"],
                          swiftSettings: [.swiftLanguageMode(.v5)])
    ]
)
