//! Hope Desktop shell — wake-triggered dashboard.
//!
//! This is a slim rewrite of the previous HTTP-backed desktop: the old
//! Ollama bootstrap + `hope serve` FastAPI pipeline is gone.  The only
//! things this shell does now are:
//!
//! 1. Launch hidden (macOS autostart at login), live in the system tray.
//! 2. Connect to the Hope daemon's WebSocket bridge on
//!    `ws://127.0.0.1:8765` and re-emit events to the frontend via
//!    [`tauri::AppHandle::emit`].
//! 3. Show the window on `wake_trigger` and hide it when the brain goes
//!    to sleep (`pane_killed` for hope-main) or when the control socket
//!    replies to `sleep`.
//! 4. Expose a Cmd+Shift+H global shortcut that manually toggles the
//!    window.
//! 5. Expose thin Tauri commands (`send_daemon_control`, `tail_daemon_log`,
//!    `subscribe_dashboard`) that bridge the frontend to the daemon's
//!    existing Unix control socket and log file.

use std::fs::OpenOptions;
use std::io::{Read, Seek, SeekFrom, Write};
use std::net::TcpStream;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_autostart::MacosLauncher;
use tauri_plugin_global_shortcut::{
    Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState,
};

const DASHBOARD_HOST: &str = "127.0.0.1";
const DEFAULT_DASHBOARD_PORT: u16 = 8765;

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

fn home_dir() -> String {
    std::env::var("HOME").unwrap_or_default()
}

fn daemon_socket_path() -> PathBuf {
    PathBuf::from(home_dir()).join(".hope").join("daemon.sock")
}

fn daemon_log_path() -> PathBuf {
    PathBuf::from(home_dir()).join(".hope").join("daemon.log")
}

// ---------------------------------------------------------------------------
// Daemon lifecycle — Hope.app owns the Python daemon. Open the app →
// daemon spawns. Quit the app (Cmd-Q / tray Quit) → daemon dies.
// Closing the WINDOW only hides it; daemon keeps listening in the
// background so wake words still work.
// ---------------------------------------------------------------------------

/// Holds the daemon Child handle so we can kill it on app exit.
struct DaemonHandle(Mutex<Option<Child>>);

/// Returns true if something is already listening on the dashboard
/// bridge port — usually means a daemon is already running (e.g. left
/// over from launchd or a stuck previous instance). We avoid double-
/// spawning in that case; the user can `pkill -f "hope start"` if they
/// want a clean slate.
fn is_daemon_running() -> bool {
    TcpStream::connect_timeout(
        &format!("{}:{}", DASHBOARD_HOST, DEFAULT_DASHBOARD_PORT)
            .parse()
            .expect("static addr parses"),
        Duration::from_millis(300),
    )
    .is_ok()
}

/// Spawn the Python daemon as a child process. Logs piped to the same
/// files the launchd plist used (`~/.hope/daemon-launchd.{out,err}.log`)
/// so existing tooling that tails them keeps working.
fn spawn_daemon() -> Result<Child, String> {
    let home = home_dir();
    let venv_hope = format!("{}/Documents/Github/Hope/.venv/bin/hope", &home);

    let out_log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(format!("{}/.hope/daemon-launchd.out.log", &home))
        .map_err(|e| format!("open out_log: {}", e))?;
    let err_log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(format!("{}/.hope/daemon-launchd.err.log", &home))
        .map_err(|e| format!("open err_log: {}", e))?;

    Command::new(&venv_hope)
        .args(["start", "--foreground"])
        .env("HOME", &home)
        .env(
            "PATH",
            format!(
                "{}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                &home
            ),
        )
        .env("CLAUDE_FLOW_MEMORY_DB", format!("{}/.hope/memory.db", &home))
        .env("HOPE_VOICE", "bf_isabella")
        // Daemon polls this PID and SIGTERMs itself if Hope.app dies.
        // Belt for the SIGINT/crash/kill -9 cases where Tauri's
        // RunEvent::ExitRequested doesn't fire and shutdown_daemon() is
        // bypassed. Suspenders below: ExitRequested still kills directly.
        .env("HOPE_PARENT_PID", std::process::id().to_string())
        .current_dir(format!("{}/Documents/Github/Hope", &home))
        .stdout(Stdio::from(out_log))
        .stderr(Stdio::from(err_log))
        .spawn()
        .map_err(|e| format!("spawn daemon: {}", e))
}

