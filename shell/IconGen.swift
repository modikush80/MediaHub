// IconGen.swift — renders a futuristic MediaHub app icon to a 1024x1024 PNG.
// Native AppKit/CoreGraphics only. Usage: swiftc -O IconGen.swift -o icongen && ./icongen out.png
import AppKit
import CoreGraphics

let S: CGFloat = 1024
let args = CommandLine.arguments
let outPath = args.count > 1 ? args[1] : "MediaHub_master.png"

guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
      let ctx = CGContext(data: nil, width: Int(S), height: Int(S),
                          bitsPerComponent: 8, bytesPerRow: 0, space: cs,
                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
    fatalError("ctx")
}

func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor {
    return CGColor(colorSpace: cs, components: [r/255, g/255, b/255, a])!
}

// ---- Squircle background path (Apple-style rounded rect with art inset) ----
let inset: CGFloat = 100
let rect = CGRect(x: inset, y: inset, width: S - inset*2, height: S - inset*2)
let radius: CGFloat = (S - inset*2) * 0.2237   // continuous-corner approximation
let bg = CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)

ctx.saveGState()
ctx.addPath(bg)
ctx.clip()

// Background vertical gradient: deep space indigo -> near-black violet
let bgGrad = CGGradient(colorsSpace: cs, colors: [
    rgb(46, 38, 96),     // top  #2E2660
    rgb(24, 20, 56),     // mid  #181438
    rgb(9, 8, 24)        // bottom #090818
] as CFArray, locations: [0, 0.55, 1])!
ctx.drawLinearGradient(bgGrad, start: CGPoint(x: 0, y: S), end: CGPoint(x: 0, y: 0), options: [])

// Center radial glow (cyan/violet aura behind the hub)
let center = CGPoint(x: S/2, y: S/2)
let glow = CGGradient(colorsSpace: cs, colors: [
    rgb(86, 230, 255, 0.55),   // cyan
    rgb(150, 90, 255, 0.22),   // violet
    rgb(9, 8, 24, 0.0)
] as CFArray, locations: [0, 0.45, 1])!
ctx.drawRadialGradient(glow, startCenter: center, startRadius: 0,
                       endCenter: center, endRadius: S*0.46, options: [])

// ---- Orbital network: nodes connected to the hub ----
let orbitR: CGFloat = S * 0.315
let nodeCount = 6
var nodes: [CGPoint] = []
for i in 0..<nodeCount {
    let ang = CGFloat(i) / CGFloat(nodeCount) * .pi * 2 - .pi/2 + 0.18
    nodes.append(CGPoint(x: center.x + cos(ang)*orbitR, y: center.y + sin(ang)*orbitR))
}

// connecting lines (thin, glowing)
ctx.setLineWidth(3.0)
ctx.setStrokeColor(rgb(120, 220, 255, 0.35))
for n in nodes {
    ctx.move(to: center)
    ctx.addLine(to: n)
}
ctx.strokePath()

// node dots with halo
for (i, n) in nodes.enumerated() {
    let ndGlow = CGGradient(colorsSpace: cs, colors: [
        rgb(150, 245, 255, 0.9), rgb(150, 245, 255, 0.0)
    ] as CFArray, locations: [0, 1])!
    ctx.drawRadialGradient(ndGlow, startCenter: n, startRadius: 0,
                           endCenter: n, endRadius: 34, options: [])
    let r: CGFloat = i % 2 == 0 ? 13 : 9
    ctx.setFillColor(rgb(235, 252, 255))
    ctx.fillEllipse(in: CGRect(x: n.x - r, y: n.y - r, width: r*2, height: r*2))
}

// ---- Central hexagonal aperture (the "hub") ----
func hexPath(_ c: CGPoint, _ r: CGFloat, rotate: CGFloat = 0) -> CGPath {
    let p = CGMutablePath()
    for i in 0..<6 {
        let a = CGFloat(i)/6 * .pi*2 - .pi/2 + rotate
        let pt = CGPoint(x: c.x + cos(a)*r, y: c.y + sin(a)*r)
        if i == 0 { p.move(to: pt) } else { p.addLine(to: pt) }
    }
    p.closeSubpath()
    return p
}

