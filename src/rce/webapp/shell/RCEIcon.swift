// RCEIcon -- draws RCE.app's icon as a full macOS .iconset (DESIGN.md
// section 8.9, "Icon"; task V4 phase 3).
//
// Compiled and run once by `rce app` (rce.webapp.macapp) with the same
// system toolchain as the shell itself, then folded into RCE.icns by
// `iconutil -c icns`. Usage: RCEIcon <output.iconset directory>.
//
// The drawing, in a 1024-unit square (y up):
// - a paper (#F7F2E9) squircle on the macOS icon grid (824 units, 100-unit
//   margin) with a hairline in --line-strong so it holds on a light Dock;
// - echoing the canvas, three sockets in a left-to-right flow: an ochre
//   dot (数据集), an olive rounded square (脚本), a clay dot (图表) --
//   joined by two ink horizontal-tangent Béziers, the canvas's own link
//   shape. At 16-32 px the links drop and only the three marks remain,
//   slightly larger, since a 1-px Bézier at that size is noise.
// No text on the icon. Colors are the app.html tokens, nothing new.

import AppKit

func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
}

let paper = rgb(0xF7F2E9)
let ink = rgb(0x26221B)
let lineStrong = rgb(0x26221B, 0.32)
let ochre = rgb(0x9A6E1F)
let olive = rgb(0x6C7042)
let clay = rgb(0xA64B2A)

func drawIcon(pixels: Int) {
    let scale = CGFloat(pixels) / 1024
    let transform = NSAffineTransform()
    transform.scale(by: scale)
    transform.concat()

    let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
    let squircle = NSBezierPath(roundedRect: tile, xRadius: 185, yRadius: 185)
    paper.setFill()
    squircle.fill()
    lineStrong.setStroke()
    squircle.lineWidth = max(1 / scale, 6)
    squircle.stroke()

    let small = pixels <= 32
    let dot: CGFloat = small ? 96 : 66          // dot radius
    let square: CGFloat = small ? 184 : 136     // rounded-square side
    let left = NSPoint(x: small ? 290 : 236, y: small ? 512 : 440)
    let mid = NSPoint(x: 512, y: small ? 512 : 596)
    let right = NSPoint(x: small ? 734 : 788, y: small ? 512 : 440)

    if !small {
        ink.setStroke()
        for (from, to) in [(NSPoint(x: left.x + dot, y: left.y), NSPoint(x: mid.x - square / 2, y: mid.y)),
                           (NSPoint(x: mid.x + square / 2, y: mid.y), NSPoint(x: right.x - dot, y: right.y))] {
            let link = NSBezierPath()
            let bend = (to.x - from.x) * 0.55
            link.move(to: from)
            link.curve(to: to, controlPoint1: NSPoint(x: from.x + bend, y: from.y),
                       controlPoint2: NSPoint(x: to.x - bend, y: to.y))
            link.lineWidth = 18
            link.lineCapStyle = .round
            link.stroke()
        }
    }

    ochre.setFill()
    NSBezierPath(ovalIn: NSRect(x: left.x - dot, y: left.y - dot, width: dot * 2, height: dot * 2)).fill()
    olive.setFill()
    NSBezierPath(roundedRect: NSRect(x: mid.x - square / 2, y: mid.y - square / 2, width: square, height: square),
                 xRadius: square * 0.2, yRadius: square * 0.2).fill()
    clay.setFill()
    NSBezierPath(ovalIn: NSRect(x: right.x - dot, y: right.y - dot, width: dot * 2, height: dot * 2)).fill()
}

func writePNG(pixels: Int, to url: URL) throws {
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
        samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0),
        let context = NSGraphicsContext(bitmapImageRep: rep) else {
        throw NSError(domain: "RCEIcon", code: 1, userInfo: [NSLocalizedDescriptionKey: "cannot create bitmap"])
    }
    rep.size = NSSize(width: pixels, height: pixels)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    context.imageInterpolation = .high
    drawIcon(pixels: pixels)
    context.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()
    guard let data = rep.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "RCEIcon", code: 2, userInfo: [NSLocalizedDescriptionKey: "cannot encode PNG"])
    }
    try data.write(to: url)
}

// The ten files iconutil expects in an .iconset.
let iconsetEntries: [(String, Int)] = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]

@main
struct RCEIconMain {
    static func main() {
        let args = CommandLine.arguments
        guard args.count == 2 else {
            FileHandle.standardError.write(Data("usage: RCEIcon <output.iconset>\n".utf8))
            exit(2)
        }
        let out = URL(fileURLWithPath: args[1], isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
            for (name, pixels) in iconsetEntries {
                try writePNG(pixels: pixels, to: out.appendingPathComponent(name))
            }
        } catch {
            FileHandle.standardError.write(Data("RCEIcon: \(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }
}
