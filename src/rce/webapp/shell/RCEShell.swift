// RCE.app -- the native shell around the RCE web app (DESIGN.md section 8.9,
// task V4 phase 3).
//
// One Swift file, AppKit + WebKit only, no Xcode project: `rce app`
// (rce.webapp.macapp) compiles it with the system `swiftc -O` into
// RCE.app/Contents/MacOS/RCE. The engine stays Python and the interface
// stays app.html; this program is only a real macOS window around it --
// Dock icon, menu bar, Cmd-shortcuts, no address bar -- the same split
// Obsidian, Notion and VS Code use, so every feature is still written once.
//
// What it does, and the security reasoning behind each piece:
//
// - Configuration comes from two sidecar files in Contents/Resources that
//   macapp.py writes at build time, read here at RUNTIME: `rce-path` (the
//   absolute path of the venv's `rce` entry point, read byte-exact) and
//   `rce-port` (the port, default 7357). No value is ever interpolated
//   into this source text, so a hostile venv path (quotes, spaces, CJK)
//   can never become Swift code -- it is only ever a Process executable
//   URL.
// - Lifecycle: probe http://127.0.0.1:<port>/api/projects. If an engine
//   already answers (e.g. one the researcher started from a terminal),
//   show it and never touch it. Otherwise spawn `<rce> serve --port <port>
//   --no-browser` as a child Process, output appended to serve.log in the
//   RCE home ($RCE_HOME when set -- the same override rce.paths.rce_home
//   honours -- else ~/.rce), and show a placeholder page (paper, the serif
//   mark, 「正在启动引擎…」) until the probe answers. After a few seconds
//   without an answer it adds that macOS may be asking for the Documents
//   folder (9.12: a rebuilt app's first read under ~/Documents waits for
//   that answer, and the log has nothing in it then). Only if the child
//   EXITS does the placeholder show the log's tail, AS TEXT: every
//   character is HTML-escaped, so a log line can never become markup. The
//   child runs with PYTHONUNBUFFERED=1, so its startup line and any error
//   reach serve.log at once. On quit, and only if THIS app spawned the engine and it is
//   still running, POST /api/shutdown with body {"pid": <child's pid>} and
//   wait for it to exit (terminate it after a grace period). The pid is
//   what keeps a terminal-started engine safe: the child may still be
//   alive WITHOUT having bound the port (blocked in a slow first-touch
//   migration) while an engine the researcher started by hand holds it --
//   the server answers 409 to a pid that is not its own and keeps
//   serving, and our own child is then stopped by signal alone (8.9: an
//   engine the user started is left alone). SIGTERM/SIGINT take the same
//   quit path.
// - Bridge, native -> page: every menu command runs the fixed string
//   `window.RCE && RCE.command('<name>')`, the name taken from the
//   compiled-in whitelist `shellCommands` below and checked again right
//   before use -- never from data. Page -> native: one message handler,
//   `rce`, accepting exactly `{type: "title", text: String}` from the main
//   frame of the engine's own origin; the label is stripped of control
//   characters and length-capped, then the window reads 「RCE — <label>」.
//   Since V5 phase 9 one more shape, `{type: "choose-folder", request:
//   String}` (「选择新位置…」, DESIGN.md 9.4): an NSOpenPanel limited to one
//   directory, whose answer goes back through the fixed callback
//   `RCE.folderChosen(request, path)` -- called with callAsyncJavaScript,
//   the request id and the path passed as ARGUMENTS (serialized by WebKit),
//   never spliced into script text. The page learns it may ask from
//   `window.RCEShellFeatures`, injected at document start. Every other
//   message shape is ignored.
// - Navigation: only http://127.0.0.1:<port> (and the placeholder's
//   about:blank) load in the window. Any other http(s)/mailto link opens
//   in the default browser; every other scheme is refused. Developer
//   extras stay off; no WebKit preference is touched.
// - The page's window.confirm/alert are answered by native sheets
//   (WKWebView returns false/does nothing without a UI delegate, which
//   would silently cancel every 删除标注 confirmation).

import AppKit
@preconcurrency import WebKit

// The page-side dispatcher (app.html, "Native shell bridge") accepts the
// same names. Only [a-z-] characters: safe inside a single-quoted JS string.
let shellCommands: Set<String> = [
    "tree", "lineage", "canvas", "variables", "new-attempt", "reload",
    "zoom-in", "zoom-out", "zoom-reset", "fit", "reveal-project", "open-map",
]