// outer hex glow ring
let hexR: CGFloat = S * 0.205
ctx.saveGState()
ctx.setShadow(offset: .zero, blur: 46, color: rgb(80, 220, 255, 0.95))
ctx.setStrokeColor(rgb(120, 235, 255, 0.95))
ctx.setLineWidth(14)
ctx.addPath(hexPath(center, hexR))
ctx.strokePath()
ctx.restoreGState()

// inner hex fill — neon gradient (magenta -> cyan)
ctx.saveGState()
ctx.addPath(hexPath(center, hexR - 10))
ctx.clip()
let coreGrad = CGGradient(colorsSpace: cs, colors: [
    rgb(255, 86, 196),    // magenta top
    rgb(120, 96, 255),    // violet
    rgb(60, 210, 255)     // cyan bottom
] as CFArray, locations: [0, 0.5, 1])!
ctx.drawLinearGradient(coreGrad,
    start: CGPoint(x: center.x - hexR, y: center.y + hexR),
    end: CGPoint(x: center.x + hexR, y: center.y - hexR), options: [])
// darken center to create aperture depth
let depth = CGGradient(colorsSpace: cs, colors: [
    rgb(9, 8, 24, 0.0), rgb(9, 8, 24, 0.82)
] as CFArray, locations: [0, 1])!
ctx.drawRadialGradient(depth, startCenter: center, startRadius: hexR*0.15,
                       endCenter: center, endRadius: hexR, options: [])
ctx.restoreGState()

// aperture blades (thin radial lines inside hex)
ctx.saveGState()
ctx.addPath(hexPath(center, hexR - 12))
ctx.clip()
ctx.setLineWidth(4)
ctx.setStrokeColor(rgb(180, 240, 255, 0.5))
for i in 0..<6 {
    let a = CGFloat(i)/6 * .pi*2 - .pi/2 + 0.52
    ctx.move(to: center)
    ctx.addLine(to: CGPoint(x: center.x + cos(a)*hexR, y: center.y + sin(a)*hexR))
}
ctx.strokePath()
ctx.restoreGState()

// bright central core (play/energy nucleus)
let nucleus = CGGradient(colorsSpace: cs, colors: [
    rgb(255, 255, 255, 1.0), rgb(150, 240, 255, 0.9), rgb(150, 240, 255, 0.0)
] as CFArray, locations: [0, 0.4, 1])!
ctx.drawRadialGradient(nucleus, startCenter: center, startRadius: 0,
                       endCenter: center, endRadius: S*0.085, options: [])

// crisp play triangle in the nucleus (media cue)
let tr: CGFloat = S*0.038
let tri = CGMutablePath()
tri.move(to: CGPoint(x: center.x - tr*0.7, y: center.y + tr))
tri.addLine(to: CGPoint(x: center.x - tr*0.7, y: center.y - tr))
tri.addLine(to: CGPoint(x: center.x + tr*0.95, y: center.y))
tri.closeSubpath()
ctx.addPath(tri)
ctx.setFillColor(rgb(20, 16, 48, 0.92))
ctx.fillPath()

ctx.restoreGState() // end squircle clip

// ---- Top specular highlight (glass sheen) ----
ctx.saveGState()
ctx.addPath(bg)
ctx.clip()
let sheen = CGGradient(colorsSpace: cs, colors: [
    rgb(255, 255, 255, 0.16), rgb(255, 255, 255, 0.0)
] as CFArray, locations: [0, 1])!
ctx.drawLinearGradient(sheen, start: CGPoint(x: 0, y: S),
                       end: CGPoint(x: 0, y: S*0.62), options: [])
// subtle inner border
ctx.addPath(bg)
ctx.setStrokeColor(rgb(160, 200, 255, 0.18))
ctx.setLineWidth(3)
ctx.strokePath()
ctx.restoreGState()

// ---- Write PNG ----
guard let img = ctx.makeImage() else { fatalError("img") }
let rep = NSBitmapImageRep(cgImage: img)
guard let data = rep.representation(using: .png, properties: [:]) else { fatalError("png") }
try! data.write(to: URL(fileURLWithPath: outPath))
print("wrote \(outPath)")
