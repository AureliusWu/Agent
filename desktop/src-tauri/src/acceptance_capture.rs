//! Explicit, isolated desktop acceptance capture; disabled in normal launches.
//! This records production component observations, not visual/user acceptance.
use serde_json::{json, Value};
use std::{fs::OpenOptions, io::Write, path::{Path, PathBuf}, time::Instant};
#[cfg(test)]
use std::fs;

pub struct AcceptanceCapture {
    pub started: Instant,
    pub nonce: Option<String>,
    pub directory: Option<PathBuf>,
}

impl AcceptanceCapture {
    pub fn from_environment() -> Self {
        Self {
            started: Instant::now(),
            nonce: std::env::var("SIYI_DESKTOP_ACCEPTANCE_NONCE").ok(),
            directory: std::env::var_os("AGENT_DESKTOP_DATA_DIRECTORY").map(PathBuf::from),
        }
    }
}

fn valid_nonce(value: &str) -> bool {
    value.len() == 36 && value.bytes().enumerate().all(|(index, byte)| {
        if [8, 13, 18, 23].contains(&index) { byte == b'-' } else { byte.is_ascii_hexdigit() }
    })
}

fn safe_directory(path: &Path) -> Result<Vec<std::fs::File>, String> {
    if !path.is_absolute() { return Err("Acceptance requires an explicit absolute isolated data directory".into()); }
    let mut handles = Vec::new();
    // Pin ancestors against replacement. WRITE sharing is required for normal
    // NTFS child commits; all file opens/publish then use the retained parent
    // object, so in-place reparse mutation cannot redirect them by path.
    for ancestor in path.ancestors().collect::<Vec<_>>().into_iter().rev() {
        #[cfg(windows)]
        let handle = {
            use std::os::windows::fs::OpenOptionsExt;
            OpenOptions::new().read(true).access_mode(if ancestor == path { 0x87 } else { 0x81 })
                .share_mode(3).custom_flags(0x02000000 | 0x00200000)
                .open(ancestor).map_err(|_| "Cannot pin acceptance directory against reparse mutation")?
        };
        #[cfg(not(windows))]
        let handle = std::fs::File::open(ancestor).map_err(|_| "Acceptance data directory is unavailable")?;
        let metadata = handle.metadata().map_err(|_| "Cannot inspect pinned acceptance directory")?;
        #[cfg(windows)] {
            use std::os::windows::fs::MetadataExt;
            if metadata.file_attributes() & 0x400 != 0 { return Err("Acceptance directory cannot contain reparse points".into()); }
        }
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err("Acceptance directory cannot contain links or files".into());
        }
        handles.push(handle);
    }
    Ok(handles)
}

fn public_manifest(value: &Value) -> Result<Value, String> {
    let fields = ["manifest_version", "product_version", "git_commit", "git_short_commit", "git_branch",
        "build_time", "build_type", "workspace_state", "source_fingerprint", "build_id",
        "component_build_ids", "database_schema_version", "evidence_manifest_hash"];
    let object = value.as_object().ok_or("Component manifest must be an object")?;
    let mut result = serde_json::Map::new();
    for field in fields {
        let item = object.get(field).ok_or("Component manifest is incomplete")?;
        if serde_json::to_vec(item).map_err(|_| "Invalid component manifest")?.len() > 4096 {
            return Err("Component manifest field is too large".into());
        }
        result.insert(field.to_string(), item.clone());
    }
    Ok(Value::Object(result))
}

