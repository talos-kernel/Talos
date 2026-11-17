import XCTest
@testable import TalosApp

final class MessageTextTests: XCTestCase {
    func testCodeStaysLiteralAndHeadingsAreReadable() {
        let blocks = MessageBlock.parse("## Changes\n**Fixed** the issue\n```sh\necho '<script>'\n```\nDone")
        XCTAssertEqual(blocks.map(\.kind), [.heading, .prose, .code, .prose])
        XCTAssertEqual(blocks[0].text, "Changes")
        XCTAssertEqual(blocks[2].text, "echo '<script>'")
    }

    func testUnclosedCodeFencePreservesTheAnswer() {
        let blocks = MessageBlock.parse("```python\nprint('hello')")
        XCTAssertEqual(blocks.last?.text, "print('hello')")
        XCTAssertEqual(blocks.last?.kind, .code)
    }
}
