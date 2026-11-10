"""Read creation times of all python.exe processes via WinAPI (no PowerShell).

Why: this session's PowerShell tool returns no stdout, and wmic is absent.
GetProcessTimes via ctypes is the only reliable way to tell a process left over
from an earlier life apart from one spawned by the current boot.
"""
import ctypes
import ctypes.wintypes as wt
from datetime import datetime, timedelta, timezone

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


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


def filetime_to_dt(ft):
    # FILETIME: 100-ns intervals since 1601-01-01 UTC
    value = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=value / 10)


def main():
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        raise ctypes.WinError(ctypes.get_last_error())

    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    rows = []

    ok = k32.Process32First(snap, ctypes.byref(entry))
    while ok:
        name = entry.szExeFile.decode("mbcs", "replace")
        if name.lower() == "python.exe":
            h = k32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID
            )
            if h:
                creation, exit_, kernel, user = (
                    wt.FILETIME(),
                    wt.FILETIME(),
                    wt.FILETIME(),
                    wt.FILETIME(),
                )
                if k32.GetProcessTimes(
                    h,
                    ctypes.byref(creation),
                    ctypes.byref(exit_),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    created = filetime_to_dt(creation)
                    cpu = (
                        (kernel.dwHighDateTime << 32 | kernel.dwLowDateTime)
                        + (user.dwHighDateTime << 32 | user.dwLowDateTime)
                    ) / 1e7
                    rows.append(
                        (
                            created.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                            entry.th32ProcessID,
                            entry.th32ParentProcessID,
                            round(cpu, 1),
                        )
                    )
                k32.CloseHandle(h)
        ok = k32.Process32Next(snap, ctypes.byref(entry))

    k32.CloseHandle(snap)
    print(f"{'created':<20} {'pid':>7} {'ppid':>7} {'cpu_s':>8}")
    for created, pid, ppid, cpu in sorted(rows):
        print(f"{created:<20} {pid:>7} {ppid:>7} {cpu:>8}")


if __name__ == "__main__":
    main()
