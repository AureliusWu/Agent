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

const SERVICE: &str = "AureliusWu.Agent";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_AUTO_RESTARTS: u8 = 2;

struct BackendProcess {
    pid: u32,
    child: CommandChild,
}

#[derive(Clone, Serialize)]
struct BackendHealth {
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
    let mut stream = TcpStream::connect_timeout(&address, timeout).map_err(|error| error.to_string())?;
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
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|error| error.to_string())?;
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
        state.health.port = Some(port);
        state.health.pid = None;
        state.health.ready = false;
        state.health.phase = if state.health.restart_count > 0 {
            "restarting".to_string()
        } else {
            "starting".to_string()
        };
        state.health.error = None;
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
            .env("AGENT_DATABASE_PATH", data_directory.join("data").join("agent.db"))
            .env("AGENT_LOG_PATH", data_directory.join("logs").join("agent.log"))
            .env("AGENT_EXTENSION_DIRECTORY", data_directory.join("extensions"))
            .spawn()
            .map_err(|error| format!("本地核心启动失败：{error}")),
        Err(error) => Err(format!("本地核心配置错误：{error}")),
    }
    .map_err(|message| {
        update_start_failure(&runtime, generation, message.clone());
        message
    })?;

    let pid = child.pid();
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
        state.process = Some(BackendProcess { pid, child });
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
                        let current = state.generation == generation
                            && state.process.as_ref().is_some_and(|process| process.pid == pid);
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
                let current = state.generation == generation
                    && state.process.as_ref().is_some_and(|process| process.pid == pid);
                if current && !state.shutting_down {
                    state.health.ready = false;
                    state.health.phase = "error".to_string();
                    state.health.error = Some("本地核心在 30 秒内未就绪，请查看日志或重启核心".to_string());
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
                    let should_restart = if let Some(runtime) = events_app.try_state::<BackendRuntime>() {
                        match runtime.0.lock() {
                            Ok(mut state) => {
                                let current = state.generation == generation
                                    && state.process.as_ref().is_some_and(|process| process.pid == pid);
                                if !current {
                                    false
                                } else {
                                    state.process = None;
                                    state.health.pid = None;
                                    state.health.ready = false;
                                    if state.shutting_down {
                                        state.health.phase = "stopped".to_string();
                                        state.health.error = None;
                                        false
                                    } else if state.health.restart_count < MAX_AUTO_RESTARTS {
                                        state.health.restart_count += 1;
                                        state.health.phase = "restarting".to_string();
                                        state.health.error = Some(format!(
                                            "本地核心异常退出（代码 {:?}），正在自动重启",
                                            payload.code
                                        ));
                                        true
                                    } else {
                                        state.health.phase = "error".to_string();
                                        state.health.error = Some(format!(
                                            "本地核心连续异常退出（代码 {:?}），请查看日志后手动重启",
                                            payload.code
                                        ));
                                        false
                                    }
                                }
                            }
                            Err(_) => false,
                        }
                    } else {
                        false
                    };
                    log::warn!("desktop sidecar terminated pid={pid} code={:?}", payload.code);
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
            Ok((status, _)) => log::warn!("desktop sidecar rejected graceful shutdown: HTTP {status}"),
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
        log::error!("desktop sidecar did not stop gracefully; forcing pid={}", process.pid);
        #[cfg(target_os = "windows")]
        let _ = std::process::Command::new("taskkill")
            .args(["/PID", &process.pid.to_string(), "/T", "/F"])
            .status();
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
fn restart_backend(app: AppHandle, state: State<'_, BackendRuntime>) -> Result<BackendHealth, String> {
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
fn set_secret(name: String, value: String) -> Result<(), String> {
    keyring::Entry::new(SERVICE, &name)
        .map_err(|error| error.to_string())?
        .set_password(&value)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn get_secret(name: String) -> Result<Option<String>, String> {
    let entry = keyring::Entry::new(SERVICE, &name).map_err(|error| error.to_string())?;
    match entry.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(error) => Err(error.to_string()),
    }
}

#[tauri::command]
fn delete_secret(name: String) -> Result<(), String> {
    let entry = keyring::Entry::new(SERVICE, &name).map_err(|error| error.to_string())?;
    match entry.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(error) => Err(error.to_string()),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
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
            desktop_build_info
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

}
