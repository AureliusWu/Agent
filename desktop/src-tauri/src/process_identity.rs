//! Retain the Windows process object while a PID is eligible for tree termination.
#[cfg(target_os = "windows")]
mod windows {
    use std::{
        ffi::c_void,
        os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    };

    #[repr(C)]
    #[derive(Default)]
    struct FileTime {
        low: u32,
        high: u32,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn GetProcessTimes(
            handle: *mut c_void,
            creation: *mut FileTime,
            exit: *mut FileTime,
            kernel: *mut FileTime,
            user: *mut FileTime,
        ) -> i32;
        fn WaitForSingleObject(handle: *mut c_void, timeout: u32) -> u32;
    }

    pub struct ProcessIdentity {
        handle: OwnedHandle,
        creation_time: u64,
    }

    fn creation_time(handle: &OwnedHandle) -> Option<u64> {
        let (mut creation, mut exit, mut kernel, mut user) = (
            FileTime::default(),
            FileTime::default(),
            FileTime::default(),
            FileTime::default(),
        );
        // The owned handle stays live throughout this query.
        let ok = unsafe {
            GetProcessTimes(
                handle.as_raw_handle(),
                &mut creation,
                &mut exit,
                &mut kernel,
                &mut user,
            )
        };
        (ok != 0).then_some((u64::from(creation.high) << 32) | u64::from(creation.low))
    }

    impl ProcessIdentity {
        pub fn capture(pid: u32) -> Option<Self> {
            // SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION; never open termination rights by PID.
            let raw = unsafe { OpenProcess(0x0010_0000 | 0x1000, 0, pid) };
            if raw.is_null() {
                return None;
            }
            let handle = unsafe { OwnedHandle::from_raw_handle(raw) };
            let creation_time = creation_time(&handle)?;
            Some(Self {
                handle,
                creation_time,
            })
        }

        pub fn is_current_and_running(&self) -> bool {
            creation_time(&self.handle) == Some(self.creation_time)
                && unsafe { WaitForSingleObject(self.handle.as_raw_handle(), 0) } == 258
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn retained_handle_and_creation_fingerprint_match_current_process() {
            let identity =
                ProcessIdentity::capture(std::process::id()).expect("own process handle");
            assert!(identity.is_current_and_running());
        }

        #[test]
        fn changed_creation_fingerprint_and_unknown_pid_fail_closed() {
            let mut identity =
                ProcessIdentity::capture(std::process::id()).expect("own process handle");
            identity.creation_time ^= 1;
            assert!(!identity.is_current_and_running());
            assert!(ProcessIdentity::capture(0).is_none());
        }

        #[test]
        fn exited_test_owned_process_is_not_eligible_for_pid_tree_kill() {
            use std::os::windows::process::CommandExt;
            let mut child = std::process::Command::new("powershell.exe")
                .creation_flags(0x0800_0000)
                .args([
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Start-Sleep -Seconds 30",
                ])
                .spawn()
                .expect("test-owned process");
            let identity = ProcessIdentity::capture(child.id());
            child.kill().expect("stop test-owned process");
            child.wait().expect("reap test-owned process");
            assert!(!identity
                .expect("retained child handle")
                .is_current_and_running());
        }
    }
}

#[cfg(target_os = "windows")]
pub use windows::ProcessIdentity;
