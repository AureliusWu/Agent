use rand::{distr::Alphanumeric, Rng};
use serde::Serialize;
use std::{
    fs,
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Manager, RunEvent, State};
use tauri_plugin_log::{Target, TargetKind};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};
mod process_identity;
mod acceptance_capture;
#[cfg(windows)]
mod acceptance_native_file;
use acceptance_capture::AcceptanceCapture;
#[cfg(target_os = "windows")]
use process_identity::ProcessIdentity;

const SERVICE: &str = "AureliusWu.Agent";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_AUTO_RESTARTS: u8 = 2;

struct BackendProcess {
    pid: u32,
    child: CommandChild,
    #[cfg(target_os = "windows")]
    identity: Option<ProcessIdentity>,
}

#[derive(Clone, Serialize)]
struct BackendHealth {
    epoch: u64,
    port: Option<u16>,
    pid: Option<u32>,
    ready: bool,
    phase: String,
    error: Option<String>,
    restart_count: u8,
    log_directory: String,
}

struct BackendRuntimeState {
    process: Option<BackendProcess>,
    health: BackendHealth,
    data_directory: PathBuf,
    api_token: String,
    shutting_down: bool,
    generation: u64,
}

struct BackendRuntime(Mutex<BackendRuntimeState>);

fn process_generation_matches(
    current_generation: u64,
    generation: u64,
    current_pid: Option<u32>,
    pid: u32,
) -> bool {
    current_generation == generation && current_pid == Some(pid)
}

fn mark_backend_start(health: &mut BackendHealth, epoch: u64, port: u16) {
    health.epoch = epoch;
    health.port = Some(port);
    health.pid = None;
    health.ready = false;
    health.phase = if health.restart_count > 0 {
        "restarting"
    } else {
        "starting"
    }
    .to_string();
    health.error = None;
}

fn mark_backend_exit(health: &mut BackendHealth, shutting_down: bool, code: Option<i32>) -> bool {
    health.pid = None;
    health.ready = false;
    if shutting_down {
        health.phase = "stopped".to_string();
        health.error = None;
        false
    } else if health.restart_count < MAX_AUTO_RESTARTS {
        health.restart_count += 1;
        health.phase = "restarting".to_string();
        health.error = Some(format!("本地核心异常退出（代码 {code:?}），正在自动重启"));
        true
    } else {
        health.phase = "error".to_string();
        health.error = Some(format!(
            "本地核心连续异常退出（代码 {code:?}），请查看日志后手动重启"
        ));
        false
    }
}

fn available_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|error| error.to_string())?;
    listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|error| error.to_string())
}

fn local_http_request(
    port: u16,
    method: &str,
    path: &str,
    token: &str,
    timeout: Duration,
) -> Result<(u16, String), String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream =
        TcpStream::connect_timeout(&address, timeout).map_err(|error| error.to_string())?;
    stream
        .set_read_timeout(Some(timeout))
        .map_err(|error| error.to_string())?;
    stream
        .set_write_timeout(Some(timeout))
        .map_err(|error| error.to_string())?;
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Agent-Api-Token: {token}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|error| error.to_string())?;
    let deadline = Instant::now() + timeout;
    let mut bytes = Vec::new();
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Err("Local HTTP response exceeded its deadline".into()); }
        stream.set_read_timeout(Some(remaining)).map_err(|error| error.to_string())?;
        let mut block = [0u8; 8192];
        let count = stream.read(&mut block).map_err(|error| error.to_string())?;
        if count == 0 { break; }
        if bytes.len() + count > 1024 * 1024 { return Err("Local HTTP response exceeded 1 MiB".into()); }
        bytes.extend_from_slice(&block[..count]);
    }
    let response = String::from_utf8(bytes).map_err(|_| "Local HTTP response is not UTF-8")?;
    let (headers, body) = response
        .split_once("\r\n\r\n")
        .ok_or_else(|| "本地核心返回了无效 HTTP 响应".to_string())?;
    let status = headers
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or_else(|| "本地核心返回了无效状态码".to_string())?;
    Ok((status, body.to_string()))
}

