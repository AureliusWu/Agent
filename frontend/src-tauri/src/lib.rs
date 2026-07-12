use std::sync::Mutex;
use tauri::{Manager, RunEvent};
use tauri_plugin_shell::{process::CommandChild, ShellExt};

const SERVICE: &str = "AureliusWu.Agent";

struct BackendProcess {
  pid: u32,
  child: CommandChild,
}

struct BackendSidecar(Mutex<Option<BackendProcess>>);

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
    .plugin(tauri_plugin_shell::init())
    .invoke_handler(tauri::generate_handler![set_secret, get_secret, delete_secret])
    .setup(|app| {
      let (_events, child) = app.shell().sidecar("agent-backend")?.spawn()?;
      let pid = child.pid();
      app.manage(BackendSidecar(Mutex::new(Some(BackendProcess { pid, child }))));
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