let defaultPort = 7357
let probeTimeout: TimeInterval = 1.0
// After this long without an answer the waiting page names the likeliest
// cause on a first launch after an install (DESIGN.md 9.12): macOS asking
// whether RCE may read the Documents folder, which suspends the engine's
// first read there until the researcher answers.
let startupBudget: TimeInterval = 4.0
let documentsHint = "正在启动引擎… 如果系统询问是否允许 RCE 访问\"文稿\"文件夹，请点\"允许\"。"
let titleLimit = 60
// What the page may ask the shell for (app.html, shellCan).
let shellFeaturesScript = "window.RCEShellFeatures = [\"choose-folder\"];"
// The page's own request ids ("folder-<time>-<random>"): anything else is ignored.
let folderRequestLimit = 80

func isFolderRequest(_ request: String) -> Bool {
    !request.isEmpty && request.count <= folderRequestLimit
        && request.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.contains($0) || $0 == "-" }
        && request.allSatisfy { $0.isASCII }
}

let paperColor = NSColor(srgbRed: 0xF7 / 255.0, green: 0xF2 / 255.0, blue: 0xE9 / 255.0, alpha: 1)

// -- Pure helpers -------------------------------------------------------------

func htmlEscape(_ text: String) -> String {
    var out = ""
    out.reserveCapacity(text.count)
    for ch in text {
        switch ch {
        case "&": out += "&amp;"
        case "<": out += "&lt;"
        case ">": out += "&gt;"
        case "\"": out += "&quot;"
        case "'": out += "&#39;"
        default: out.append(ch)
        }
    }
    return out
}

// A project label for the window title: control and format characters
// (newlines, bidi overrides) dropped, whitespace runs collapsed, capped.
func sanitizedLabel(_ raw: String) -> String {
    var cleaned = ""
    var lastWasSpace = false
    for scalar in raw.unicodeScalars {
        let category = scalar.properties.generalCategory
        if category == .control || category == .format || category == .lineSeparator
            || category == .paragraphSeparator {
            continue
        }
        if scalar.properties.isWhitespace {
            if !lastWasSpace && !cleaned.isEmpty { cleaned.append(" ") }
            lastWasSpace = true
        } else {
            cleaned.unicodeScalars.append(scalar)
            lastWasSpace = false
        }
    }
    cleaned = cleaned.trimmingCharacters(in: .whitespaces)
    if cleaned.count > titleLimit {
        cleaned = String(cleaned.prefix(titleLimit)) + "…"
    }
    return cleaned
}

func resourceText(_ name: String) -> String? {
    guard let url = Bundle.main.resourceURL?.appendingPathComponent(name),
          let data = try? Data(contentsOf: url) else { return nil }
    return String(data: data, encoding: .utf8)
}

func configuredPort() -> Int {
    guard let text = resourceText("rce-port"),
          let value = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)),
          (1...65535).contains(value) else { return defaultPort }
    return value
}

func rceHome() -> URL {
    if let override = ProcessInfo.processInfo.environment["RCE_HOME"], !override.isEmpty {
        return URL(fileURLWithPath: (override as NSString).expandingTildeInPath, isDirectory: true)
    }
    return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".rce", isDirectory: true)
}

func logTail(_ url: URL, maxBytes: Int = 16 * 1024, maxLines: Int = 40) -> String? {
    guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
    defer { try? handle.close() }
    let size = (try? handle.seekToEnd()) ?? 0
    let start = size > UInt64(maxBytes) ? size - UInt64(maxBytes) : 0
    try? handle.seek(toOffset: start)
    let data = (try? handle.readToEnd()) ?? Data()
    let text = String(decoding: data, as: UTF8.self)
    let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
    return lines.suffix(maxLines).joined(separator: "\n")
}

// -- Placeholder pages (fixed markup; only escaped text is ever inserted) -----

func placeholderPage(headline: String, detail: String?, logText: String?) -> String {
    var body = "<div class=\"mark\">RCE</div><p class=\"msg\">\(htmlEscape(headline))</p>"
    if let detail = detail {
        body += "<p class=\"detail\">\(htmlEscape(detail))</p>"
    }
    if let logText = logText {
        body += "<pre>\(htmlEscape(logText))</pre>"
    }
    return """
    <!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
    <style>
    html, body { margin: 0; height: 100%; background: #F7F2E9; color: #26221B; }
    body { display: flex; flex-direction: column; align-items: center; justify-content: center;
           font-family: "PingFang SC", -apple-system, sans-serif; }
    .mark { font-family: "Songti SC", "STSong", serif; font-size: 56px; letter-spacing: 0.04em; }
    .msg { font-size: 15px; color: #5B564A; margin: 14px 0 0; }
    .detail { font-size: 13px; color: #5B564A; margin: 8px 24px 0; text-align: center; }
    pre { max-width: 86%; max-height: 50%; overflow: auto; margin: 18px 0 0; padding: 12px 14px;
          background: #F0E9DA; border: 1px solid rgba(38, 34, 27, 0.14); border-radius: 6px;
          font: 12px ui-monospace, Menlo, monospace; white-space: pre-wrap; word-break: break-all; }
    </style></head><body>\(body)</body></html>
    """
}

