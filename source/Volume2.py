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
import serial.tools.list_ports


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
        # self.serial_port = tk.StringVar(value=DEFAULT_PORT)
        self.serial_port = tk.StringVar(value="AUTO")  # now AUTO by default
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
        self.expr_text = tk.StringVar(value="V_chamber*(1 - P2/P1)")  # default formula


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

            port = self.serial_port.get().strip()
            ports_to_try = []

            if port.upper() == "AUTO":
                ports_to_try = [p.device for p in serial.tools.list_ports.comports()]
            else:
                ports_to_try = [port]

            for p in ports_to_try:
                try:
                    self.ser = serial.Serial(p, int(self.baud_rate.get()), timeout=1)
                    time.sleep(2)  # let Arduino reset fully
                    print(f"✅ Connected to Arduino on {port}")
                    return True
                except Exception:
                    continue

            # If no port worked
            messagebox.showwarning("Serial error", "Could not open any serial port, switching to simulation.")
            self.use_sim.set(True)
            self.ser = None
            return False

        except Exception as e:
            messagebox.showwarning("Serial error", f"Unexpected error: {e}")
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
        self.span1 = SpanSelector(self.ax, self.onselect1, 'horizontal', useblit=True, rectprops=dict(alpha=0.3, facecolor='red'), span_stays=True)
        messagebox.showinfo("Select Initial Interval", "Drag on the plot to select the INITIAL (atmosphere) interval (red).")


    def enable_span2(self):
        if self.span2 is not None:
            self.span2.set_active(False)
        self.span2 = SpanSelector(self.ax, self.onselect2, 'horizontal', useblit=True, rectprops=dict(alpha=0.3, facecolor='blue'), span_stays=True)
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
        if self.t2[0] is not None and self.t2[1] is not None:
            if self.t2[0] > self.t2[1]:
                self.t2 = (self.t2[1], self.t2[0])
        self.click_mode = None
        self.update_stats_labels()
        self._refresh_plot()


    # ----------------- stats / compute -----------------
    def update_stats_labels(self):
        P1_avg = P1_std = P2_avg = P2_std = None
        with self.lock:
            t = np.array(self.time_data)
            p = np.array(self.pressure_data)
        # initial
        if self.t1[0] is not None and self.t1[1] is not None and t.size>0:
            mask1 = (t >= self.t1[0]) & (t <= self.t1[1])
            sel1 = p[mask1]
            if sel1.size > 0:
                P1_avg = float(np.mean(sel1)); P1_std = float(np.std(sel1))
                self.t1_label.config(text=f"Initial interval: {self.t1[0]:.2f} — {self.t1[1]:.2f} s")
                self.t1_stats.config(text=f"P1 avg: {P1_avg:.6g}   std: {P1_std:.6g}")
            else:
                self.t1_label.config(text="Initial interval: (no data in interval)")
                self.t1_stats.config(text="P1 avg: N/A   std: N/A")
        else:
            self.t1_label.config(text="Initial interval: none")
            self.t1_stats.config(text="P1 avg: N/A   std: N/A")


        # final
        if self.t2[0] is not None and self.t2[1] is not None and t.size>0:
            mask2 = (t >= self.t2[0]) & (t <= self.t2[1])
            sel2 = p[mask2]
            if sel2.size > 0:
                P2_avg = float(np.mean(sel2)); P2_std = float(np.std(sel2))
                self.t2_label.config(text=f"Final interval: {self.t2[0]:.2f} — {self.t2[1]:.2f} s")
                self.t2_stats.config(text=f"P2 avg: {P2_avg:.6g}   std: {P2_std:.6g}")
            else:
                self.t2_label.config(text="Final interval: (no data in interval)")
                self.t2_stats.config(text="P2 avg: N/A   std: N/A")
        else:
            self.t2_label.config(text="Final interval: none")
            self.t2_stats.config(text="P2 avg: N/A   std: N/A")


    def compute_volume(self):
        with self.lock:
            t = np.array(self.time_data)
            p = np.array(self.pressure_data)
        if self.t1[0] is None or self.t1[1] is None or self.t2[0] is None or self.t2[1] is None:
            messagebox.showwarning("Intervals missing", "Please select both initial and final intervals before computing.")
            return
        mask1 = (t >= self.t1[0]) & (t <= self.t1[1])
        mask2 = (t >= self.t2[0]) & (t <= self.t2[1])
        sel1 = p[mask1]; sel2 = p[mask2]
        if sel1.size == 0 or sel2.size == 0:
            messagebox.showwarning("No data", "One of the selected intervals contains no data. Acquire more data or expand intervals.")
            return
        P1 = float(np.mean(sel1)); P1_std = float(np.std(sel1))
        P2 = float(np.mean(sel2)); P2_std = float(np.std(sel2))
        V_chamber = float(self.v_chamber.get())


        # evaluate user expression safely-ish
        local_env = {"P1":P1, "P2":P2, "V_chamber":V_chamber, "np":np}
        expr = self.expr_text.get().strip()
        try:
            if "V_kernel" in expr:
                exec(expr, {}, local_env)
                V_kernel = local_env.get("V_kernel", None)
            else:
                V_kernel = eval(expr, {}, local_env)
        except Exception as e:
            messagebox.showerror("Evaluation error", f"Error evaluating expression:\n{e}")
            return


        if V_kernel is None:
            self.output_label.config(text="Kernel Volume: evaluation returned None")
        else:
            self.output_label.config(text=f"Kernel Volume: {float(V_kernel):.6g} (units per V_chamber)")
            # also show a summary dialog
            info = (f"P1 avg = {P1:.6g}, P1 std = {P1_std:.6g}\n"
                    f"P2 avg = {P2:.6g}, P2 std = {P2_std:.6g}\n"
                    f"V_chamber = {V_chamber}\n"
                    f"Computed kernel volume = {V_kernel}\n\n"
                    "Note: ensure units match V_chamber.")
            messagebox.showinfo("Computation complete", info)


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
        self.t1 = (None, None); self.t2 = (None, None)
        self._refresh_plot()
        self.update_stats_labels()


    # ----------------- auto-detect -----------------
    def auto_detect(self):
        # pick low-std regions near start and near end
        with self.lock:
            t = np.array(self.time_data)
            p = np.array(self.pressure_data)
        if t.size < 10:
            messagebox.showwarning("Insufficient data", "Acquire more data before auto-detecting.")
            return
        window = max(3, int(0.5 * (self.sample_rate.get() or 10)))
        stds = np.array([np.std(p[max(0,i-window):i+1]) for i in range(p.size)])
        thr = np.percentile(stds, 20)
        start_idx = None
        for i in range(0, len(stds)-window):
            if np.all(stds[i:i+window] <= thr):
                start_idx = i; break
        end_idx = None
        for i in range(len(stds)-1, window-1, -1):
            if np.all(stds[i-window+1:i+1] <= thr):
                end_idx = i; break
        if start_idx is None or end_idx is None or start_idx >= end_idx:
            messagebox.showinfo("Auto-detect result", "Could not auto-identify stable regions. Try selecting manually.")
            return
        self.t1 = (t[max(0,start_idx)], t[min(len(t)-1, start_idx+window-1)])
        self.t2 = (t[max(0,end_idx-window+1)], t[min(len(t)-1, end_idx)])
        self.update_stats_labels()
        self._refresh_plot()
        messagebox.showinfo("Auto-detect", f"Initial: {self.t1[0]:.2f}—{self.t1[1]:.2f}s\nFinal: {self.t2[0]:.2f}—{self.t2[1]:.2f}s")


def main():
    root = tk.Tk()
    app = PressureGUI(root)
    root.geometry("1100x700")
    root.mainloop()


if __name__ == "__main__":
    main()