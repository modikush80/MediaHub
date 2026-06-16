// MediaHubShell.swift — native macOS shell (SwiftUI + WKWebView)
//
// Responsibilities (Priority 4):
//   • Detect an already-running MediaHub instance and attach to it.
//   • Otherwise start the bundled Python backend (python3 -m mediahub) with the
//     browser suppressed, parse its port, and host the UI in a WKWebView window.
//   • Clean lifecycle: only stop the backend if WE started it.
//
// Build: swiftc -O -parse-as-library -framework WebKit MediaHubShell.swift -o MediaHub
import SwiftUI
import WebKit
import AppKit

// MARK: - Backend lifecycle
final class Backend: ObservableObject {
    @Published var url: URL?
    @Published var status: String = "Starting MediaHub…"
    private var process: Process?
    private var weStarted = false

    /// Bundled engine dir: <bundle>/Contents/Resources/app
    private var appDir: String {
        if let res = Bundle.main.resourceURL?.appendingPathComponent("app").path,
           FileManager.default.fileExists(atPath: res + "/mediahub") {
            return res
        }
        // dev fallback: repo root next to this source
        return FileManager.default.currentDirectoryPath
    }

    private func pythonPath() -> String? {
        // Prefer a Python bundled inside the app (zero external dependency).
        if let res = Bundle.main.resourceURL?.appendingPathComponent("python/bin/python3").path,
           FileManager.default.isExecutableFile(atPath: res) {
            return res
        }
        // Fall back to a system Python.
        let cands = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        return cands.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    private func isMediaHub(port: Int) -> Bool {
        guard let u = URL(string: "http://127.0.0.1:\(port)/api/summary") else { return false }
        var req = URLRequest(url: u); req.timeoutInterval = 0.8
        let sem = DispatchSemaphore(value: 0); var ok = false
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            if let h = resp as? HTTPURLResponse,
               h.value(forHTTPHeaderField: "X-App") == "MediaHub" { ok = true }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 1.2)
        return ok
    }

    func start() {
        DispatchQueue.global().async {
            // 1) Attach to an existing instance (don't spawn a duplicate).
            for p in 8765...8775 where self.isMediaHub(port: p) {
                self.publish(URL(string: "http://127.0.0.1:\(p)/"), "Connected to running MediaHub")
                return
            }
            // 2) Spawn the bundled backend.
            guard let py = self.pythonPath() else {
                self.publish(nil, "Python 3 not found. Install it (e.g. brew install python) and reopen.")
                return
            }
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: py)
            proc.arguments = ["-m", "mediahub"]
            proc.currentDirectoryURL = URL(fileURLWithPath: self.appDir)
            var env = ProcessInfo.processInfo.environment
            env["PYTHONPATH"] = self.appDir
            env["MEDIAHUB_NO_BROWSER"] = "1"      // the shell owns the window
            proc.environment = env
            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = pipe
            pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
                let data = h.availableData
                guard !data.isEmpty, let s = String(data: data, encoding: .utf8) else { return }
                if let r = s.range(of: "http://127.0.0.1:") {
                    let frag = String(s[r.lowerBound...])
                    let token = frag.split(whereSeparator: { " \n\r\t".contains($0) }).first.map(String.init) ?? ""
                    if let u = URL(string: token.trimmingCharacters(in: .whitespacesAndNewlines)) {
                        self?.publish(u, "Running")
                    }
                }
            }
            do {
                try proc.run()
                self.process = proc
                self.weStarted = true
                self.pollForServer()      // robust fallback to stdout parsing
            } catch {
                self.publish(nil, "Failed to start backend: \(error.localizedDescription)")
            }
        }
    }

    /// Belt-and-suspenders: probe ports for the X-App header in case the
    /// stdout "running at" line is missed.
    private func pollForServer() {
        DispatchQueue.global().async {
            for _ in 0..<40 {
                if self.url != nil { return }
                for p in 8765...8775 where self.isMediaHub(port: p) {
                    self.publish(URL(string: "http://127.0.0.1:\(p)/"), "Running")
                    return
                }
                Thread.sleep(forTimeInterval: 0.5)
            }
            if self.url == nil {
                self.publish(nil, "Backend did not respond. Open Console and check ~/Library/Logs.")
            }
        }
    }

    private func publish(_ u: URL?, _ msg: String) {
        DispatchQueue.main.async {
            if let u = u, self.url == nil { self.url = u }
            self.status = msg
        }
    }

    func stop() {
        if weStarted, let p = process, p.isRunning { p.terminate() }
    }
}

// MARK: - WKWebView host
struct WebView: NSViewRepresentable {
    let url: URL
    func makeNSView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        let wv = WKWebView(frame: .zero, configuration: cfg)
        wv.load(URLRequest(url: url))
        return wv
    }
    func updateNSView(_ wv: WKWebView, context: Context) {
        if wv.url == nil { wv.load(URLRequest(url: url)) }
    }
}

// MARK: - UI
struct ContentView: View {
    @ObservedObject var backend: Backend
    var body: some View {
        Group {
            if let u = backend.url {
                WebView(url: u)
            } else {
                VStack(spacing: 14) {
                    ProgressView()
                    Text(backend.status).foregroundColor(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(minWidth: 1120, minHeight: 740)
    }
}

// MARK: - App lifecycle
final class AppDelegate: NSObject, NSApplicationDelegate {
    let backend = Backend()
    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        backend.start()
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ n: Notification) { backend.stop() }
}

@main
struct MediaHubApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene {
        WindowGroup("MediaHub") {
            ContentView(backend: delegate.backend)
        }
    }
}
