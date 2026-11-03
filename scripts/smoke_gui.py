"""Open the app's window without showing it for long, and check the parts that
behave differently per platform: sizing, the scrollbar and wheel/trackpad
scrolling. Run by the Build workflow on Windows and macOS before building.

    python scripts/smoke_gui.py
"""

import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viavi_results.gui import App  # noqa: E402


class Event:
    def __init__(self, widget, delta):
        self.widget, self.delta = widget, delta


def main() -> int:
    root = tk.Tk()
    app = App(root)
    root.update()
    print(f"Tk {tk.TkVersion}, screen {root.winfo_screenwidth()}x{root.winfo_screenheight()}, "
          f"window {root.winfo_width()}x{root.winfo_height()}, contents need {app.winfo_reqheight()}")
    assert root.winfo_width() <= root.winfo_screenwidth(), "window wider than the screen"
    assert root.winfo_height() <= root.winfo_screenheight(), "window taller than the screen"

    root.geometry("1000x450")
    root.update()
    app._fit()
    root.update()
    assert app.vbar.winfo_ismapped(), "no scrollbar when the window is shorter than its contents"

    def top():
        root.update()
        return app.canvas.yview()[0]

    app._on_wheel(Event(app.summary, -120))  # one notch down
    after_notch = top()
    assert 0 < after_notch < 0.3, f"one wheel notch scrolled to {after_notch}"
    app._on_wheel(Event(app.summary, -40))  # a small precision-touchpad step
    assert 0 < top() - after_notch < 0.1, "a small touchpad delta scrolled too far"
    app._on_wheel(Event(app.flag_tree, -120))  # over a list: the list scrolls, not the window
    assert top() - after_notch < 0.1
    app._on_touchpad(Event(app.summary, 30 & 0xFFFF))  # Tk 9 trackpad, 30 px up
    app.canvas.yview_moveto(0)

    root.geometry("1000x2000")
    root.update()
    app._fit()
    root.update()
    root.destroy()
    print("GUI smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
