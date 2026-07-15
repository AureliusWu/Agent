use rand::{distr::Alphanumeric, Rng};
use serde::Serialize;
use std::{
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};
use tauri::{Manager, RunEvent, State};
use tauri_plugin_shell::{process::CommandChild, ShellExt};

const SERVICE: &str = "AureliusWu.Agent";

struct BackendProcess {
    pid: u32,
    child: CommandChild,
}

struct BackendSidecar(Mutex<Option<BackendProcess>>);

#[derive(Clone, Serialize)]
struct BackendHealth {
    port: Option<u16>,
    ready: bool,
    error: Option<String>,
}

struct BackendStatus(Mutex<BackendHealth>);

struct BackendAccessToken(String);

fn available_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|error| error.to_string())?;
    listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|error| error.to_string())
}

fn wait_for_backend(port: u16, timeout: Duration) -> bool {
    let started = Instant::now();
    while started.elapsed() < timeout {
        if TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(150))
            .is_ok()
        {
            return true;
        }
        thread::sleep(Duration::from_millis(100));
    }
    false
}

fn agent_data_directory(local_data: &Path, configured: Option<PathBuf>) -> PathBuf {
    configured.unwrap_or_else(|| local_data.join("AureliusWu").join("Agent"))
}

#[tauri::command]
fn backend_status(state: State<'_, BackendStatus>) -> Result<BackendHealth, String> {
    let mut status = state.0.lock().map_err(|_| "后端状态锁异常".to_string())?;
    if let Some(port) = status.port {
        status.ready =
            TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(200))
                .is_ok();
        if !status.ready && status.error.is_none() {
            status.error = Some("本地后端连接已断开".to_string());
        }
    }
    Ok(status.clone())
}

#[tauri::command]
fn backend_api_token(state: State<'_, BackendAccessToken>) -> String {
    state.0.clone()
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
        .invoke_handler(tauri::generate_handler![
            set_secret,
            get_secret,
            delete_secret,
            backend_status,
            backend_api_token
        ])
        .setup(|app| {
            let api_token: String = rand::rng()
                .sample_iter(&Alphanumeric)
                .take(64)
                .map(char::from)
                .collect();
            app.manage(BackendAccessToken(api_token.clone()));
            let port = available_port().ok();
            let mut process = None;
            let mut health = BackendHealth {
                port,
                ready: false,
                error: None,
            };
            if let Some(port) = port {
                let data_directory = agent_data_directory(
                    &app.path().local_data_dir()?,
                    std::env::var_os("AGENT_DESKTOP_DATA_DIRECTORY").map(PathBuf::from),
                );
                std::fs::create_dir_all(&data_directory)?;
                match app.shell().sidecar("agent-backend") {
                    Ok(command) => match command
                        .env("AGENT_PORT", port.to_string())
                        .env("AGENT_DEPLOYMENT_MODE", "desktop_local")
                        .env("AGENT_BIND_HOST", "127.0.0.1")
                        .env("AGENT_API_TOKEN", api_token)
                        .env("AGENT_DATABASE_PATH", data_directory.join("agent.db"))
                        .env(
                            "AGENT_LOG_PATH",
                            data_directory.join("logs").join("agent.log"),
                        )
                        .env(
                            "AGENT_EXTENSION_DIRECTORY",
                            data_directory.join("extensions"),
                        )
                        .spawn()
                    {
                        Ok((_events, child)) => {
                            let pid = child.pid();
                            health.ready = wait_for_backend(port, Duration::from_secs(15));
                            if !health.ready {
                                health.error =
                                    Some("本地后端在 15 秒内未就绪，请查看日志".to_string());
                            }
                            process = Some(BackendProcess { pid, child });
                        }
                        Err(error) => health.error = Some(format!("本地后端启动失败：{error}")),
                    },
                    Err(error) => health.error = Some(format!("本地后端配置错误：{error}")),
                }
            } else {
                health.error = Some("无法分配本地端口".to_string());
            }
            app.manage(BackendSidecar(Mutex::new(process)));
            app.manage(BackendStatus(Mutex::new(health)));
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Agent");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(state) = handle.try_state::<BackendSidecar>() {
                if let Ok(mut child) = state.0.lock() {
                    if let Some(process) = child.take() {
                        #[cfg(target_os = "windows")]
                        let _ = std::process::Command::new("taskkill")
                            .args(["/PID", &process.pid.to_string(), "/T", "/F"])
                            .status();
                        let _ = process.child.kill();
                    }
                }
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
    fn readiness_detects_listener() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("listener");
        let port = listener.local_addr().expect("address").port();
        assert!(wait_for_backend(port, Duration::from_millis(300)));
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
