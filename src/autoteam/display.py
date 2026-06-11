"""自动设置虚拟显示器（无头服务器）— 在 import 时执行，Windows/macOS 跳过"""

import logging
import os
import subprocess
import sys
import atexit

logger = logging.getLogger(__name__)

_vdisplay = None
_fallback_proc = None

# Windows 和 macOS 不需要虚拟显示器（有真实显示器或 Playwright 自带 headless）
if sys.platform == "linux" and not os.environ.get("DISPLAY"):
    try:
        from xvfbwrapper import Xvfb

        _vdisplay = Xvfb(width=1280, height=800)
        _vdisplay.start()
    except (ImportError, OSError, RuntimeError):
        try:
            _fallback_proc = subprocess.Popen(
                ["Xvfb", ":99", "-screen", "0", "1280x800x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            os.environ["DISPLAY"] = ":99"
        except Exception:
            pass


def stop_virtual_display():
    global _vdisplay, _fallback_proc
    if _vdisplay is not None:
        try:
            _vdisplay.stop()
        except Exception as exc:
            logger.warning("[显示器] 停止 Xvfb wrapper 失败: %s", exc)
        finally:
            _vdisplay = None
    if _fallback_proc is not None:
        try:
            _fallback_proc.terminate()
            _fallback_proc.wait(timeout=5)
        except Exception:
            try:
                _fallback_proc.kill()
            except Exception:
                pass
        finally:
            _fallback_proc = None


atexit.register(stop_virtual_display)
