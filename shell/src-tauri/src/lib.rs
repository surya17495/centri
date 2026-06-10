// CENTRI desktop shell entry point. The window simply hosts the Vite-built
// React frontend; all backend communication happens from the webview over
// HTTP/WebSocket, so the Rust side stays thin.

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default();

    // Global shortcut stub: registered on desktop so a future "summon window"
    // hotkey has a home. Wiring the handler to focus/toggle the window is left
    // for a later phase; for now we just install the plugin.
    #[cfg(desktop)]
    let builder = builder.plugin(tauri_plugin_global_shortcut::Builder::new().build());

    builder
        .run(tauri::generate_context!())
        .expect("error while running CENTRI shell");
}
