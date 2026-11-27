from __future__ import annotations

import ctypes
import shutil
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _filetime_to_int(ft: FILETIME) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


@dataclass
class SystemMetrics:
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    gpu_percent: float | None = None
    gpu_mem_used_mb: float | None = None
    gpu_mem_total_mb: float | None = None
    gpu_temp_c: float | None = None
    gpu_name: str = ""


class SystemMonitor:
    """Lightweight Windows CPU/RAM/GPU monitor using Win32 ctypes and nvidia-smi."""

    def __init__(self, gpu_poll_interval: float = 2.0) -> None:
        self._kernel32 = getattr(ctypes, "windll", None)
        if self._kernel32 is not None:
            self._kernel32 = self._kernel32.kernel32
            self._kernel32.GetSystemTimes.argtypes = [
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
            ]
            self._kernel32.GetSystemTimes.restype = wintypes.BOOL
            self._kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
            self._kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

        self._prev_times: tuple[int, int, int] | None = None
        self._init_cpu_baseline()

        self._nvidia_smi = shutil.which("nvidia-smi")
        self._gpu_poll_interval = max(1.0, gpu_poll_interval)
        self._gpu_lock = threading.Lock()
        self._gpu_data: tuple[float | None, float | None, float | None, float | None, str] = (
            None,
            None,
            None,
            None,
            "",
        )
        self._stop_event = threading.Event()
        self._gpu_thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._nvidia_smi or (self._gpu_thread and self._gpu_thread.is_alive()):
            return
        self._stop_event.clear()
        self._gpu_thread = threading.Thread(
            target=self._gpu_loop, name="NvidiaGpuPoller", daemon=True
        )
        self._gpu_thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _read_system_times(self) -> tuple[int, int, int] | None:
        if self._kernel32 is None:
            return None
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        ok = self._kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        )
        if not ok:
            return None
        return _filetime_to_int(idle), _filetime_to_int(kernel), _filetime_to_int(user)

    def _init_cpu_baseline(self) -> None:
        self._prev_times = self._read_system_times()

    def _sample_cpu(self) -> float:
        curr = self._read_system_times()
        if curr is None:
            return 0.0
        if self._prev_times is None:
            self._prev_times = curr
            return 0.0
        prev_idle, prev_kernel, prev_user = self._prev_times
        idle, kernel, user = curr
        self._prev_times = curr

        d_idle = max(0, idle - prev_idle)
        d_kernel = max(0, kernel - prev_kernel)
        d_user = max(0, user - prev_user)
        total = d_kernel + d_user
        if total <= 0:
            return 0.0
        busy = max(0, total - d_idle)
        pct = (busy / total) * 100.0
        return max(0.0, min(100.0, pct))

    def _sample_ram(self) -> tuple[float, float, float]:
        if self._kernel32 is None:
            return 0.0, 0.0, 0.0
        mem = MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not self._kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            return 0.0, 0.0, 0.0
        total_gb = mem.ullTotalPhys / (1024**3)
        avail_gb = mem.ullAvailPhys / (1024**3)
        used_gb = max(0.0, total_gb - avail_gb)
        pct = float(mem.dwMemoryLoad)
        return pct, used_gb, total_gb

    def _query_nvidia_once(self) -> None:
        if not self._nvidia_smi:
            return
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            proc = subprocess.run(
                [
                    self._nvidia_smi,
                    "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3.0,
                creationflags=creationflags,
            )
            if proc.returncode != 0:
                return
            lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            if not lines:
                return
            utils: list[float] = []
            mem_used = 0.0
            mem_total = 0.0
            temps: list[float] = []
            names: list[str] = []
            for ln in lines:
                parts = [p.strip() for p in ln.split(",")]
                if len(parts) < 5:
                    continue
                try:
                    utils.append(float(parts[0]))
                    mem_used += float(parts[1])
                    mem_total += float(parts[2])
                    temps.append(float(parts[3]))
                    names.append(parts[4])
                except ValueError:
                    continue
            if utils:
                avg_util = max(0.0, min(100.0, sum(utils) / len(utils)))
                max_temp = max(temps) if temps else None
                gpu_name = names[0] if len(names) == 1 else f"{names[0]} (+{len(names)-1})"
                with self._gpu_lock:
                    self._gpu_data = (avg_util, mem_used, mem_total, max_temp, gpu_name)
        except Exception:
            pass

    def _gpu_loop(self) -> None:
        while not self._stop_event.is_set():
            self._query_nvidia_once()
            self._stop_event.wait(self._gpu_poll_interval)

    def sample(self) -> SystemMetrics:
        cpu = self._sample_cpu()
        ram_pct, ram_used, ram_total = self._sample_ram()
        with self._gpu_lock:
            gpu_pct, gpu_mem_used, gpu_mem_total, gpu_temp, gpu_name = self._gpu_data
        return SystemMetrics(
            cpu_percent=cpu,
            ram_percent=ram_pct,
            ram_used_gb=ram_used,
            ram_total_gb=ram_total,
            gpu_percent=gpu_pct,
            gpu_mem_used_mb=gpu_mem_used,
            gpu_mem_total_mb=gpu_mem_total,
            gpu_temp_c=gpu_temp,
            gpu_name=gpu_name,
        )