pub fn write_observation(capture: &AcceptanceCapture, actual_directory: &Path, react: &Value,
    tauri: &Value, sidecar: &Value, sidecar_pid: u32, window_handle: u64) -> Result<bool, String> {
    let Some(nonce) = capture.nonce.as_deref() else { return Ok(false); };
    if !valid_nonce(nonce) { return Err("Acceptance nonce must be a collector UUID".into()); }
    let directory = capture.directory.as_ref().ok_or("Acceptance requires an isolated data directory")?;
    let _namespace_handles = safe_directory(directory)?;
    if directory.canonicalize().map_err(|_| "Acceptance directory is unavailable")?
        != actual_directory.canonicalize().map_err(|_| "Actual data directory is unavailable")? {
        return Err("Acceptance must use the actual isolated data directory".into());
    }
    #[cfg(not(windows))]
    let marker_path = directory.join("rc-acceptance-owner.json");
    #[cfg(not(windows))]
    let options = { let mut options = OpenOptions::new(); options.read(true); options };
    #[cfg(windows)]
    let marker = crate::acceptance_native_file::open_relative(_namespace_handles.last().ok_or("Pinned directory is unavailable")?,
        "rc-acceptance-owner.json", false).map_err(|_| "Collector ownership marker is missing")?;
    #[cfg(not(windows))]
    let marker = options.open(marker_path).map_err(|_| "Collector ownership marker is missing")?;
    let metadata = marker.metadata().map_err(|_| "Ownership marker is unavailable")?;
    #[cfg(windows)] {
        use std::os::windows::fs::MetadataExt;
        if metadata.file_attributes() & 0x400 != 0 { return Err("Ownership marker cannot be a reparse point".into()); }
    }
    if !metadata.is_file() || metadata.len() > 4096 { return Err("Ownership marker is invalid".into()); }
    use std::io::Read;
    let mut marker_bytes = Vec::new(); marker.take(4097).read_to_end(&mut marker_bytes).map_err(|_| "Cannot read ownership marker")?;
    let owner: Value = serde_json::from_slice(&marker_bytes).map_err(|_| "Ownership marker is invalid")?;
    if owner != json!({"schema_version":1,"acceptance_nonce":nonce,"isolated_test_data":true,"created_by":"record-rc-desktop-startup"}) {
        return Err("Ownership marker does not belong to this collector launch".into());
    }
    let react = public_manifest(react)?;
    let tauri = public_manifest(tauri)?;
    let original_sidecar = sidecar;
    let mut sidecar = public_manifest(original_sidecar)?;
    for (field, value) in tauri.as_object().ok_or("Native manifest is invalid")? {
        if react[field] != *value || sidecar[field] != *value {
            return Err(format!("Production component identity mismatch: {field}"));
        }
    }
    if original_sidecar["embedded"] != true || original_sidecar["component"] != "sidecar"
        || original_sidecar["component_build_id"] != tauri["component_build_ids"]["sidecar"] {
        return Err("Sidecar must report its embedded component identity".into());
    }
    sidecar["embedded"] = json!(true); sidecar["component"] = json!("sidecar");
    sidecar["component_build_id"] = tauri["component_build_ids"]["sidecar"].clone();
    if sidecar_pid == 0 { return Err("Actual sidecar process is unavailable".into()); }
    if window_handle == 0 { return Err("Actual main window handle is unavailable".into()); }
    let report = json!({"schema_version":1,"report_type":"rc_desktop_runtime_observation",
        "protocol_version":"desktop-render-ready-v1","status":"PASS","actual_run":true,
        "acceptance_nonce":nonce,"isolated_test_data":true,"desktop_render_ready":true,"sidecar_ready":true,
        "readiness_ms":capture.started.elapsed().as_millis().max(1),
        "process_ids":{"desktop":std::process::id(),"sidecar":sidecar_pid},
        "window_handle":window_handle,"window_label":"main",
        "components":{"react":react,"tauri":tauri,"sidecar":sidecar}});
    let bytes = serde_json::to_vec(&report).map_err(|_| "Cannot serialize acceptance observation")?;
    if bytes.len() > 64 * 1024 { return Err("Acceptance observation exceeds the bound".into()); }
    #[cfg(windows)]
    let parent = _namespace_handles.last().ok_or("Pinned directory is unavailable")?;
    #[cfg(windows)]
    if crate::acceptance_native_file::open_relative(parent, "rc-desktop-observation.json", false).is_ok() { return Ok(false); }
    #[cfg(windows)]
    let opened = crate::acceptance_native_file::open_relative(parent, "rc-desktop-observation.tmp", true);
    #[cfg(not(windows))]
    let opened: std::io::Result<std::fs::File> = Err(std::io::Error::new(std::io::ErrorKind::Unsupported, "Acceptance capture requires Windows"));
    let mut file = match opened {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => return Ok(false),
        Err(_) => return Err("Cannot exclusively create acceptance observation".into()),
    };
    file.write_all(&bytes).map_err(|_| "Cannot write acceptance observation")?;
    file.sync_all().map_err(|_| "Cannot flush acceptance observation")?;
    #[cfg(windows)]
    let published = crate::acceptance_native_file::publish_no_replace(&file, parent, "rc-desktop-observation.json");
    #[cfg(not(windows))]
    let published: std::io::Result<()> = Err(std::io::Error::new(std::io::ErrorKind::Unsupported, "Acceptance capture requires Windows"));
    // Publish the flushed file by root-handle-relative native rename, never
    // replace a final receipt, and retain staging on failure for inspection.
    match published {
        Ok(()) => (),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            return Ok(false);
        },
        Err(_) => return Err("Cannot atomically publish acceptance observation".into()),
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn manifest() -> Value {
        json!({"manifest_version":1,"product_version":"16.0.0","git_commit":"a",
            "git_short_commit":"a","git_branch":"test","build_time":"time","build_type":"Release",
            "workspace_state":"DIRTY","source_fingerprint":"b","build_id":"c",
            "component_build_ids":{"react":"react-c","tauri":"tauri-c","sidecar":"sidecar-c"},
            "database_schema_version":46,"evidence_manifest_hash":"d"})
    }
    #[test]
    fn capture_is_disabled_without_nonce() {
        let capture = AcceptanceCapture { started: Instant::now(), nonce: None, directory: None };
        assert!(!write_observation(&capture, Path::new("missing"), &Value::Null, &Value::Null, &Value::Null, 0, 0).unwrap());
    }
    #[test]
    fn nonce_and_manifest_are_bounded() {
        assert!(valid_nonce("00000000-0000-0000-0000-000000000000"));
        assert!(!valid_nonce("../../output"));
        assert!(public_manifest(&Value::Null).is_err());
        let mut source = manifest(); source["secret"] = json!("never-copy");
        assert!(public_manifest(&source).unwrap().get("secret").is_none());
        source["build_id"] = json!("a".repeat(5000));
        assert!(public_manifest(&source).is_err());
    }
    #[test]
    fn real_observation_is_exclusive_and_rejects_mismatch() {
        let directory = std::env::temp_dir().join(format!("siyi-capture-test-{}-{}", std::process::id(), rand::random::<u64>()));
        fs::create_dir(&directory).unwrap();
        let capture = AcceptanceCapture { started: Instant::now(), nonce: Some("00000000-0000-0000-0000-000000000000".into()), directory: Some(directory.clone()) };
        let source = manifest(); let mut sidecar = source.clone();
        sidecar["embedded"] = json!(true); sidecar["component"] = json!("sidecar"); sidecar["component_build_id"] = json!("sidecar-c");
        assert!(write_observation(&capture, &directory, &source, &source, &sidecar, 123, 1).is_err());
        fs::write(directory.join("rc-acceptance-owner.json"), serde_json::to_vec(&json!({"schema_version":1,
            "acceptance_nonce":capture.nonce,"isolated_test_data":true,"created_by":"record-rc-desktop-startup"})).unwrap()).unwrap();
        for field in ["git_branch", "build_time", "git_short_commit", "evidence_manifest_hash", "build_id"] {
            let mut wrong = source.clone(); wrong[field] = json!("must-not-copy-secret");
            assert!(write_observation(&capture, &directory, &wrong, &source, &sidecar, 123, 1).is_err());
        }
        assert!(write_observation(&capture, &directory, &source, &source, &sidecar, 123, 1).unwrap());
        let before = fs::read(directory.join("rc-desktop-observation.json")).unwrap();
        assert!(!write_observation(&capture, &directory, &source, &source, &sidecar, 123, 1).unwrap());
        assert_eq!(before, fs::read(directory.join("rc-desktop-observation.json")).unwrap());
        fs::remove_file(directory.join("rc-desktop-observation.json")).unwrap();
        fs::remove_file(directory.join("rc-acceptance-owner.json")).unwrap(); fs::remove_dir(directory).unwrap();
    }
    #[cfg(windows)]
    #[test]
    fn pinned_namespace_prevents_parent_replacement() {
        let directory = std::env::temp_dir().join(format!("siyi-capture-pin-{}-{}", std::process::id(), rand::random::<u64>()));
        fs::create_dir(&directory).unwrap();
        let moved = directory.with_extension("moved");
        let handles = safe_directory(&directory).unwrap();
        assert!(fs::rename(&directory, &moved).is_err());
        drop(handles);
        fs::rename(&directory, &moved).unwrap(); fs::remove_dir(moved).unwrap();
    }
    #[cfg(windows)]
    #[test]
    fn in_place_reparse_cannot_redirect_native_receipt_publish() {
        use std::{ffi::c_void, os::windows::{fs::OpenOptionsExt, io::AsRawHandle}};
        #[link(name = "kernel32")]
        extern "system" {
            fn DeviceIoControl(handle: *mut c_void, code: u32, input: *const c_void,
                input_size: u32, output: *mut c_void, output_size: u32, returned: *mut u32, overlapped: *mut c_void) -> i32;
        }
        let base = std::env::temp_dir().join(format!("siyi-capture-reparse-{}-{}", std::process::id(), rand::random::<u64>()));
        fs::create_dir(&base).unwrap();
        let target = base.join("target"); let outside = base.join("outside");
        fs::create_dir(&target).unwrap(); fs::create_dir(&outside).unwrap();
        let handles = safe_directory(&target).unwrap(); let parent = handles.last().unwrap();
        let mutator = OpenOptions::new().write(true).share_mode(7).custom_flags(0x02000000 | 0x00200000).open(&target).unwrap();
        let substitute: Vec<u8> = format!("\\??\\{}", outside.display()).encode_utf16().flat_map(u16::to_le_bytes).collect();
        let printable: Vec<u8> = outside.to_string_lossy().encode_utf16().flat_map(u16::to_le_bytes).collect();
        let mut names = substitute.clone(); names.extend_from_slice(&[0, 0]); names.extend_from_slice(&printable); names.extend_from_slice(&[0, 0]);
        let mut buffer = 0xA0000003u32.to_le_bytes().to_vec();
        for number in [(8 + names.len()) as u16, 0, 0, substitute.len() as u16, (substitute.len() + 2) as u16, printable.len() as u16] {
            buffer.extend_from_slice(&number.to_le_bytes());
        }
        buffer.extend_from_slice(&names);
        let mut returned = 0;
        let changed = unsafe { DeviceIoControl(mutator.as_raw_handle(), 0x000900A4, buffer.as_ptr() as *const c_void,
            buffer.len() as u32, std::ptr::null_mut(), 0, &mut returned, std::ptr::null_mut()) };
        assert_ne!(changed, 0, "probe must inject the actual in-place reparse tag");
        let staging = crate::acceptance_native_file::open_relative(parent, "rc-desktop-observation.tmp", true);
        if let Ok(mut file) = staging {
            file.write_all(b"complete synthetic receipt").unwrap(); file.sync_all().unwrap();
            let _result = crate::acceptance_native_file::publish_no_replace(&file, parent, "rc-desktop-observation.json");
        }
        assert!(!outside.join("rc-desktop-observation.json").exists(), "receipt was redirected outside the pinned directory");
        assert!(!outside.join("rc-desktop-observation.tmp").exists(), "staging creation escaped the pinned directory");
        let clear = [3u8, 0, 0, 160, 0, 0, 0, 0];
        assert_ne!(unsafe { DeviceIoControl(mutator.as_raw_handle(), 0x000900AC, clear.as_ptr() as *const c_void,
            8, std::ptr::null_mut(), 0, &mut returned, std::ptr::null_mut()) }, 0);
        drop(mutator); drop(handles);
        for name in ["rc-desktop-observation.json", "rc-desktop-observation.tmp"] {
            let file = target.join(name); if file.exists() { fs::remove_file(file).unwrap(); }
        }
        fs::remove_dir(target).unwrap(); fs::remove_dir(outside).unwrap(); fs::remove_dir(base).unwrap();
    }
}
