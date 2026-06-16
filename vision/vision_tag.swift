// vision_tag.swift
// On-device image tagging using Apple's Vision framework.
// Runs on the Neural Engine (great on M-series). Reads newline-delimited
// image file paths from stdin and prints one JSON object per line:
//
//   {"path":"...","ok":true,"labels":[{"id":"beach","conf":0.91}],
//    "hasText":true,"text":"HELLO"}
//
// Build:
//   swiftc -O vision_tag.swift -o vision_tag
// Use:
//   echo /path/to/img.heic | ./vision_tag
//
// Notes:
// - Decodes via ImageIO (CGImageSource) so HEIC/JPEG/PNG/TIFF and many RAW
//   formats supported by macOS work.
// - Classification = VNClassifyImageRequest (scene/object taxonomy).
// - OCR = VNRecognizeTextRequest (fast) to flag screenshots/documents/receipts.

import Foundation
import Vision
import ImageIO
import CoreGraphics

let minConf: Float = (ProcessInfo.processInfo.environment["VISION_MIN_CONF"]
    as NSString?)?.floatValue ?? 0.15
let topN = Int(ProcessInfo.processInfo.environment["VISION_TOP_N"] ?? "5") ?? 5

func jsonEscape(_ s: String) -> String {
    var out = ""
    for c in s {
        switch c {
        case "\"": out += "\\\""
        case "\\": out += "\\\\"
        case "\n": out += "\\n"
        case "\r": out += "\\r"
        case "\t": out += "\\t"
        default: out.append(c)
        }
    }
    return out
}

func loadCGImage(_ path: String) -> CGImage? {
    let url = URL(fileURLWithPath: path)
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
    // Use a thumbnail for speed; full decode not needed for classification.
    let opts: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: 1024,
    ]
    if let thumb = CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary) {
        return thumb
    }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}

func process(_ path: String) -> String {
    guard let cg = loadCGImage(path) else {
        return "{\"path\":\"\(jsonEscape(path))\",\"ok\":false,\"error\":\"decode_failed\"}"
    }
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])

    // Classification
    var labels: [(String, Float)] = []
    let classify = VNClassifyImageRequest()
    // OCR
    let ocr = VNRecognizeTextRequest()
    ocr.recognitionLevel = .fast
    ocr.usesLanguageCorrection = false

    do {
        try handler.perform([classify, ocr])
    } catch {
        return "{\"path\":\"\(jsonEscape(path))\",\"ok\":false,\"error\":\"vision_failed\"}"
    }

    if let obs = classify.results {
        for o in obs where o.confidence >= minConf {
            labels.append((o.identifier, o.confidence))
            if labels.count >= topN { break }
        }
    }

    var textPieces: [String] = []
    if let tobs = ocr.results {
        for t in tobs.prefix(8) {
            if let top = t.topCandidates(1).first, top.string.count >= 2 {
                textPieces.append(top.string)
            }
        }
    }
    let hasText = !textPieces.isEmpty
    let textSample = textPieces.prefix(5).joined(separator: " | ")

    var labelJson = "["
    labelJson += labels.map {
        "{\"id\":\"\(jsonEscape($0.0))\",\"conf\":\(String(format: "%.3f", $0.1))}"
    }.joined(separator: ",")
    labelJson += "]"

    return "{\"path\":\"\(jsonEscape(path))\",\"ok\":true,\"labels\":\(labelJson)," +
           "\"hasText\":\(hasText),\"text\":\"\(jsonEscape(textSample))\"}"
}

// Read paths from stdin, one per line.
while let line = readLine(strippingNewline: true) {
    let path = line.trimmingCharacters(in: .whitespaces)
    if path.isEmpty { continue }
    print(process(path))
    fflush(stdout)
}