fn backend_is_ready(port: u16, token: &str, timeout: Duration) -> bool {
    matches!(
        local_http_request(port, "GET", "/api/desktop/status", token, timeout),
        Ok((200, body)) if body.contains("\"status\":\"ok\"")
    )
}

fn agent_data_directory(local_data: &Path, configured: Option<PathBuf>) -> PathBuf {
    configured.unwrap_or_else(|| local_data.join("AureliusWu").join("Agent"))
}

fn update_start_failure(runtime: &BackendRuntime, generation: u64, message: String) {
    if let Ok(mut state) = runtime.0.lock() {
        if state.generation == generation {
            state.process = None;
            state.health.pid = None;
            state.health.ready = false;
            state.health.phase = "error".to_string();
            state.health.error = Some(message);
        }
    }
}

fn start_backend(app: &AppHandle) -> Result<BackendHealth, String> {
    let port = available_port()?;
    let runtime = app.state::<BackendRuntime>();
    let (data_directory, api_token, generation) = {
        let mut state = runtime
            .0
            .lock()
            .map_err(|_| "本地核心状态锁异常".to_string())?;
        if state.shutting_down {
            return Err("应用正在退出，不能启动本地核心".to_string());
        }
        if state.process.is_some() {
            return Ok(state.health.clone());
        }
        state.generation += 1;
        let generation = state.generation;
        mark_backend_start(&mut state.health, generation, port);
        (
            state.data_directory.clone(),
            state.api_token.clone(),
            state.generation,
        )
    };

    let (mut events, child) = match app.shell().sidecar("agent-backend") {
        Ok(command) => command
            .env("AGENT_PORT", port.to_string())
            .env("AGENT_PARENT_PID", std::process::id().to_string())
            .env("AGENT_DEPLOYMENT_MODE", "desktop_local")
            .env("AGENT_RUNTIME_ENV", "production")
            .env("AGENT_DATA_ROOT", &data_directory)
            .env("AGENT_BIND_HOST", "127.0.0.1")
            .env("AGENT_API_TOKEN", &api_token)
            .env(
                "AGENT_DATABASE_PATH",
                data_directory.join("data").join("agent.db"),
            )
            .env(
                "AGENT_LOG_PATH",
                data_directory.join("logs").join("agent.log"),
            )
            .env(
                "AGENT_EXTENSION_DIRECTORY",
                data_directory.join("extensions"),
            )
            .spawn()
            .map_err(|error| format!("本地核心启动失败：{error}")),
        Err(error) => Err(format!("本地核心配置错误：{error}")),
    }
    .inspect_err(|message| {
        update_start_failure(&runtime, generation, message.clone());
    })?;

    let pid = child.pid();
    #[cfg(target_os = "windows")]
    let identity = ProcessIdentity::capture(pid);
    {
        let mut state = runtime
            .0
            .lock()
            .map_err(|_| "本地核心状态锁异常".to_string())?;
        if state.generation != generation || state.shutting_down {
            let _ = child.kill();
            return Err("本地核心启动已取消".to_string());
        }
        state.health.pid = Some(pid);
        state.process = Some(BackendProcess {
            pid,
            child,
            #[cfg(target_os = "windows")]
            identity,
        });
    }
    log::info!("desktop sidecar spawned pid={pid} port={port}");

    let readiness_app = app.clone();
    let readiness_token = api_token.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let started = Instant::now();
        while started.elapsed() < STARTUP_TIMEOUT {
            if backend_is_ready(port, &readiness_token, Duration::from_millis(500)) {
                if let Some(runtime) = readiness_app.try_state::<BackendRuntime>() {
                    if let Ok(mut state) = runtime.0.lock() {
                        let current = process_generation_matches(
                            state.generation,
                            generation,
                            state.process.as_ref().map(|process| process.pid),
                            pid,
                        );
                        if current {
                            state.health.ready = true;
                            state.health.phase = "ready".to_string();
                            state.health.error = None;
                            log::info!("desktop sidecar ready pid={pid} port={port}");
                        }
                    }
                }
                return;
            }
            thread::sleep(Duration::from_millis(150));
        }
        if let Some(runtime) = readiness_app.try_state::<BackendRuntime>() {
            if let Ok(mut state) = runtime.0.lock() {
                let current = process_generation_matches(
                    state.generation,
                    generation,
                    state.process.as_ref().map(|process| process.pid),
                    pid,
                );
                if current && !state.shutting_down {
                    state.health.ready = false;
                    state.health.phase = "error".to_string();
                    state.health.error =
                        Some("本地核心在 30 秒内未就绪，请查看日志或重启核心".to_string());
                    log::error!("desktop sidecar readiness timed out pid={pid} port={port}");
                }
            }
        }
    });

    let events_app = app.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let message = String::from_utf8_lossy(&line);
                    log::info!(target: "agent_sidecar", "{}", message.trim());
                }
                CommandEvent::Stderr(line) => {
                    let message = String::from_utf8_lossy(&line);
                    log::warn!(target: "agent_sidecar", "{}", message.trim());
                }
                CommandEvent::Error(error) => {
                    log::error!(target: "agent_sidecar", "{error}");
                }
                CommandEvent::Terminated(payload) => {
                    let should_restart =
                        if let Some(runtime) = events_app.try_state::<BackendRuntime>() {
                            match runtime.0.lock() {
                                Ok(mut state) => {
                                    let current = process_generation_matches(
                                        state.generation,
                                        generation,
                                        state.process.as_ref().map(|process| process.pid),
                                        pid,
                                    );
                                    if !current {
                                        false
                                    } else {
                                        state.process = None;
                                        let shutting_down = state.shutting_down;
                                        mark_backend_exit(
                                            &mut state.health,
                                            shutting_down,
                                            payload.code,
                                        )
                                    }
                                }
                                Err(_) => false,
                            }
                        } else {
                            false
                        };
                    log::warn!(
                        "desktop sidecar terminated pid={pid} code={:?}",
                        payload.code
                    );
                    if should_restart {
                        let restart_app = events_app.clone();
                        tauri::async_runtime::spawn_blocking(move || {
                            thread::sleep(Duration::from_millis(600));
                            if let Err(error) = start_backend(&restart_app) {
                                log::error!("desktop sidecar automatic restart failed: {error}");
                            }
                        });
                    }
                    break;
                }
                _ => {}
            }
        }
    });

    let state = runtime
        .0
        .lock()
        .map_err(|_| "本地核心状态锁异常".to_string())?;
    Ok(state.health.clone())
}

