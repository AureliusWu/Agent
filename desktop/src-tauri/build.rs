use std::{env, fs, path::PathBuf, process::Command};

fn main() {
    let crate_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    let root = crate_dir
        .join("../..")
        .canonicalize()
        .expect("repository root");
    let manifest = env::var_os("SIYI_BUILD_MANIFEST")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("build/generated/build-info.json"));
    if !manifest.is_file() {
        let bundled_python = root.join("siyi/.venv/Scripts/python.exe");
        let python = if bundled_python.is_file() {
            bundled_python
        } else {
            PathBuf::from("python")
        };
        let status = Command::new(python)
            .arg(root.join("scripts/generate_build_info.py"))
            .args(["--build-type", "Development"])
            .current_dir(&root)
            .status()
            .expect("run build manifest generator");
        assert!(status.success(), "build manifest generation failed");
    }
    let payload = fs::read_to_string(&manifest).expect("read build manifest");
    println!("cargo:rerun-if-changed={}", manifest.display());
    println!("cargo:rustc-env=SIYI_BUILD_INFO_JSON={payload}");
    tauri_build::build()
}
