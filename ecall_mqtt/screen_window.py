"""e-Call 수신 화면을 '앱 창'으로 띄우고 맨 앞으로 가져온다 (ecall_screen.py 가 쓴다).

Windows: Edge(없으면 Chrome)를 주소창 없는 앱 창(--app)으로 열고, 창 크기를 작업 영역의
가로 절반 × 세로 거의 전체로 맞춰 가운데에 놓은 뒤, 다른 창(카톡·메모 등)보다 위(항상 위)로 올린다.
그 밖의 OS 나 브라우저를 못 찾으면 기본 브라우저로 연다.
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import webbrowser

TITLE = "e-Call 수신 화면"          # ecall_screen.html 의 <title> 과 같아야 창을 찾는다
WIDTH_RATIO = 0.5                   # 작업 영역 가로의 절반
HEIGHT_RATIO = 0.94                 # 작업 영역 세로의 대부분

BROWSERS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]


def _find_browser():
    for path in BROWSERS:
        path = os.path.expandvars(path)
        if os.path.isfile(path):
            return path
    return None


if sys.platform == "win32":
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    try:                                                  # 실제 픽셀 좌표로 다루기 (150% 배율 등)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    HWND_TOPMOST = -1
    SWP_SHOWWINDOW = 0x0040
    SW_RESTORE = 9
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _work_area():
        r = wintypes.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA (작업 표시줄 제외)
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def window_rect():
        x0, y0, w, h = _work_area()
        ww, wh = int(w * WIDTH_RATIO), int(h * HEIGHT_RATIO)
        return x0 + (w - ww) // 2, y0 + (h - wh) // 2, ww, wh

    def _find_window():
        found = []

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value == TITLE:                     # 앱 창은 페이지 제목만 창 제목으로 쓴다
                    found.append(hwnd)
            return True

        user32.EnumWindows(EnumProc(cb), 0)
        return found[0] if found else None

    def _bring_to_front(hwnd, resize):
        user32.ShowWindow(hwnd, SW_RESTORE)
        x, y, w, h = window_rect()
        flags = SWP_SHOWWINDOW | (0 if resize else 0x0001 | 0x0002)   # 이미 열린 창은 크기·위치 유지
        user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, flags)
        # 다른 프로그램이 앞에 있으면 Windows 가 포커스 가져오기를 막으므로, Alt 키를 한 번 눌러 풀어 준다
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)

    def _wait_and_front(resize, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            hwnd = _find_window()
            if hwnd:
                time.sleep(0.2)                            # 창이 다 그려진 뒤 옮긴다
                _bring_to_front(hwnd, resize)
                return True
            time.sleep(0.1)
        print("[화면] 창을 찾지 못해 맨 앞으로 가져오지 못했습니다")
        return False


def show_screen(url):
    """수신 화면 창을 띄운다. 이미 떠 있으면 새로 열지 않고 맨 앞으로만 가져온다."""
    if sys.platform != "win32":
        webbrowser.open(url)
        return
    if _find_window():
        threading.Thread(target=_wait_and_front, args=(False,), daemon=True).start()
        return
    exe = _find_browser()
    if not exe:
        webbrowser.open(url)
        return
    # 평소 쓰는 브라우저 프로필 그대로 앱 창만 새로 연다 (별도 프로필은 Edge 가 로그인·동기화 화면을 띄움).
    # 크기·위치는 창이 뜬 뒤 _bring_to_front 에서 실제 픽셀로 다시 맞춘다.
    subprocess.Popen([exe, "--new-window", f"--app={url}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=_wait_and_front, args=(True,), daemon=True).start()