fn stop_backend(runtime: &BackendRuntime, final_shutdown: bool) {
    let (port, token) = match runtime.0.lock() {
        Ok(mut state) => {
            state.shutting_down = true;
            (state.health.port, state.api_token.clone())
        }
        Err(_) => return,
    };
    if let Some(port) = port {
        match local_http_request(
            port,
            "POST",
            "/api/desktop/shutdown",
            &token,
            Duration::from_secs(2),
        ) {
            Ok((202, _)) => log::info!("desktop sidecar accepted graceful shutdown"),
            Ok((status, _)) => {
                log::warn!("desktop sidecar rejected graceful shutdown: HTTP {status}")
            }
            Err(error) => log::warn!("desktop sidecar graceful shutdown request failed: {error}"),
        }
    }

    let started = Instant::now();
    while started.elapsed() < SHUTDOWN_TIMEOUT {
        let stopped = runtime
            .0
            .lock()
            .map(|state| state.process.is_none())
            .unwrap_or(true);
        if stopped {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }

    let fallback = runtime
        .0
        .lock()
        .ok()
        .and_then(|mut state| state.process.take());
    if let Some(process) = fallback {
        log::error!(
            "desktop sidecar did not stop gracefully; forcing pid={}",
            process.pid
        );
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            if process
                .identity
                .as_ref()
                .is_some_and(ProcessIdentity::is_current_and_running)
            {
                // Keep the original process handle alive until taskkill returns: never target a reused PID.
                let _ = std::process::Command::new("taskkill")
                    .creation_flags(0x0800_0000)
                    .args(["/PID", &process.pid.to_string(), "/T", "/F"])
                    .status();
            } else {
                log::warn!(
                    "sidecar tree termination skipped: process identity unavailable or exited"
                );
            }
        }
        let _ = process.child.kill();
    }

    if let Ok(mut state) = runtime.0.lock() {
        state.health.pid = None;
        state.health.ready = false;
        state.health.phase = "stopped".to_string();
        if !final_shutdown {
            state.shutting_down = false;
        }
    }
}

