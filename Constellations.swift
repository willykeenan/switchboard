import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var children: [ObjectIdentifier: NSWindow] = [:]
    var captures: Set<ObjectIdentifier> = []
    var titleObservers: [ObjectIdentifier:NSKeyValueObservation] = [:]
    var openingVerificationPopup = false
    var panelSelectionRequested = false
    let origin = "http://127.0.0.1:47834"
    func applicationDidFinishLaunching(_ notification: Notification) {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem(); mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu(); appMenu.addItem(withTitle: "Quit Switchboard", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"); appMenuItem.submenu = appMenu
        let editItem = NSMenuItem(); mainMenu.addItem(editItem); let editMenu = NSMenu(title: "Edit"); editItem.submenu = editMenu
        for (title,action,key) in [("Copy","copy:","c"),("Paste","paste:","v"),("Select All","selectAll:","a")] { editMenu.addItem(withTitle:title,action:Selector(action),keyEquivalent:key) }
        NSApp.mainMenu = mainMenu
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        web = WKWebView(frame:.zero,configuration:config)
        web.navigationDelegate=self;web.uiDelegate=self
        window = NSWindow(contentRect:NSRect(x:0,y:0,width:1440,height:930),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        window.title="Switchboard";window.minSize=NSSize(width:1000,height:650)
        window.contentView=web;window.setFrameAutosaveName("KEConstellationsMain");if !window.setFrameUsingName("KEConstellationsMain") { window.center() }
        if CommandLine.arguments.contains("--background") { window.orderBack(nil) } else { window.makeKeyAndOrderFront(nil) }
        web.load(URLRequest(url:URL(string:origin+"/constellations")!))
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { window.makeKeyAndOrderFront(nil);return true }
    func webView(_ webView:WKWebView, decidePolicyFor action:WKNavigationAction, decisionHandler:@escaping(WKNavigationActionPolicy)->Void) {
        guard let u=action.request.url else { decisionHandler(.cancel);return }
        let local = u.scheme=="http" && u.host=="127.0.0.1" && u.port==47834
        if !local && action.navigationType == .linkActivated && ["https","http","mailto"].contains(u.scheme ?? "") { NSWorkspace.shared.open(u) }
        decisionHandler(local ? .allow:.cancel)
    }
    func webView(_ webView:WKWebView, createWebViewWith configuration:WKWebViewConfiguration, for action:WKNavigationAction, windowFeatures:WKWindowFeatures)->WKWebView? {
        guard let u=action.request.url,u.scheme=="http",u.host=="127.0.0.1",u.port==47834 else { return nil }
        let child=WKWebView(frame:.zero,configuration:configuration)
        child.navigationDelegate=self;child.uiDelegate=self
        let panel=NSWindow(contentRect:NSRect(x:0,y:0,width:920,height:830),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        panel.title="Agent session";panel.minSize=NSSize(width:520,height:450);panel.isReleasedWhenClosed=false;panel.delegate=self;panel.contentView=child
        panel.center();children[ObjectIdentifier(panel)]=panel
        titleObservers[ObjectIdentifier(panel)]=child.observe(\.title,options:[.new]) { [weak panel] view,_ in panel?.title=view.title ?? "Agent session" }
        if openingVerificationPopup { panel.orderBack(nil) } else { panel.makeKeyAndOrderFront(nil) }
        return child
    }
    func windowWillClose(_ notification:Notification) { if let panel=notification.object as? NSWindow { children.removeValue(forKey:ObjectIdentifier(panel));titleObservers.removeValue(forKey:ObjectIdentifier(panel)) } }
    func webView(_ webView:WKWebView,didFailProvisionalNavigation navigation:WKNavigation!,withError error:Error) {
        let html="<html><body style='background:#171a1d;color:#edf1f2;font:18px -apple-system;padding:60px'><h1>Switchboard is offline</h1><p>The local room service is unavailable. Your saved layout is preserved.</p><p>Reopen this app after the room service is available.</p></body></html>"
        webView.loadHTMLString(html,baseURL:URL(string:origin))
    }
    func webView(_ webView:WKWebView,didFinish navigation:WKNavigation!) {
        if webView !== web { webView.evaluateJavaScript("document.title") { value,_ in webView.window?.title=(value as? String) ?? "Agent session" } }
        guard let idx=CommandLine.arguments.firstIndex(of:"--verify"),CommandLine.arguments.count>idx+1 else { return }
        if webView !== web, let wanted=CommandLine.arguments.firstIndex(of:"--verify-session"), CommandLine.arguments.count>wanted+1 {
            let agent=webView.url.flatMap { URLComponents(url:$0,resolvingAgainstBaseURL:false) }?.queryItems?.first(where:{$0.name=="agent"})?.value
            guard agent == CommandLine.arguments[wanted+1] else { return }
        }
        let prefix=CommandLine.arguments[idx+1]+(webView === web ? "" : "-session")
        capture(webView,prefix:prefix,attempt:0)
    }
    func capture(_ view:WKWebView,prefix:String,attempt:Int) {
        guard !captures.contains(ObjectIdentifier(view)) else {return}
        if view === web, let panelIndex=CommandLine.arguments.firstIndex(of:"--verify-panel-agent"),CommandLine.arguments.count>panelIndex+1 {
            let agent=CommandLine.arguments[panelIndex+1]
            let messageIndex=CommandLine.arguments.firstIndex(of:"--verify-message-id")
            let message=(messageIndex != nil && CommandLine.arguments.count>messageIndex!+1) ? CommandLine.arguments[messageIndex!+1] : ""
            let args=try! JSONSerialization.data(withJSONObject:[agent,message]);let json=String(data:args,encoding:.utf8)!
            let script="""
            (()=>{const [agent,message]=\(json);if(!window.WorkflowDesk)return JSON.stringify({ready:false});const current=document.querySelector('.wf-session-frame');if(!current||new URL(current.src).searchParams.get('agent')!==agent){window.WorkflowDesk.select(agent);return JSON.stringify({ready:false});}const d=current.contentDocument;if(!d||!d.querySelector('.message'))return JSON.stringify({ready:false});const row=message?d.querySelector('[data-entry-id="'+message+'"]'):d.querySelector('.message');if(!row){const older=d.getElementById('older');if(older&&!older.hidden&&!older.disabled)older.click();return JSON.stringify({ready:false});}row.scrollIntoView({block:'center'});const image=row.querySelector('.attachment-card img');return JSON.stringify({ready:!image||(image.complete&&image.naturalWidth>0),panelAgent:agent,panelMessage:row.dataset.entryId,panelMessages:d.querySelectorAll('.message').length,panelImageLoaded:!!image&&image.naturalWidth>0,popups:document.querySelectorAll('.wf-session-frame').length-1,zoom:document.getElementById('zoom-slider')?.value});})()
            """
            view.evaluateJavaScript(script) { value,error in
                let raw=(value as? String) ?? "{}";let result=(try? JSONSerialization.jsonObject(with:Data(raw.utf8))) as? [String:Any] ?? [:]
                if result["ready"] as? Bool != true && attempt<120 { DispatchQueue.main.asyncAfter(deadline:.now()+0.5){self.capture(view,prefix:prefix,attempt:attempt+1)};return }
                self.captureResult(view,prefix:prefix,extra:result)
            };return
        }
        view.evaluateJavaScript("JSON.stringify({title:document.title,url:location.href,lanes:document.querySelectorAll('.lane').length,sessions:document.querySelectorAll('.session').length,messages:document.querySelectorAll('.message').length,agent:new URLSearchParams(location.search).get('agent'),taskflowReady:!!window.SwitchboardTasks,tasks:window.SwitchboardTasks?.snapshot.tasks.length||0,taskIcons:document.querySelectorAll('.tf-task-icon').length,individualPaths:document.querySelectorAll('[data-agent-path]').length,attentionTasks:document.querySelectorAll('.tf-task-icon[data-stuck=true]').length,ready:location.pathname==='/session'?document.getElementById('state')?.textContent==='Connected':document.querySelectorAll('.lane').length>0&&!!window.SwitchboardTasks&&document.querySelectorAll('[data-agent-path]').length>0,error:document.getElementById('error')?.hidden===false?document.getElementById('error').textContent:null})") { value,error in
            let output=(value as? String) ?? "{}"
            let parsed=(try? JSONSerialization.jsonObject(with:Data(output.utf8))) as? [String:Any]
            if parsed?["ready"] as? Bool != true && attempt<40 {DispatchQueue.main.asyncAfter(deadline:.now()+0.5){self.capture(view,prefix:prefix,attempt:attempt+1)};return}
            self.captures.insert(ObjectIdentifier(view));var record=parsed ?? [:];record["nativeWindowFrame"]=view.window.map { NSStringFromRect($0.frame) };record["nativeWindowKey"]=view.window?.isKeyWindow
            if let bytes=try? JSONSerialization.data(withJSONObject:record,options:[.sortedKeys]) {try? bytes.write(to:URL(fileURLWithPath:prefix+".json"),options:.atomic)}
            view.takeSnapshot(with:nil) { image,error in if let tiff=image?.tiffRepresentation,let bitmap=NSBitmapImageRep(data:tiff),let png=bitmap.representation(using:.png,properties:[:]) {try? png.write(to:URL(fileURLWithPath:prefix+".png"))} }
            if view === self.web,let idx=CommandLine.arguments.firstIndex(of:"--verify-session"),CommandLine.arguments.count>idx+1 {
                let agent=CommandLine.arguments[idx+1];let encoded=agent.addingPercentEncoding(withAllowedCharacters:.urlQueryAllowed) ?? ""
                let arg=try? JSONSerialization.data(withJSONObject:["/session?agent="+encoded]);if let arg=arg,let json=String(data:arg,encoding:.utf8) {self.openingVerificationPopup=true;view.evaluateJavaScript("window.open("+json+"[0], 'verify-session', 'popup,width=920,height=830')") { _,_ in self.openingVerificationPopup=false }}
            }
        }
    }


    func captureResult(_ view:WKWebView,prefix:String,extra:[String:Any]) {
        captures.insert(ObjectIdentifier(view))
        view.evaluateJavaScript("JSON.stringify({title:document.title,url:location.href,lanes:document.querySelectorAll('.lane').length,sessions:document.querySelectorAll('.session').length,error:document.getElementById('error')?.hidden===false?document.getElementById('error').textContent:null})") { value,_ in
            var record=((value as? String).flatMap { try? JSONSerialization.jsonObject(with:Data($0.utf8)) } as? [String:Any]) ?? [:]
            record.merge(extra){_,new in new};record["nativeWindowFrame"]=view.window.map{NSStringFromRect($0.frame)};record["nativeWindowKey"]=view.window?.isKeyWindow
            if let bytes=try? JSONSerialization.data(withJSONObject:record,options:[.sortedKeys]) {try? bytes.write(to:URL(fileURLWithPath:prefix+".json"),options:.atomic)}
            view.takeSnapshot(with:nil){image,_ in if let tiff=image?.tiffRepresentation,let bitmap=NSBitmapImageRep(data:tiff),let png=bitmap.representation(using:.png,properties:[:]) {try? png.write(to:URL(fileURLWithPath:prefix+".png"))}}
        }
    }

}
let app=NSApplication.shared
let delegate=AppDelegate()
app.setActivationPolicy(.regular);app.delegate=delegate;app.run()