/// Tell an already-running daemon (one we don't own — usually a leftover
/// from a previous Hope.app session, the disabled launchd plist, or a
/// manual `hope start` for testing) to shut itself down via the Unix
/// control socket. Polls until the bridge port is free again, capped at
/// `max_wait`. Returns Err on socket failure or wait timeout.
///
/// The daemon's `stop` command runs `HopeDaemon.shutdown()` on a worker
/// thread (closes the tmux session, kills specialist panes, drops the
/// dashboard bridge, removes the PID file) and exits the Python process.
/// On a healthy daemon this is well under a second; we give 5 s of
/// headroom for slow machines / paused brains.
fn stop_existing_daemon() -> Result<(), String> {
    // Best-effort send. If the socket is missing/unreachable we still
    // try the port-clear poll below — maybe the daemon is in a half-up
    // state where the control socket is gone but the bridge port is
    // still bound by a zombie listener.
    let sent = send_control_sync("stop", None);
    match sent {
        Ok(_) => eprintln!("[hope-desktop] sent stop to existing daemon"),
        Err(e) => eprintln!(
            "[hope-desktop] stop command failed ({}); polling port anyway",
            e,
        ),
    }
    let max_wait = Duration::from_secs(5);
    let start = std::time::Instant::now();
    loop {
        if !is_daemon_running() {
            eprintln!(
                "[hope-desktop] :{} cleared after {} ms",
                DEFAULT_DASHBOARD_PORT,
                start.elapsed().as_millis(),
            );
            return Ok(());
        }
        if start.elapsed() > max_wait {
            return Err(format!(
                ":{} still bound after {} s",
                DEFAULT_DASHBOARD_PORT,
                max_wait.as_secs(),
            ));
        }
        std::thread::sleep(Duration::from_millis(150));
    }
}

/// Send SIGTERM, wait up to 3 s for graceful shutdown, then SIGKILL as
/// last resort. The graceful path lets the daemon close its tmux
/// session, brain panes, and SQLite connections cleanly.
fn shutdown_daemon(state: &DaemonHandle) {
    let mut guard = match state.0.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    let mut child = match guard.take() {
        Some(c) => c,
        None => return, // never spawned (was already running)
    };
    let pid = child.id();
    // Graceful SIGTERM via shell — avoids pulling libc/nix as a dep.
    let _ = Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status();
    let start = std::time::Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return, // exited cleanly
            Err(_) => return,      // already gone
            Ok(None) => {}
        }
        if start.elapsed() > Duration::from_secs(3) {
            let _ = child.kill();
            let _ = child.wait();
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

// ---------------------------------------------------------------------------
// Window management
// ---------------------------------------------------------------------------

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
        let _ = window.emit("hope:window-shown", ());
    }
}

fn hide_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
        let _ = window.emit("hope:window-hidden", ());
    }
}

fn toggle_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        if window.is_visible().unwrap_or(false) {
            hide_main_window(app);
        } else {
            show_main_window(app);
        }
    }
}

// ---------------------------------------------------------------------------
// Unix control-socket client — talks to ``~/.hope/daemon.sock``
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Debug)]
struct ControlRequest {
    cmd: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    payload: Option<serde_json::Value>,
}