// -- The application ------------------------------------------------------------

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuItemValidation,
    WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {

    let port = configuredPort()
    var window: NSWindow!
    var webView: WKWebView!
    var child: Process?
    var probeTimer: Timer?
    var launchStarted = Date()
    var appLoaded = false
    var showingLog = false
    var hintShown = false
    var quitting = false
    var signalSources: [DispatchSourceSignal] = []
    var choosingFolder = false

    lazy var session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = probeTimeout
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: config)
    }()

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }
    var logURL: URL { rceHome().appendingPathComponent("serve.log") }

    // MARK: launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = buildMainMenu()
        buildWindow()
        installSignalHandlers()
        startEngine()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 840),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "RCE"
        window.contentMinSize = NSSize(width: 900, height: 600)
        window.isReleasedWhenClosed = false
        window.backgroundColor = paperColor
        window.center()
        window.setFrameAutosaveName("RCEMain")

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "rce")
        config.userContentController.addUserScript(WKUserScript(
            source: shellFeaturesScript, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView = WKWebView(frame: window.contentView!.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        window.contentView!.addSubview(webView)
        window.makeKeyAndOrderFront(nil)
    }

    func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { NSApp.terminate(nil) }
            source.resume()
            signalSources.append(source)
        }
    }

    // MARK: engine lifecycle

    func showPlaceholder(_ headline: String, detail: String? = nil, log: String? = nil) {
        appLoaded = false
        webView.loadHTMLString(placeholderPage(headline: headline, detail: detail, logText: log), baseURL: nil)
    }

    func probe(_ done: @escaping (Bool) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/projects"))
        request.timeoutInterval = probeTimeout
        session.dataTask(with: request) { _, response, error in
            let ok = error == nil && (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { done(ok) }
        }.resume()
    }

    // Probe once; an engine that answers is used as-is (and never stopped
    // by us). Otherwise spawn our own and poll until it answers.
    func startEngine() {
        probeTimer?.invalidate()
        showingLog = false
        hintShown = false
        launchStarted = Date()
        showPlaceholder("正在启动引擎…")
        probe { [weak self] ok in
            guard let self = self else { return }
            if ok { self.loadApp(); return }
            if self.child?.isRunning != true {
                guard self.spawnEngine() else { return }
            }
            self.schedulePolling()
        }
    }

    func spawnEngine() -> Bool {
        guard let rcePath = resourceText("rce-path"), !rcePath.isEmpty else {
            showPlaceholder("找不到 rce 引擎的位置", detail: "请在终端里重新运行 rce app。")
            return false
        }
        guard FileManager.default.isExecutableFile(atPath: rcePath) else {
            showPlaceholder("找不到 rce 引擎", detail: rcePath + " 不存在或不可执行。请在终端里重新运行 rce app。")
            return false
        }
        let home = rceHome()
        try? FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: rcePath)
        process.arguments = ["serve", "--port", String(port), "--no-browser"]
        // Unbuffered, so serve.log has the engine's words by the time the
        // placeholder shows its tail -- a block-buffered child that hangs
        // would otherwise leave the log empty.
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        if let handle = try? FileHandle(forWritingTo: logURL) {
            _ = try? handle.seekToEnd()
            process.standardOutput = handle
            process.standardError = handle
        }
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async { self?.engineExited() }
        }
        do {
            try process.run()
        } catch {
            showPlaceholder("无法启动 rce 引擎", detail: error.localizedDescription)
            return false
        }
        child = process
        return true
    }

    func engineExited() {
        if quitting || appLoaded { return }
        probeTimer?.invalidate()
        showLog(headline: "引擎启动失败")
    }

    func schedulePolling() {
        probeTimer?.invalidate()
        let timer = Timer(timeInterval: 0.25, repeats: true) { [weak self] _ in self?.pollTick() }
        RunLoop.main.add(timer, forMode: .common)
        probeTimer = timer
    }

    func pollTick() {
        probe { [weak self] ok in
            guard let self = self, !self.appLoaded, !self.quitting else { return }
            if ok {
                self.probeTimer?.invalidate()
                self.loadApp()
            } else if !self.showingLog && !self.hintShown
                        && Date().timeIntervalSince(self.launchStarted) > startupBudget {
                // Keep polling afterwards: a slow start still recovers. No
                // log tail here -- a suspended engine has written nothing;
                // the log is shown only once the engine has EXITED.
                self.hintShown = true
                self.showPlaceholder(documentsHint)
            }
        }
    }

    func showLog(headline: String) {
        showingLog = true
        let tail = logTail(logURL)
        let detail = tail == nil
            ? "还没有日志文件：" + logURL.path
            : "以下是 " + logURL.path + " 的最后几行（⌘R 重试）："
        showPlaceholder(headline, detail: detail, log: tail ?? nil)
    }

    func loadApp() {
        appLoaded = true
        webView.load(URLRequest(url: baseURL))
    }

    // MARK: quit

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let process = child, process.isRunning else { return .terminateNow }
        if quitting { return .terminateLater }
        quitting = true
        probeTimer?.invalidate()
        var request = URLRequest(url: baseURL.appendingPathComponent("api/shutdown"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // Only the engine whose pid this is may act on it (header comment).
        request.httpBody = Data("{\"pid\": \(process.processIdentifier)}".utf8)
        session.dataTask(with: request) { _, _, _ in }.resume()

        let deadline = Date().addingTimeInterval(3)
        var terminatedAt: Date?
        let timer = Timer(timeInterval: 0.1, repeats: true) { timer in
            if !process.isRunning {
                timer.invalidate()
                NSApp.reply(toApplicationShouldTerminate: true)
            } else if terminatedAt == nil && Date() > deadline {
                terminatedAt = Date()
                process.terminate()
            } else if let t = terminatedAt, Date().timeIntervalSince(t) > 2 {
                kill(process.processIdentifier, SIGKILL)
                timer.invalidate()
                NSApp.reply(toApplicationShouldTerminate: true)
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        return .terminateLater
    }

    // MARK: menus

    func item(_ title: String, _ action: Selector?, _ key: String = "",
              _ modifiers: NSEvent.ModifierFlags = [.command], command: String? = nil) -> NSMenuItem {
        let menuItem = NSMenuItem(title: title, action: action, keyEquivalent: key)
        menuItem.keyEquivalentModifierMask = modifiers
        if let command = command {
            precondition(shellCommands.contains(command))
            menuItem.target = self
            menuItem.representedObject = command
        }
        return menuItem
    }

    func cmd(_ title: String, _ name: String, _ key: String = "",
             _ modifiers: NSEvent.ModifierFlags = [.command]) -> NSMenuItem {
        item(title, #selector(sendCommand(_:)), key, modifiers, command: name)
    }

    func submenu(_ title: String, _ items: [NSMenuItem]) -> NSMenuItem {
        let top = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let menu = NSMenu(title: title)
        items.forEach { menu.addItem($0) }
        top.submenu = menu
        return top
    }

    func buildMainMenu() -> NSMenu {
        let main = NSMenu()
        main.addItem(submenu("RCE", [
            item("关于 RCE", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            .separator(),
            item("退出", #selector(NSApplication.terminate(_:)), "q"),
        ]))
        main.addItem(submenu("文件", [
            cmd("新增尝试", "new-attempt", "n"),
            cmd("在 Finder 中显示项目", "reveal-project", "r", [.command, .shift]),
            .separator(),
            item("关闭", #selector(NSWindow.performClose(_:)), "w"),
        ]))
        main.addItem(submenu("编辑", [
            item("撤销", Selector(("undo:")), "z"),
            item("重做", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("剪切", #selector(NSText.cut(_:)), "x"),
            item("拷贝", #selector(NSText.copy(_:)), "c"),
            item("粘贴", #selector(NSText.paste(_:)), "v"),
            item("全选", #selector(NSText.selectAll(_:)), "a"),
        ]))
        // ⌘= reaches 放大 too (no Shift needed on most layouts) through a
        // hidden twin, as Safari does.
        let zoomInAlt = cmd("放大", "zoom-in", "=")
        zoomInAlt.isHidden = true
        zoomInAlt.allowsKeyEquivalentWhenHidden = true
        let viewMenu = submenu("视图", [
            cmd("决策树", "tree", "1"),
            cmd("血缘", "lineage", "2"),
            cmd("画布", "canvas", "3"),
            cmd("变量", "variables", "4"),
            .separator(),
            item("重新载入", #selector(reloadPage(_:)), "r"),
            .separator(),
            cmd("放大", "zoom-in", "+"),
            zoomInAlt,
            cmd("缩小", "zoom-out", "-"),
            cmd("实际大小", "zoom-reset", "0"),
            cmd("适应全部", "fit", "f", []),
        ])
        viewMenu.submenu?.items.first { $0.action == #selector(reloadPage(_:)) }?.target = self
        main.addItem(viewMenu)
        let windowMenu = submenu("窗口", [
            item("最小化", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("缩放", #selector(NSWindow.performZoom(_:)), ""),
        ])
        main.addItem(windowMenu)
        NSApp.windowsMenu = windowMenu.submenu
        let helpMenu = submenu("帮助", [cmd("打开项目地图", "open-map")])
        main.addItem(helpMenu)
        NSApp.helpMenu = helpMenu.submenu
        return main
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem.action == #selector(sendCommand(_:)) { return appLoaded }
        return true
    }

    @objc func sendCommand(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String, shellCommands.contains(name),
              name.allSatisfy({ ("a"..."z").contains($0) || $0 == "-" }), appLoaded else { return }
        webView.evaluateJavaScript("window.RCE && RCE.command('\(name)')", completionHandler: nil)
    }

    // 重新载入: the page reloads itself; the placeholder re-runs the probe
    // (and respawns the engine if ours is no longer running).
    @objc func reloadPage(_ sender: Any?) {
        if appLoaded {
            webView.evaluateJavaScript("window.RCE && RCE.command('reload')") { [weak self] result, _ in
                if (result as? Bool) != true { self?.webView.reload() }
            }
        } else {
            startEngine()
        }
    }

    // MARK: navigation

    func isEngineURL(_ url: URL?) -> Bool {
        guard let url = url else { return false }
        return url.scheme == "http" && url.host == "127.0.0.1" && url.port == port
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let url = navigationAction.request.url
        if isEngineURL(url) || url?.absoluteString == "about:blank" {
            decisionHandler(.allow)
            return
        }
        decisionHandler(.cancel)
        openExternally(url)
    }

    func openExternally(_ url: URL?) {
        guard let url = url, let scheme = url.scheme?.lowercased(),
              ["http", "https", "mailto"].contains(scheme) else { return }
        NSWorkspace.shared.open(url)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        // The engine went away between the probe and the load: start over.
        if appLoaded && !quitting {
            appLoaded = false
            startEngine()
        }
    }

    // target=_blank / window.open: never a second web view.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        let url = navigationAction.request.url
        if isEngineURL(url), let url = url {
            webView.load(URLRequest(url: url))
        } else {
            openExternally(url)
        }
        return nil
    }

    // MARK: page dialogs (window.alert / window.confirm)

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "好")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "好")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { response in
            completionHandler(response == .alertFirstButtonReturn)
        }
    }

    // MARK: bridge (page -> native)

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "rce", message.frameInfo.isMainFrame else { return }
        let origin = message.frameInfo.securityOrigin
        guard origin.protocol == "http", origin.host == "127.0.0.1", origin.port == port else { return }
        guard let body = message.body as? [String: Any], body.count == 2 else { return }
        if body["type"] as? String == "title", let text = body["text"] as? String {
            let label = sanitizedLabel(text)
            window.title = label.isEmpty ? "RCE" : "RCE — " + label
        } else if body["type"] as? String == "choose-folder", let request = body["request"] as? String,
                  isFolderRequest(request) {
            chooseFolder(request)
        }
    }

    // 「选择新位置…」: one directory, chosen in a sheet; the answer (or null
    // when cancelled) goes back through the page's fixed callback.
    func chooseFolder(_ request: String) {
        guard !choosingFolder, appLoaded else {
            answerFolder(request, nil)
            return
        }
        choosingFolder = true
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.prompt = "选择"
        panel.message = "选择项目文件夹的新位置"
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self = self else { return }
            self.choosingFolder = false
            let path = response == .OK ? panel.url?.path : nil
            self.answerFolder(request, path)
        }
    }

    func answerFolder(_ request: String, _ path: String?) {
        let arguments: [String: Any] = ["request": request, "path": path ?? NSNull()]
        webView.callAsyncJavaScript("window.RCE && RCE.folderChosen(request, path)", arguments: arguments,
                                    in: nil, in: .page, completionHandler: nil)
    }
}

@main
struct RCEShellMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { app.run() }
    }
}