#[tauri::command]
fn backend_status(state: State<'_, BackendRuntime>) -> Result<BackendHealth, String> {
    let (snapshot, token, generation, has_process) = {
        let runtime = state
            .0
            .lock()
            .map_err(|_| "本地核心状态锁异常".to_string())?;
        (
            runtime.health.clone(),
            runtime.api_token.clone(),
            runtime.generation,
            runtime.process.is_some(),
        )
    };
    let Some(port) = snapshot.port else {
        return Ok(snapshot);
    };
    if !has_process {
        return Ok(snapshot);
    }

    let ready = backend_is_ready(port, &token, Duration::from_millis(350));
    let mut runtime = state
        .0
        .lock()
        .map_err(|_| "本地核心状态锁异常".to_string())?;
    if runtime.generation == generation {
        runtime.health.ready = ready;
        if ready {
            runtime.health.phase = "ready".to_string();
            runtime.health.error = None;
        } else if snapshot.ready {
            runtime.health.phase = "error".to_string();
            runtime.health.error = Some("本地核心连接已断开，正在等待恢复".to_string());
        }
    }
    Ok(runtime.health.clone())
}

#[tauri::command]
fn restart_backend(
    app: AppHandle,
    state: State<'_, BackendRuntime>,
) -> Result<BackendHealth, String> {
    stop_backend(&state, false);
    {
        let mut runtime = state
            .0
            .lock()
            .map_err(|_| "本地核心状态锁异常".to_string())?;
        runtime.health.restart_count = 0;
        runtime.health.error = None;
    }
    start_backend(&app)
}

#[tauri::command]
fn backend_api_token(state: State<'_, BackendRuntime>) -> Result<String, String> {
    state
        .0
        .lock()
        .map(|runtime| runtime.api_token.clone())
        .map_err(|_| "本地核心状态锁异常".to_string())
}

#[tauri::command]
fn desktop_build_info() -> Result<serde_json::Value, String> {
    serde_json::from_str(env!("SIYI_BUILD_INFO_JSON")).map_err(|error| error.to_string())
}

#[tauri::command]
fn record_desktop_acceptance(window: tauri::WebviewWindow, react: serde_json::Value, capture: State<'_, AcceptanceCapture>,
    state: State<'_, BackendRuntime>) -> Result<bool, String> {
    if capture.nonce.is_none() { return Ok(false); }
    let result = (|| -> Result<bool, String> {
    if window.label() != "main" { return Err("Acceptance requires the actual main desktop window".into()); }
    #[cfg(windows)]
    let window_handle = window.hwnd().map_err(|_| "Actual main window handle is unavailable")?.0 as usize as u64;
    #[cfg(not(windows))]
    let window_handle = 0;
    let (directory, port, token, pid, generation) = {
        let runtime = state.0.lock().map_err(|_| "Backend state is unavailable")?;
        if runtime.shutting_down || !runtime.health.ready { return Err("Actual sidecar is not ready".into()); }
        #[cfg(target_os = "windows")]
        if !runtime.process.as_ref().is_some_and(|process| process.identity.as_ref()
            .is_some_and(ProcessIdentity::is_current_and_running)) { return Err("Retained sidecar process has exited".into()); }
        (runtime.data_directory.clone(), runtime.health.port.ok_or("Sidecar port is unavailable")?,
            runtime.api_token.clone(), runtime.health.pid.ok_or("Sidecar process is unavailable")?, runtime.generation)
    };
    let (status, body) = local_http_request(port, "GET", "/api/diagnostics/status", &token, Duration::from_secs(2))?;
    if status != 200 || body.len() > 1024 * 1024 { return Err("Authenticated diagnostics are unavailable".into()); }
    let diagnostics: serde_json::Value = serde_json::from_str(&body).map_err(|_| "Diagnostics are invalid")?;
    let desktop = desktop_build_info()?;
    if diagnostics["database"]["schema_version"] != desktop["database_schema_version"]
        || diagnostics["database"]["status"] != "ok" { return Err("Actual database schema is not ready".into()); }
    let runtime = state.0.lock().map_err(|_| "Backend state is unavailable")?;
    if runtime.shutting_down || !runtime.health.ready || !process_generation_matches(runtime.generation, generation, runtime.health.pid, pid) {
        return Err("Sidecar changed during component observation".into());
    }
    #[cfg(target_os = "windows")]
    if !runtime.process.as_ref().is_some_and(|process| process.pid == pid && process.identity.as_ref()
        .is_some_and(ProcessIdentity::is_current_and_running)) { return Err("Retained sidecar process has exited".into()); }
    acceptance_capture::write_observation(&capture, &directory, &react, &desktop, &diagnostics["build"], pid, window_handle)
    })();
    if let Err(error) = &result { log::warn!("desktop acceptance observation failed: {error}"); }
    result
}

