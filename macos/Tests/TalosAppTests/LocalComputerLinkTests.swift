import Darwin
import Foundation
import XCTest
@testable import TalosApp

final class LocalComputerLinkTests: XCTestCase {
    func testLocalOmarchyReplacesRemoteComputerControls() throws {
        let local = try XCTUnwrap(URL(string: "http://127.0.0.1:8830/#token=abcdefghijklmnopqrstuvwxyz012345"))
        XCTAssertEqual(ComputerViewBackend.select(localURL: local), .omarchy(local))
        XCTAssertEqual(ComputerViewBackend.select(localURL: nil), .remote)
    }

    func testAcceptsOnlyTheFixedLoopbackWorkbench() {
        XCTAssertNotNil(LocalComputerLink.validate("http://127.0.0.1:8830/#token=abcdefghijklmnopqrstuvwxyz012345"))
        XCTAssertNotNil(LocalComputerLink.validate("http://[::1]:8830/#token=abcdefghijklmnopqrstuvwxyz012345"))
        for value in [
            "http://localhost:8830/#token=abcdefghijklmnopqrstuvwxyz012345",
            "http://127.0.0.1:8840/#token=abcdefghijklmnopqrstuvwxyz012345",
            "https://127.0.0.1:8830/#token=abcdefghijklmnopqrstuvwxyz012345",
            "http://127.0.0.1:8830/other#token=abcdefghijklmnopqrstuvwxyz012345",
            "http://127.0.0.1:8830/?query=1#token=abcdefghijklmnopqrstuvwxyz012345",
            "http://127.0.0.1:8830/#token=bad.value"
        ] { XCTAssertNil(LocalComputerLink.validate(value), value) }
    }

    func testLoadsOnlyPrivateOperatorOwnedRegularFile() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("talos-link-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: root) }
        let file = root.appendingPathComponent("computer.url")
        try Data("http://127.0.0.1:8830/#token=abcdefghijklmnopqrstuvwxyz012345\n".utf8).write(to: file)
        XCTAssertEqual(chmod(file.path, 0o600), 0)
        XCTAssertNotNil(LocalComputerLink.load(from: file))
        XCTAssertEqual(chmod(file.path, 0o644), 0)
        XCTAssertNil(LocalComputerLink.load(from: file))
    }
}
