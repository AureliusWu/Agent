//! Small fixed-name receipt files relative to a pinned Windows directory.
//! Never reopen the checked directory namespace during create/atomic publish.
#![cfg(windows)]
use std::{ffi::c_void, fs::File, io, mem, os::windows::io::{AsRawHandle, FromRawHandle}, ptr};

#[repr(C)]
struct UnicodeString { length: u16, maximum: u16, buffer: *mut u16 }
#[repr(C)]
struct ObjectAttributes { length: u32, root: *mut c_void, name: *mut UnicodeString,
    attributes: u32, security: *mut c_void, quality: *mut c_void }
#[repr(C)]
#[derive(Default)]
struct IoStatusBlock { status: isize, information: usize }
#[repr(C)]
struct RenameInformation { replace: u8, root: *mut c_void, name_length: u32, name: [u16; 1] }

#[link(name = "ntdll")]
extern "system" {
    fn NtCreateFile(handle: *mut *mut c_void, access: u32, attributes: *mut ObjectAttributes,
        completion: *mut IoStatusBlock, allocation: *mut i64, file_attributes: u32,
        share: u32, disposition: u32, options: u32, ea: *mut c_void, ea_length: u32) -> i32;
    fn NtSetInformationFile(handle: *mut c_void, completion: *mut IoStatusBlock,
        information: *mut c_void, length: u32, class: u32) -> i32;
    fn RtlNtStatusToDosError(status: i32) -> u32;
}

fn fixed_name(name: &str) -> io::Result<Vec<u16>> {
    if name.is_empty() || name.len() > 128 || !name.bytes().all(|byte| byte.is_ascii_alphanumeric() || b".-_".contains(&byte))
        || name == "." || name == ".." {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid acceptance file name"));
    }
    Ok(name.encode_utf16().chain(Some(0)).collect())
}

pub fn open_relative(parent: &File, name: &str, create: bool) -> io::Result<File> {
    let mut wide = fixed_name(name)?;
    let length = ((wide.len() - 1) * 2) as u16;
    let mut unicode = UnicodeString { length, maximum: length + 2, buffer: wide.as_mut_ptr() };
    let mut attributes = ObjectAttributes { length: mem::size_of::<ObjectAttributes>() as u32,
        root: parent.as_raw_handle(), name: &mut unicode, attributes: 0x40,
        security: ptr::null_mut(), quality: ptr::null_mut() };
    let mut handle = ptr::null_mut(); let mut completion = IoStatusBlock::default();
    // Synchronous non-directory file, OPEN_REPARSE_POINT, no DELETE sharing.
    let access = 0x100000 | 0x80 | if create { 0x10000 | 0x2 } else { 0x1 };
    let status = unsafe { NtCreateFile(&mut handle, access, &mut attributes, &mut completion,
        ptr::null_mut(), 0x80, 3, if create { 2 } else { 1 }, 0x20 | 0x40 | 0x200000, ptr::null_mut(), 0) };
    if status < 0 { return Err(io::Error::from_raw_os_error(unsafe { RtlNtStatusToDosError(status) } as i32)); }
    if handle.is_null() { return Err(io::Error::new(io::ErrorKind::Other, "native acceptance file handle unavailable")); }
    Ok(unsafe { File::from_raw_handle(handle) })
}

pub fn publish_no_replace(staging: &File, parent: &File, name: &str) -> io::Result<()> {
    let wide = fixed_name(name)?;
    let name_bytes = (wide.len() - 1) * 2;
    let offset = mem::offset_of!(RenameInformation, name);
    let size = offset + name_bytes;
    let mut aligned = vec![0u64; (size + 7) / 8];
    let information = aligned.as_mut_ptr() as *mut RenameInformation;
    unsafe {
        (*information).replace = 0;
        (*information).root = parent.as_raw_handle();
        (*information).name_length = name_bytes as u32;
        ptr::copy_nonoverlapping(wide.as_ptr(), (information as *mut u8).add(offset) as *mut u16, wide.len() - 1);
    }
    let mut completion = IoStatusBlock::default();
    let status = unsafe { NtSetInformationFile(staging.as_raw_handle(), &mut completion,
        information as *mut c_void, size as u32, 10) };
    if status < 0 { return Err(io::Error::from_raw_os_error(unsafe { RtlNtStatusToDosError(status) } as i32)); }
    Ok(())
}