#[tauri::command]
fn record_desktop_renderer_diagnostic(kind: String, line: u32, column: u32,
    capture: State<'_, AcceptanceCapture>) -> Result<bool, String> {
    if capture.nonce.is_none() { return Ok(false); }
    // No exception messages, URLs, DOM text, user data or credentials are logged.
    if !matches!(kind.as_str(), "boot" | "dom_with_root" | "dom_empty_root" |
        "reference_error" | "type_error" | "javascript_error" | "asset_error" | "unhandled_rejection") {
        return Err("Renderer diagnostic category is invalid".into());
    }
    static COUNT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    if COUNT.fetch_update(std::sync::atomic::Ordering::Relaxed, std::sync::atomic::Ordering::Relaxed,
        |value| (value < 16).then_some(value + 1)).is_err() { return Ok(false); }
    log::info!("acceptance renderer diagnostic kind={kind} line={line} column={column}");
    Ok(true)
}

#[tauri::command]
fn set_secret(name: String, value: String) -> Result<(), String> {
    validate_secret_name(&name)?;
    if value.is_empty() || value.len() > 16_384 {
        return Err("密钥内容不能为空且不得超过 16 KiB".to_string());
    }
    keyring::Entry::new(SERVICE, &name)
        .map_err(|error| error.to_string())?
        .set_password(&value)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn get_secret(name: String) -> Result<Option<String>, String> {
    validate_secret_name(&name)?;
    let entry = keyring::Entry::new(SERVICE, &name).map_err(|error| error.to_string())?;
    match entry.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(error) => Err(error.to_string()),
    }
}

#[tauri::command]
fn delete_secret(name: String) -> Result<(), String> {
    validate_secret_name(&name)?;
    let entry = keyring::Entry::new(SERVICE, &name).map_err(|error| error.to_string())?;
    match entry.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(error) => Err(error.to_string()),
    }
}

