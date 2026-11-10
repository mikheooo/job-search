"""Print command lines of all python.exe processes via NtQueryInformationProcess.

Why: tasklist /V shows only the image path, and this session's PowerShell tool
returns no stdout. ProcessCommandLineInformation (class 60) is the only way left
to see *what* each python process was actually told to run.
"""
import ctypes
import ctypes.wintypes as wt

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ProcessCommandLineInformation = 60


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT), ("Buffer", ctypes.c_void_p)]


def command_line(pid):
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        size = wt.ULONG(0)
        ntdll.NtQueryInformationProcess(h, ProcessCommandLineInformation, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        status = ntdll.NtQueryInformationProcess(
            h, ProcessCommandLineInformation, buf, size.value, ctypes.byref(size)
        )
        if status != 0:
            return None
        us = ctypes.cast(buf, ctypes.POINTER(UNICODE_STRING)).contents
        if not us.Buffer or not us.Length:
            return None
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    finally:
        k32.CloseHandle(h)


def main():
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    ok = k32.Process32First(snap, ctypes.byref(entry))
    while ok:
        name = entry.szExeFile.decode("mbcs", "replace").lower()
        if name in ("python.exe", "pythonw.exe"):
            cmd = command_line(entry.th32ProcessID) or "<no access>"
            print(f"pid={entry.th32ProcessID:<7} ppid={entry.th32ParentProcessID:<7} {cmd[:400]}")
        ok = k32.Process32Next(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)


if __name__ == "__main__":
    main()