fn send_control_sync(
    cmd: &str,
    payload: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    let path = daemon_socket_path();
    if !path.exists() {
        return Err(format!(
            "daemon control socket missing at {} — is the Hope daemon running?",
            path.display(),
        ));
    }
    let mut stream = UnixStream::connect(&path)
        .map_err(|e| format!("connect {}: {}", path.display(), e))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .map_err(|e| format!("set_read_timeout: {}", e))?;
    stream
        .set_write_timeout(Some(Duration::from_secs(3)))
        .map_err(|e| format!("set_write_timeout: {}", e))?;

    let req = ControlRequest {
        cmd: cmd.into(),
        payload,
    };
    let line = serde_json::to_string(&req).map_err(|e| e.to_string())? + "\n";
    stream
        .write_all(line.as_bytes())
        .map_err(|e| format!("write: {}", e))?;
    let _ = stream.shutdown(std::net::Shutdown::Write);

    let mut buf = String::new();
    stream
        .read_to_string(&mut buf)
        .map_err(|e| format!("read: {}", e))?;
    let first = buf.split('\n').next().unwrap_or("");
    serde_json::from_str(first).map_err(|e| format!("parse response: {}", e))
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
async fn send_daemon_control(
    cmd: String,
    payload: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    tokio::task::spawn_blocking(move || send_control_sync(&cmd, payload))
        .await
        .map_err(|e| e.to_string())?
}

/// Tail the last *max_bytes* of ``~/.hope/daemon.log`` as UTF-8 text.
#[tauri::command]
async fn tail_daemon_log(max_bytes: Option<u64>) -> Result<String, String> {
    let path = daemon_log_path();
    let limit = max_bytes.unwrap_or(32 * 1024);
    tokio::task::spawn_blocking(move || -> Result<String, String> {
        let mut file = std::fs::File::open(&path)
            .map_err(|e| format!("open {}: {}", path.display(), e))?;
        let size = file
            .metadata()
            .map_err(|e| e.to_string())?
            .len();
        let start = size.saturating_sub(limit);
        file.seek(SeekFrom::Start(start))
            .map_err(|e| e.to_string())?;
        let mut buf = Vec::with_capacity(limit as usize);
        file.read_to_end(&mut buf).map_err(|e| e.to_string())?;
        Ok(String::from_utf8_lossy(&buf).into_owned())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn dashboard_endpoint() -> serde_json::Value {
    serde_json::json!({
        "host": DASHBOARD_HOST,
        "port": DEFAULT_DASHBOARD_PORT,
        "url": format!("ws://{}:{}", DASHBOARD_HOST, DEFAULT_DASHBOARD_PORT),
    })
}

#[tauri::command]
fn show_window(app: AppHandle) {
    show_main_window(&app);
}

#[tauri::command]
fn hide_window(app: AppHandle) {
    hide_main_window(&app);
}

// ---------------------------------------------------------------------------
// Dashboard bridge client — connects to the daemon's WS, re-emits frames
// ---------------------------------------------------------------------------

/// Payload the frontend receives via ``@tauri-apps/api/event`` on the
/// ``hope:event`` channel. Mirrors the JSON envelope that
/// :mod:`hope.daemon.dashboard_bridge` broadcasts.
#[derive(Serialize, Deserialize, Clone, Debug)]
struct BridgeEvent {
    #[serde(rename = "type")]
    event_type: String,
    timestamp: f64,
    data: serde_json::Value,
}

fn apply_event_side_effects(app: &AppHandle, event: &BridgeEvent) {
    match event.event_type.as_str() {
        "wake_trigger" => {
            // Deliberately no show_main_window() here. The React handler
            // in store.ts decides whether to bring the window forward
            // (gated on `listeningPaused` so wake never snatches focus
            // when the user has muted Hope). If the webview isn't open
            // we leave the dashboard alone — user can pop it from the
            // tray, dock, or Cmd+Shift+H.
        }
        "pane_killed" => {
            // Hide the window only when the hope-main pane dies — specialists
            // come and go without changing window visibility.
            let is_main = event
                .data
                .get("role")
                .and_then(|v| v.as_str())
                .map(|s| s == "hope-main")
                .unwrap_or(false)
                || event
                    .data
                    .get("pane_name")
                    .and_then(|v| v.as_str())
                    .map(|s| s == "hope-main")
                    .unwrap_or(false);
            if is_main {
                hide_main_window(app);
            }
        }
        _ => {}
    }
}

/// Long-running task: connect, read text frames, re-emit as Tauri events,
/// reconnect with backoff on error.
async fn run_bridge_client(app: AppHandle, host: String, port: u16) {
    let mut backoff = Duration::from_millis(500);
    let max_backoff = Duration::from_secs(5);
    loop {
        match connect_and_pump(&app, &host, port).await {
            Ok(()) => {
                backoff = Duration::from_millis(500);
            }
            Err(e) => {
                let _ = app.emit(
                    "hope:bridge-status",
                    serde_json::json!({ "connected": false, "error": e }),
                );
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(max_backoff);
            }
        }
    }
}

async fn connect_and_pump(app: &AppHandle, host: &str, port: u16) -> Result<(), String> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpStream;

    let addr = format!("{}:{}", host, port);
    let mut stream = TcpStream::connect(&addr)
        .await
        .map_err(|e| format!("tcp connect {}: {}", addr, e))?;

    // Tiny hand-written RFC 6455 client handshake — matches what
    // ``dashboard_bridge.py`` expects.
    use base64::Engine;
    let raw_key: [u8; 16] = rand::random();
    let key = base64::engine::general_purpose::STANDARD.encode(raw_key);
    let request = format!(
        "GET / HTTP/1.1\r\n\
         Host: {host}:{port}\r\n\
         Upgrade: websocket\r\n\
         Connection: Upgrade\r\n\
         Sec-WebSocket-Key: {key}\r\n\
         Sec-WebSocket-Version: 13\r\n\
         \r\n"
    );
    stream
        .write_all(request.as_bytes())
        .await
        .map_err(|e| format!("write handshake: {}", e))?;

    // Read until \r\n\r\n
    let mut headers = Vec::<u8>::with_capacity(512);
    let mut scratch = [0u8; 512];
    loop {
        let n = stream
            .read(&mut scratch)
            .await
            .map_err(|e| format!("read handshake: {}", e))?;
        if n == 0 {
            return Err("server closed during handshake".into());
        }
        headers.extend_from_slice(&scratch[..n]);
        if headers.windows(4).any(|w| w == b"\r\n\r\n") {
            break;
        }
        if headers.len() > 8192 {
            return Err("handshake headers too large".into());
        }
    }
    let hdr_str = String::from_utf8_lossy(&headers);
    if !hdr_str.starts_with("HTTP/1.1 101") {
        return Err(format!("handshake rejected: {}", hdr_str.lines().next().unwrap_or("")));
    }

    let _ = app.emit(
        "hope:bridge-status",
        serde_json::json!({ "connected": true }),
    );

    // Heartbeat: re-emit `connected: true` every 2 s while the connection
    // holds. React's listener in App.tsx mounts asynchronously after the
    // webview loads, so it usually misses the first emit above. The
    // heartbeat means a late subscriber catches up within 2 s. The task
    // is aborted via the RAII guard below as soon as this fn returns
    // (Err or normal close), so we never falsely emit connected after the
    // socket is gone.
    let hb_app = app.clone();
    let heartbeat = tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(2));
        // First tick fires immediately — swallow it so the heartbeat
        // starts at +2 s, not +0.
        tick.tick().await;
        loop {
            tick.tick().await;
            let _ = hb_app.emit(
                "hope:bridge-status",
                serde_json::json!({ "connected": true }),
            );
        }
    });
    struct HeartbeatGuard(tokio::task::JoinHandle<()>);
    impl Drop for HeartbeatGuard {
        fn drop(&mut self) {
            self.0.abort();
        }
    }
    let _hb_guard = HeartbeatGuard(heartbeat);

    // Read loop — server → client frames have no mask.
    loop {
        let mut header = [0u8; 2];
        stream
            .read_exact(&mut header)
            .await
            .map_err(|e| format!("read frame header: {}", e))?;
        let opcode = header[0] & 0x0F;
        let len = (header[1] & 0x7F) as usize;
        let length = match len {
            126 => {
                let mut buf = [0u8; 2];
                stream.read_exact(&mut buf).await.map_err(|e| e.to_string())?;
                u16::from_be_bytes(buf) as usize
            }
            127 => {
                let mut buf = [0u8; 8];
                stream.read_exact(&mut buf).await.map_err(|e| e.to_string())?;
                u64::from_be_bytes(buf) as usize
            }
            n => n,
        };
        let mut payload = vec![0u8; length];
        if length > 0 {
            stream
                .read_exact(&mut payload)
                .await
                .map_err(|e| format!("read frame body: {}", e))?;
        }
        match opcode {
            0x1 => {
                if let Ok(parsed) = serde_json::from_slice::<BridgeEvent>(&payload) {
                    apply_event_side_effects(app, &parsed);
                    let _ = app.emit("hope:event", &parsed);
                }
            }
            0x8 => {
                return Err("server closed".into());
            }
            0x9 => {
                // ping → pong
                let mut reply = Vec::with_capacity(2 + payload.len());
                reply.push(0x80 | 0xA);
                reply.push(payload.len() as u8);
                reply.extend_from_slice(&payload);
                stream.write_all(&reply).await.ok();
            }
            _ => {}
        }
    }
}