fn validate_secret_name(name: &str) -> Result<(), String> {
    let valid = (3..=80).contains(&name.len())
        && name.bytes().all(|value| {
            value.is_ascii_lowercase() || value.is_ascii_digit() || b"._-".contains(&value)
        })
        && name.as_bytes()[0].is_ascii_lowercase();
    if valid {
        Ok(())
    } else {
        Err("密钥名称必须为 3-80 位小写字母、数字、点、下划线或连字符".to_string())
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let acceptance = AcceptanceCapture::from_environment();
    let acceptance_enabled = acceptance.nonce.is_some();
    let mut builder = tauri::Builder::default().manage(acceptance);
    if acceptance_enabled {
        builder = builder.plugin(tauri::plugin::Builder::<_, ()>::new("acceptance-diagnostics")
            .js_init_script(r#"(() => {
                if (window.top !== window) return;
                const send = (kind, line = 0, column = 0) => {
                    const invoke = window.__TAURI_INTERNALS__?.invoke;
                    if (invoke) void invoke('record_desktop_renderer_diagnostic', { kind, line, column }).catch(() => {});
                };
                send('boot');
                window.addEventListener('error', event => send(event.error?.name === 'ReferenceError' ? 'reference_error' :
                    event.error?.name === 'TypeError' ? 'type_error' : event.error ? 'javascript_error' : 'asset_error',
                    event.lineno || 0, event.colno || 0), true);
                window.addEventListener('unhandledrejection', () => send('unhandled_rejection'));
                window.addEventListener('DOMContentLoaded', () => send(document.getElementById('root')?.hasChildNodes() ? 'dom_with_root' : 'dom_empty_root'));
            })();"#).build());
    }
    let app = builder
        .on_page_load(|webview, payload| {
            if webview.try_state::<AcceptanceCapture>().is_some_and(|capture| capture.nonce.is_some()) {
                let route = if payload.url().host_str() == Some("localhost") && payload.url().port() == Some(5173) {
                    "development_server"
                } else if matches!(payload.url().host_str(), Some("tauri.localhost")) || payload.url().scheme() == "tauri" {
                    "packaged_assets"
                } else { "other" };
                log::info!("acceptance page load event={:?} route={route}", payload.event());
            }
        })
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            set_secret,
            get_secret,
            delete_secret,
            backend_status,
            backend_api_token,
            restart_backend,
            desktop_build_info,
            record_desktop_acceptance,
            record_desktop_renderer_diagnostic
        ])
        .setup(|app| {
            let data_directory = agent_data_directory(
                &app.path().local_data_dir()?,
                std::env::var_os("AGENT_DESKTOP_DATA_DIRECTORY").map(PathBuf::from),
            );
            let log_directory = data_directory.join("logs");
            fs::create_dir_all(&log_directory)?;
            app.handle().plugin(
                tauri_plugin_log::Builder::default()
                    .clear_targets()
                    .target(Target::new(TargetKind::Folder {
                        path: log_directory.clone(),
                        file_name: Some("siyi-shell".to_string()),
                    }))
                    .level(log::LevelFilter::Info)
                    .build(),
            )?;
            let api_token: String = rand::rng()
                .sample_iter(&Alphanumeric)
                .take(64)
                .map(char::from)
                .collect();
            app.manage(BackendRuntime(Mutex::new(BackendRuntimeState {
                process: None,
                health: BackendHealth {
                    epoch: 0,
                    port: None,
                    pid: None,
                    ready: false,
                    phase: "stopped".to_string(),
                    error: None,
                    restart_count: 0,
                    log_directory: log_directory.to_string_lossy().to_string(),
                },
                data_directory,
                api_token,
                shutting_down: false,
                generation: 0,
            })));
            if app.state::<AcceptanceCapture>().nonce.is_some() {
                log::info!("acceptance capture enabled");
                if let Some(window) = app.get_webview_window("main") {
                    let route = match window.url() {
                        Ok(url) if url.host_str() == Some("localhost") && url.port() == Some(5173) => "development_server",
                        Ok(url) if url.host_str() == Some("tauri.localhost") || url.scheme() == "tauri" => "packaged_assets",
                        Ok(_) => "other",
                        Err(_) => "unavailable",
                    };
                    log::info!("acceptance initial page route={route}");
                }
            }
            if let Err(error) = start_backend(app.handle()) {
                log::error!("desktop sidecar initial start failed: {error}");
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Siyi");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(runtime) = handle.try_state::<BackendRuntime>() {
                stop_backend(&runtime, true);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn health() -> BackendHealth {
        BackendHealth {
            epoch: 0,
            port: None,
            pid: None,
            ready: false,
            phase: "stopped".into(),
            error: None,
            restart_count: 0,
            log_directory: String::new(),
        }
    }

    #[test]
    fn restart_publishes_new_epoch_and_port_and_rejects_old_pid_events() {
        let mut status = health();
        mark_backend_start(&mut status, 1, 4100);
        status.pid = Some(123);
        status.ready = true;
        mark_backend_start(&mut status, 2, 4200);
        assert_eq!(status.epoch, 2);
        assert_eq!(status.port, Some(4200));
        assert!(!status.ready);
        assert_eq!(status.pid, None);
        assert!(!process_generation_matches(2, 1, Some(123), 123));
        assert!(process_generation_matches(2, 2, Some(123), 123));
        assert_eq!(
            serde_json::to_value(status).expect("health JSON")["epoch"],
            2
        );
    }

    #[test]
    fn crash_restart_is_bounded_and_shutdown_does_not_restart() {
        let mut status = health();
        assert!(mark_backend_exit(&mut status, false, Some(1)));
        assert!(mark_backend_exit(&mut status, false, Some(1)));
        assert!(!mark_backend_exit(&mut status, false, Some(1)));
        assert_eq!(status.phase, "error");
        assert_eq!(status.restart_count, MAX_AUTO_RESTARTS);
        assert!(!mark_backend_exit(&mut status, true, Some(0)));
        assert_eq!(status.phase, "stopped");
    }

    #[test]
    fn allocated_port_is_locally_bindable() {
        let port = available_port().expect("port");
        assert!(port > 0);
    }

    #[test]
    fn readiness_requires_authenticated_json_health() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("listener");
        let port = listener.local_addr().expect("address").port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept");
            let mut request = [0_u8; 2048];
            let bytes = stream.read(&mut request).expect("read");
            let request = String::from_utf8_lossy(&request[..bytes]);
            assert!(request.contains("GET /api/desktop/status"));
            assert!(request.contains("X-Agent-Api-Token: test-token"));
            let body = "{\"status\":\"ok\"}";
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).expect("write");
        });
        assert!(backend_is_ready(port, "test-token", Duration::from_secs(1)));
        server.join().expect("server");
    }

    #[test]
    fn data_directory_is_stable_under_windows_local_data() {
        let path = agent_data_directory(Path::new(r"C:\Users\test\AppData\Local"), None);
        assert!(path.ends_with(Path::new(r"AureliusWu\Agent")));
    }

    #[test]
    fn configured_data_directory_is_used_for_isolated_smoke_tests() {
        let configured = PathBuf::from(r"C:\Temp\agent-smoke-data");
        let path = agent_data_directory(
            Path::new(r"C:\Users\test\AppData\Local"),
            Some(configured.clone()),
        );
        assert_eq!(path, configured);
    }

    #[test]
    fn provider_secret_round_trips_through_windows_credential_manager() {
        let name = format!("v8-release-gate-{}", std::process::id());
        let value = "siyi-v8-synthetic-credential";
        let _ = delete_secret(name.clone());
        let outcome = (|| -> Result<(), String> {
            set_secret(name.clone(), value.to_string())?;
            if get_secret(name.clone())? != Some(value.to_string()) {
                return Err("credential round trip did not preserve the value".to_string());
            }
            Ok(())
        })();
        let cleanup = delete_secret(name.clone());
        assert!(
            cleanup.is_ok(),
            "synthetic credential cleanup failed: {cleanup:?}"
        );
        assert!(
            outcome.is_ok(),
            "credential manager round trip failed: {outcome:?}"
        );
        assert_eq!(get_secret(name).expect("read after cleanup"), None);
    }

    #[test]
    fn secret_names_are_bounded_and_namespaced() {
        assert!(validate_secret_name("deepseek.api-key").is_ok());
        assert!(validate_secret_name("../escape").is_err());
        assert!(validate_secret_name("UPPERCASE").is_err());
        assert!(validate_secret_name("x").is_err());
    }

    #[test]
    fn candidate_frontend_directory_must_not_be_a_windows_drive_url() {
        use tauri::utils::config::FrontendDist;
        let absolute: FrontendDist = serde_json::from_value(serde_json::json!("C:/repo/frontend-dist")).unwrap();
        assert!(matches!(absolute, FrontendDist::Url(_)), "SDK contract changed: recheck candidate path interpretation");
        let relative: FrontendDist = serde_json::from_value(serde_json::json!("../../build/candidates/test/frontend-dist")).unwrap();
        assert!(matches!(relative, FrontendDist::Directory(_)), "candidate frontend must embed directory assets");
    }
}
