"""Full-desktop overlay: drag to pick an area, Enter = whole monitor, Esc = cancel."""
import tkinter as tk

from . import winutil


def select_region(master=None):
    """master: an existing Tk root to open on (its thread must be the caller's)."""
    vx, vy, vw, vh = winutil.virtual_screen()
    result = {}

    root = tk.Toplevel(master) if master else tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    root.attributes("-alpha", 0.35)
    root.attributes("-topmost", True)
    canvas = tk.Canvas(root, cursor="cross", bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    mx, my = winutil.cursor_pos()
    m = winutil.monitor_at(mx, my)
    canvas.create_text(m[0] - vx + m[2] // 2, m[1] - vy + 60, fill="white", font=("Segoe UI", 20),
                       text="Drag to select an area  ·  Enter = whole screen  ·  Esc = cancel")
    state = {"start": None, "rect": None}

    def press(e):
        state["start"] = (e.x_root, e.y_root)

    def drag(e):
        if not state["start"]:
            return
        sx, sy = state["start"]
        if state["rect"]:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(sx - vx, sy - vy, e.x_root - vx, e.y_root - vy,
                                                outline="#ff3b30", width=3, fill="white")

    def release(e):
        if not state["start"]:
            return
        sx, sy = state["start"]
        x, y = min(sx, e.x_root), min(sy, e.y_root)
        w, h = abs(e.x_root - sx), abs(e.y_root - sy)
        if w > 50 and h > 50:
            result["r"] = (x, y, w, h)
            root.destroy()
        else:
            state["start"] = None

    def full(_e):
        px, py = winutil.cursor_pos()
        result["r"] = winutil.monitor_at(px, py)
        root.destroy()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", release)
    root.bind("<Return>", full)
    root.bind("<Escape>", lambda _e: root.destroy())
    root.after(50, root.focus_force)
    if master:
        master.wait_window(root)
    else:
        root.mainloop()
    return result.get("r")