// ---------------------------------------------------------------------------
// App entry point
// ---------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec!["--hidden"]),
        ))
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }))
        .setup(move |app| {
            // Hope.app is the sole owner of the daemon lifecycle:
            // open app → daemon up, quit app → daemon down. Closing
            // the window only hides the UI; the daemon stays running
            // until the app process exits.
            //
            // If a daemon is already on the bridge port we used to
            // leave it alone and run with DaemonHandle=None — but
            // that produced an orphaned daemon Hope.app couldn't
            // kill on Cmd-Q, which is the source of the "I quit
            // Hope but it's still listening" reports. Now we shut
            // any existing daemon down first, then spawn a fresh
            // child we own end-to-end. The launchd plist that used
            // to auto-respawn the daemon has been moved aside (see
            // ~/Library/LaunchAgents/com.hope.daemon.plist.disabled-
            // by-app-ownership) so we can't race it any more.
            if is_daemon_running() {
                eprintln!(
                    "[hope-desktop] daemon already on :{}, taking ownership \
                     (stop + respawn so Cmd-Q kills it cleanly)",
                    DEFAULT_DASHBOARD_PORT,
                );
                if let Err(e) = stop_existing_daemon() {
                    eprintln!(
                        "[hope-desktop] could not stop existing daemon: {} \
                         — continuing without owned child",
                        e,
                    );
                }
            }
            let daemon_child = match spawn_daemon() {
                Ok(c) => {
                    eprintln!("[hope-desktop] spawned daemon pid={}", c.id());
                    Some(c)
                }
                Err(e) => {
                    eprintln!("[hope-desktop] failed to spawn daemon: {}", e);
                    None
                }
            };
            app.manage(DaemonHandle(Mutex::new(daemon_child)));

            // Only hide the window for the autostart case — the macOS
            // launch-agent passes `--hidden` so Hope starts in the tray
            // and waits for a wake trigger. A *manual* launch (clicking
            // the dock icon, opening from Finder) should show the window
            // immediately; otherwise the click looks broken.
            let started_hidden = std::env::args().any(|a| a == "--hidden");
            if let Some(window) = app.get_webview_window("main") {
                if started_hidden {
                    let _ = window.hide();
                } else {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }

            // System tray — the only UI entry point until a wake fires.
            let show_item = MenuItemBuilder::with_id("show", "Show Hope").build(app)?;
            let hide_item = MenuItemBuilder::with_id("hide", "Hide Hope").build(app)?;
            let wake_item = MenuItemBuilder::with_id("wake", "Wake").build(app)?;
            let sleep_item = MenuItemBuilder::with_id("sleep", "Sleep").build(app)?;
            let quit_item = MenuItemBuilder::with_id("quit", "Quit").build(app)?;
            let menu = MenuBuilder::new(app)
                .item(&show_item)
                .item(&hide_item)
                .separator()
                .item(&wake_item)
                .item(&sleep_item)
                .separator()
                .item(&quit_item)
                .build()?;

            let _tray = TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Hope")
                .menu(&menu)
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "show" => show_main_window(app),
                    "hide" => hide_main_window(app),
                    "wake" => {
                        let _ = send_control_sync(
                            "wake",
                            Some(serde_json::json!({"source": "tray"})),
                        );
                    }
                    "sleep" => {
                        let _ = send_control_sync("sleep", None);
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            // Cmd+Shift+H — manual toggle even when the daemon is down.
            let app_handle_for_sc = app.handle().clone();
            let shortcut = Shortcut::new(
                Some(Modifiers::META | Modifiers::SHIFT),
                Code::KeyH,
            );
            if let Err(e) = app
                .global_shortcut()
                .on_shortcut(shortcut, move |_app, _sc, ev| {
                    if ev.state == ShortcutState::Pressed {
                        toggle_main_window(&app_handle_for_sc);
                    }
                })
            {
                eprintln!("warning: could not register Cmd+Shift+H: {e}");
            }

            // Spawn the dashboard bridge client.
            let bridge_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                run_bridge_client(
                    bridge_handle,
                    DASHBOARD_HOST.into(),
                    DEFAULT_DASHBOARD_PORT,
                )
                .await;
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            send_daemon_control,
            tail_daemon_log,
            dashboard_endpoint,
            show_window,
            hide_window,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Hope Desktop")
        .run(|app, event| match event {
            // macOS dock click on an already-running Hope sends Reopen.
            // Without this handler the click does nothing because the
            // main window starts hidden (revealed only on wake). When
            // there are no visible windows, treat the dock click as
            // "Show Hope".
            tauri::RunEvent::Reopen {
                has_visible_windows,
                ..
            } => {
                if !has_visible_windows {
                    show_main_window(app);
                }
            }
            // Cmd-Q / tray "Quit" / app.exit() — gracefully kill the
            // daemon child before the app process exits. Without this
            // the daemon (and its tmux + claude subprocesses) would
            // outlive the app, which is what Joel called out.
            tauri::RunEvent::ExitRequested { .. } => {
                if let Some(state) = app.try_state::<DaemonHandle>() {
                    eprintln!("[hope-desktop] app exiting — shutting down daemon");
                    shutdown_daemon(&state);
                }
            }
            _ => {}
        });
}
