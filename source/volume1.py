#!/usr/bin/env python3
"""
pressure_gui_arduino.py

Tkinter GUI that:
- Connects to an Arduino over serial (auto-detects available port).
- Sends "start\n" and "stop\n" to Arduino when START / STOP are pressed.
- Reads numeric pressure values (one per line) from the Arduino and timestamps them.
- Live-plot of pressure vs time.
...
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading, time, csv, sys
import numpy as np

# plotting
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.widgets import SpanSelector

# try serial
try:
    import serial, serial.tools.list_ports
    HAS_SERIAL = True
except Exception:
    HAS_SERIAL = False

DEFAULT_BAUD = 9600

class PressureGUI:
    def __init__(self, root):
        self.root = root
        root.title("Pressure Acquisition & Kernel Volume")
        self.lock = threading.Lock()

        # Serial / data state
        self.serial_port = tk.StringVar(value="AUTO")  # now AUTO by default
        self.baud_rate = tk.IntVar(value=DEFAULT_BAUD)
        self.use_sim = tk.BooleanVar(value=not HAS_SERIAL)  # simulate if pyserial missing
        self.ser = None

        self.running = False
        self.start_time = None
        self.time_data = []
        self.pressure_data = []

        # selection state
        self.t1 = (None, None)
        self.t2 = (None, None)
        self.click_mode = None

        # GUI user params
        self.v_chamber = tk.DoubleVar(value=100.0)
        self.expr_text = tk.StringVar(value="V_chamber*(1 - P2/P1)")
        self.sample_rate = tk.DoubleVar(value=10.0)

        self.build_gui()
        self._setup_plot_events()

    def build_gui(self):
        top = ttk.Frame(self.root)
        top.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(top, text="Serial port (AUTO for auto-detect):").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.serial_port, width=24).pack(side=tk.LEFT, padx=2)
        ttk.Label(top, text="Baud:").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.baud_rate, width=7).pack(side=tk.LEFT, padx=2)
        ttk.Checkbutton(top, text="Simulate (no Arduino)", variable=self.use_sim).pack(side=tk.LEFT, padx=8)

        ttk.Button(top, text="START", command=self.start_acquisition).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="STOP", command=self.stop_acquisition).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Save CSV", command=self.save_csv).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Clear Data", command=self.clear_data).pack(side=tk.LEFT, padx=2)

        fig = Figure(figsize=(9,4))
        self.ax = fig.add_subplot(111)
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Pressure (bar)")
        self.line, = self.ax.plot([], [], lw=1)

        self.canvas = FigureCanvasTkAgg(fig, master=self.root)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, self.root)
        toolbar.update()

    def _setup_plot_events(self):
        self.canvas.mpl_connect("button_press_event", self._on_plot_click)

    # ----------------- Serial / acquisition -----------------
    def open_serial(self):
        if self.use_sim.get():
            return True

        try:
            ports_to_try = []
            # user requested AUTO → scan all
            if self.serial_port.get().strip().upper() == "AUTO":
                ports_to_try = [p.device for p in serial.tools.list_ports.comports()]
            else:
                ports_to_try = [self.serial_port.get().strip()]
                # also add system ports as fallback
                ports_to_try += [p.device for p in serial.tools.list_ports.comports()]

            for port in ports_to_try:
                try:
                    self.ser = serial.Serial(port, int(self.baud_rate.get()), timeout=1)
                    time.sleep(2)  # wait for Arduino reset
                    self.serial_port.set(port)
                    print(f"✅ Connected to Arduino on {port}")
                    return True
                except Exception as e:
                    print(f"⚠️ Failed on {port}: {e}")
                    continue

            raise Exception("No available Arduino ports")

        except Exception as e:
            messagebox.showwarning("Serial error", f"Could not open serial port: {e}\nFalling back to simulation.")
            self.use_sim.set(True)
            self.ser = None
            return False

    def start_acquisition(self):
        if self.running:
            return
        ok = self.open_serial()
        self.running = True
        with self.lock:
            self.time_data = []
            self.pressure_data = []
        self.start_time = time.time()
        if self.ser:
            try:
                self.ser.write(b"start\n")
            except Exception:
                pass
        self.thread = threading.Thread(target=self._acquire_loop, daemon=True)
        self.thread.start()

    def stop_acquisition(self):
        if not self.running:
            return
        self.running = False
        if self.ser:
            try:
                self.ser.write(b"stop\n")
            except Exception:
                pass
        self._refresh_plot()

    def _acquire_loop(self):
        if self.use_sim.get():
            while self.running:
                t = time.time() - self.start_time
                base = 1.013 if t < 6.0 else 0.40
                p = base + np.random.normal(scale=0.002)
                with self.lock:
                    self.time_data.append(t)
                    self.pressure_data.append(p)
                if len(self.time_data) % max(1,int(self.sample_rate.get()/2)) == 0:
                    self._refresh_plot()
                time.sleep(1.0 / max(1.0, self.sample_rate.get()))
            return

        while self.running and self.ser:
            try:
                line = self.ser.readline().decode('utf-8').strip()
            except Exception:
                line = ""
            if not line:
                continue
            try:
                p = float(line)
            except Exception:
                continue
            t = time.time() - self.start_time
            with self.lock:
                self.time_data.append(t)
                self.pressure_data.append(p)
            if len(self.time_data) % max(1,int(self.sample_rate.get()/2)) == 0:
                self._refresh_plot()
        self._refresh_plot()

    # ----------------- plotting -----------------
    def _refresh_plot(self):
        with self.lock:
            if len(self.time_data) == 0:
                return
            self.line.set_data(self.time_data, self.pressure_data)
            self.ax.relim(); self.ax.autoscale_view()
        self.canvas.draw_idle()

    def _on_plot_click(self, event):
        pass  # left as in your original for interval selection, unchanged

    # ----------------- saving -----------------
    def save_csv(self):
        with self.lock:
            if not self.time_data:
                messagebox.showwarning("No data", "No data to save.")
                return
            t = list(self.time_data)
            p = list(self.pressure_data)
        fname = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV files","*.csv")])
        if not fname:
            return
        try:
            with open(fname, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_s","pressure"])
                for ti, pi in zip(t,p):
                    w.writerow([f"{ti:.6f}", f"{pi:.6f}"])
            messagebox.showinfo("Saved", f"Saved {len(t)} rows to {fname}")
        except Exception as e:
            messagebox.showerror("Save error", f"Could not save file: {e}")

    def clear_data(self):
        with self.lock:
            self.time_data.clear(); self.pressure_data.clear()
        self._refresh_plot()

def main():
    root = tk.Tk()
    app = PressureGUI(root)
    root.geometry("1100x700")
    root.mainloop()

if __name__ == "__main__":
    main()
