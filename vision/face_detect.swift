// face_detect.swift — Apple Vision face detection + per-face feature prints.
// Reads image paths on stdin (one per line), emits one JSON object per line:
//   {"path": "...", "faces": [{"bbox":[x,y,w,h], "vec":[...]}], "error": "..."}
// Feature prints come from VNGenerateImageFeaturePrintRequest on each cropped face
// (Apple does not expose a public face-identity embedding; the crop feature print is
// a practical proxy for clustering "same person"). On-device, no network.
import Foundation
import Vision
import AppKit
import CoreImage

func cgImage(_ path: String) -> CGImage? {
    guard let img = NSImage(contentsOfFile: path) else { return nil }
    var rect = NSRect(x: 0, y: 0, width: img.size.width, height: img.size.height)
    return img.cgImage(forProposedRect: &rect, context: nil, hints: nil)
}

func featurePrint(_ cg: CGImage) -> [Float]? {
    let req = VNGenerateImageFeaturePrintRequest()
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([req]) } catch { return nil }
    guard let obs = req.results?.first as? VNFeaturePrintObservation else { return nil }
    let count = obs.elementCount
    var floats = [Float](repeating: 0, count: count)
    let data = obs.data
    floats.withUnsafeMutableBytes { buf in data.copyBytes(to: buf) }
    // L2 normalize for cosine clustering
    let norm = sqrt(floats.reduce(0) { $0 + $1 * $1 })
    if norm > 0 { for i in 0..<floats.count { floats[i] /= norm } }
    return floats
}

func detectFaces(_ cg: CGImage) -> [VNFaceObservation] {
    let req = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([req]) } catch { return [] }
    return (req.results as? [VNFaceObservation]) ?? []
}

func cropFace(_ cg: CGImage, _ bb: CGRect) -> CGImage? {
    let w = CGFloat(cg.width), h = CGFloat(cg.height)
    // Vision bbox is normalized, origin bottom-left → convert to pixel, top-left.
    let pad: CGFloat = 0.15
    var x = (bb.origin.x - bb.width * pad) * w
    var y = (1.0 - bb.origin.y - bb.height * (1 + pad)) * h
    var cw = bb.width * (1 + 2 * pad) * w
    var ch = bb.height * (1 + 2 * pad) * h
    x = max(0, x); y = max(0, y)
    cw = min(cw, w - x); ch = min(ch, h - y)
    if cw < 8 || ch < 8 { return nil }
    return cg.cropping(to: CGRect(x: x, y: y, width: cw, height: ch))
}

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj),
       let s = String(data: d, encoding: .utf8) {
        print(s); fflush(stdout)
    }
}

while let line = readLine(strippingNewline: true) {
    let path = line.trimmingCharacters(in: .whitespaces)
    if path.isEmpty { continue }
    guard let cg = cgImage(path) else {
        emit(["path": path, "faces": [], "error": "decode failed"]); continue
    }
    var faces: [[String: Any]] = []
    for f in detectFaces(cg) {
        guard let crop = cropFace(cg, f.boundingBox), let vec = featurePrint(crop) else { continue }
        faces.append([
            "bbox": [f.boundingBox.origin.x, f.boundingBox.origin.y,
                     f.boundingBox.width, f.boundingBox.height],
            "vec": vec,
        ])
    }
    emit(["path": path, "faces": faces])
}
