import AppKit
import Foundation
import Vision

guard CommandLine.arguments.count == 2,
      let image = NSImage(contentsOfFile: CommandLine.arguments[1]) else {
    fputs("usage: vision_ocr.swift image.png\n", stderr)
    exit(2)
}
var rectangle = NSRect(origin: .zero, size: image.size)
guard let cgImage = image.cgImage(forProposedRect: &rectangle, context: nil, hints: nil) else {
    fputs("image has no CGImage\n", stderr)
    exit(2)
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US"]
let handler = VNImageRequestHandler(cgImage: cgImage)
try handler.perform([request])
for observation in request.results ?? [] {
    if let text = observation.topCandidates(1).first?.string {
        print(text)
    }
}
