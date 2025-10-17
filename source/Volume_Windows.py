#!/usr/bin/env python3
"""
pressure_gui_arduino.py


Tkinter GUI that:
- Connects to an Arduino over serial (or simulates if serial unavailable).
- Sends "start\n" and "stop\n" to Arduino when START / STOP are pressed.
- Reads numeric pressure values (one per line) from the Arduino and timestamps them.
- Live-plot of pressure vs time.
- Two ways to select intervals:
    1) Drag using SpanSelector (one red span = initial, one blue span = final).
    2) Click-mode: press 'Set Initial Start', click on plot to set start time, etc.
- Computes average and std within each selected interval and shows stability.
- Lets operator enter a Python expression (uses P1,P2,V_chamber) to compute kernel volume.
- Save CSV of raw time/pressure data and save averages log.


Usage:
    python pressure_gui_arduino.py
Change default SERIAL_PORT if necessary.
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
    import serial
    HAS_SERIAL = True
except Exception:
    HAS_SERIAL = False


DEFAULT_PORT = "COM3" if sys.platform.startswith("win") else "/dev/ttyACM0"
DEFAULT_BAUD = 9600


class PressureGUI:
    def __init__(self, root):
        self.root = root
        root.title("Pressure Acquisition & Kernel Volume")
        self.lock = threading.Lock()


        # Serial / data state
        self.serial_port = tk.StringVar(value=DEFAULT_PORT)
        self.baud_rate = tk.IntVar(value=DEFAULT_BAUD)
        self.use_sim = tk.BooleanVar(value=not HAS_SERIAL)  # simulate if pyserial missing
        self.ser = None


        self.running = False
        self.start_time = None
        self.time_data = []   # seconds relative to acquisition start
        self.pressure_data = []


        # selection state
        self.t1 = (None, None)  # initial interval (start, end)
        self.t2 = (None, None)  # final interval (start, end)
        self.click_mode = None  # one of: 'init_start','init_end','final_start','final_end', None


        # GUI user params
        self.v_chamber = tk.DoubleVar(value=100.0)
        self.expr_text = tk.StringVar(value="2 * V_chamber * (P1 - P2) / (1.0 - P2)")


        # Sample rate hint (we will read serial lines as they arrive)
        self.sample_rate = tk.DoubleVar(value=10.0)


        self.build_gui()
        self._setup_plot_events()


    def build_gui(self):
        # top control frame
        top = ttk.Frame(self.root)
        top.pack(fill=tk.X, padx=6, pady=6)


        ttk.Label(top, text="Serial port:").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.serial_port, width=14).pack(side=tk.LEFT, padx=2)
        ttk.Label(top, text="Baud:").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.baud_rate, width=7).pack(side=tk.LEFT, padx=2)
        ttk.Checkbutton(top, text="Simulate (no Arduino)", variable=self.use_sim).pack(side=tk.LEFT, padx=8)


        ttk.Button(top, text="START", command=self.start_acquisition).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="STOP", command=self.stop_acquisition).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Save CSV", command=self.save_csv).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Clear Data", command=self.clear_data).pack(side=tk.LEFT, padx=2)


        # plot area
        fig = Figure(figsize=(9,4))
        self.ax = fig.add_subplot(111)
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Pressure (bar)")
        self.line, = self.ax.plot([], [], lw=1)


        self.canvas = FigureCanvasTkAgg(fig, master=self.root)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, self.root)
        toolbar.update()


        # interval controls
        intf = ttk.Frame(self.root)
        intf.pack(fill=tk.X, padx=6, pady=4)


        ttk.Label(intf, text="Select intervals:").pack(side=tk.LEFT)
        ttk.Button(intf, text="Drag: Initial (red)", command=self.enable_span1).pack(side=tk.LEFT, padx=4)
        ttk.Button(intf, text="Drag: Final (blue)", command=self.enable_span2).pack(side=tk.LEFT, padx=4)


        # Click-mode buttons: set start/end by clicking
        ttk.Separator(intf, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=6, fill=tk.Y)
        ttk.Label(intf, text="Click-mode:").pack(side=tk.LEFT)
        ttk.Button(intf, text="Set Initial Start", command=lambda: self.set_click_mode('init_start')).pack(side=tk.LEFT, padx=2)
        ttk.Button(intf, text="Set Initial End",   command=lambda: self.set_click_mode('init_end')).pack(side=tk.LEFT, padx=2)
        ttk.Button(intf, text="Set Final Start",   command=lambda: self.set_click_mode('final_start')).pack(side=tk.LEFT, padx=2)
        ttk.Button(intf, text="Set Final End",     command=lambda: self.set_click_mode('final_end')).pack(side=tk.LEFT, padx=2)
        ttk.Button(intf, text="Auto-detect stable", command=self.auto_detect).pack(side=tk.LEFT, padx=8)


        # stats area
        stats = ttk.Frame(self.root)
        stats.pack(fill=tk.X, padx=6, pady=4)
        self.t1_label = ttk.Label(stats, text="Initial interval: none")
        self.t1_label.grid(row=0, column=0, sticky='w')
        self.t1_stats = ttk.Label(stats, text="P1 avg: N/A   std: N/A")
        self.t1_stats.grid(row=0, column=1, sticky='w', padx=8)
        self.t2_label = ttk.Label(stats, text="Final interval: none")
        self.t2_label.grid(row=1, column=0, sticky='w')
        self.t2_stats = ttk.Label(stats, text="P2 avg: N/A   std: N/A")
        self.t2_stats.grid(row=1, column=1, sticky='w', padx=8)


        # formula and compute
        frm = ttk.Frame(self.root)
        frm.pack(fill=tk.X, padx=6, pady=6)
        ttk.Label(frm, text="V_chamber:").pack(side=tk.LEFT)
        ttk.Entry(frm, textvariable=self.v_chamber, width=10).pack(side=tk.LEFT, padx=4)
        ttk.Label(frm, text="Volume formula (use P1,P2,V_chamber):").pack(side=tk.LEFT, padx=6)
        ttk.Entry(frm, textvariable=self.expr_text, width=40).pack(side=tk.LEFT, padx=4)
        ttk.Button(frm, text="Compute Volume", command=self.compute_volume).pack(side=tk.LEFT, padx=6)


        # output
        out = ttk.Frame(self.root)
        out.pack(fill=tk.X, padx=6, pady=6)
        self.output_label = ttk.Label(out, text="Kernel Volume: N/A", font=("TkDefaultFont", 12, "bold"))
        self.output_label.pack(anchor='w')


        # span selectors (initialized later when used)
        self.span1 = None
        self.span2 = None


    def _setup_plot_events(self):
        self.canvas.mpl_connect("button_press_event", self._on_plot_click)


    # ----------------- Serial / acquisition -----------------
    def open_serial(self):
        if self.use_sim.get():
            return True
        try:
            if self.ser and self.ser.is_open:
                return True
            self.ser = serial.Serial(self.serial_port.get(), int(self.baud_rate.get()), timeout=1)
            # small pause to let Arduino reset if it does
            time.sleep(1.5)
            return True
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
        # if hardware, tell Arduino to start
        if self.ser:
            try:
                self.ser.write(b"start\n")
            except Exception:
                pass
        # if sim, nothing to send
        self.thread = threading.Thread(target=self._acquire_loop, daemon=True)
        self.thread.start()


    def stop_acquisition(self):
        if not self.running:
            return
        self.running = False
        # tell Arduino to stop if connected
        if self.ser:
            try:
                self.ser.write(b"stop\n")
            except Exception:
                pass
            # don't close serial here, leave it open for reuse
        # final refresh
        self._refresh_plot()
        self.update_stats_labels()


    def _acquire_loop(self):
        # Read serial lines as they arrive (hardware) or generate simulated data at sample_rate (simulate).
        if self.use_sim.get():
            # simulated run: hold baseline for 6s then drop to simulate valve opening
            while self.running:
                t = time.time() - self.start_time
                base = 1.013 if t < 6.0 else 0.40  # bar
                p = base + np.random.normal(scale=0.002)
                with self.lock:
                    self.time_data.append(t)
                    self.pressure_data.append(p)
                if len(self.time_data) % max(1,int(self.sample_rate.get()/2)) == 0:
                    self._refresh_plot()
                time.sleep(1.0 / max(1.0, self.sample_rate.get()))
            return


        # hardware: read lines produced by Arduino
        # Arduino is expected to print a single numeric pressure per line (e.g., "0.012345\n")
        while self.running and self.ser:
            try:
                line = self.ser.readline().decode('utf-8').strip()
            except Exception:
                line = ""
            if not line:
                continue
            # ignore echo text from Arduino that isn't numeric
            try:
                p = float(line)
            except Exception:
                # optionally show debug prints
                # print("non-numeric from arduino:", line)
                continue
            t = time.time() - self.start_time
            with self.lock:
                self.time_data.append(t)
                self.pressure_data.append(p)
            # update plot occasionally
            if len(self.time_data) % max(1,int(self.sample_rate.get()/2)) == 0:
                self._refresh_plot()
        # finish
        self._refresh_plot()


    # ----------------- plotting / selection -----------------
    def _refresh_plot(self):
        with self.lock:
            if len(self.time_data) == 0:
                return
            self.line.set_data(self.time_data, self.pressure_data)
            self.ax.relim(); self.ax.autoscale_view()
        # draw current interval spans as patches by clearing old rectangles and re-drawing:
        # (for simplicity, we'll just re-create span selectors when intervals set)
        self.canvas.draw_idle()


    def enable_span1(self):
        # red span for initial
        if self.span1 is not None:
            self.span1.set_active(False)
        self.span1 = SpanSelector(self.ax, self.onselect1, 'horizontal', useblit=True, props=dict(alpha=0.3, facecolor='red'))
        messagebox.showinfo("Select Initial Interval", "Drag on the plot to select the INITIAL (atmosphere) interval (red).")


    def enable_span2(self):
        if self.span2 is not None:
            self.span2.set_active(False)
        self.span2 = SpanSelector(self.ax, self.onselect2, 'horizontal', useblit=True, props=dict(alpha=0.3, facecolor='blue'))
        messagebox.showinfo("Select Final Interval", "Drag on the plot to select the FINAL (after valve open) interval (blue).")


    def onselect1(self, xmin, xmax):
        if xmin > xmax:
            xmin, xmax = xmax, xmin
        self.t1 = (xmin, xmax)
        self.update_stats_labels()


    def onselect2(self, xmin, xmax):
        if xmin > xmax:
            xmin, xmax = xmax, xmin
        self.t2 = (xmin, xmax)
        self.update_stats_labels()


    def set_click_mode(self, mode):
        # allow one click to set a particular endpoint; next click on plot sets the time
        self.click_mode = mode
        messagebox.showinfo("Click-mode", f"Click on the plot to set {mode.replace('_',' ')}")


    def _on_plot_click(self, event):
        # handle user clicks on the plot when click_mode is set
        if event.inaxes != self.ax:
            return
        if self.click_mode is None:
            return
        t_click = event.xdata
        if t_click is None:
            return
        if self.click_mode == 'init_start':
            a,b = self.t1
            self.t1 = (t_click, b)
        elif self.click_mode == 'init_end':
            a,b = self.t1
            self.t1 = (a, t_click)
        elif self.click_mode == 'final_start':
            a,b = self.t2
            self.t2 = (t_click, b)
        elif self.click_mode == 'final_end':
            a,b = self.t2
            self.t2 = (a, t_click)
        # normalize intervals if both endpoints set and swapped
        if self.t1[0] is not None and self.t1[1] is not None:
            if self.t1[0] > self.t1[1]:
                self.t1 = (self.t1[1], self.t1[0])
        if self.t2[0