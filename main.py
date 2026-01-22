import csv
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from ortools.sat.python import cp_model



# Helpers (time + labeling)

def format_hour(hour: int) -> str:
    """Converts 24h integer hour to 12h time string."""
    hour = hour % 24
    if hour == 0:
        return "12:00 AM"
    if hour < 12:
        return f"{hour}:00 AM"
    if hour == 12:
        return "12:00 PM"
    return f"{hour - 12}:00 PM"


def label_shift(day_start: int, day_end: int, shift_start: int) -> str:
    """Opening/Mid/Closing based on where the shift START falls within the day thirds."""
    day_len = day_end - day_start
    if day_len <= 0:
        return "Mid"
    third = day_len / 3.0
    pos = shift_start - day_start
    if pos < third:
        return "Opening"
    elif pos < 2 * third:
        return "Mid"
    else:
        return "Closing"



# Scheduling core (OR-Tools)
def build_candidate_shifts(day_start_hour: int, day_length_hours: int, allowed_lengths=(4, 5)):
    day_end_hour = day_start_hour + day_length_hours
    shifts = []
    for L in allowed_lengths:
        for start in range(day_start_hour, day_end_hour - L + 1):
            shifts.append((start, start + L))
    return shifts


def overlaps_unavailability(shift, unavailable_blocks):
    start, end = shift
    for h in range(start, end):
        if h in unavailable_blocks:
            return True
    return False


def schedule_workers_softmin_hardmax(
    employees,
    day_start_hour=8,
    day_length_hours=12,
    allowed_shift_lengths=(4, 5),
    min_workers_per_hour=1,
    max_workers_per_hour=2,
    max_one_shift_per_employee=True,
    solver_time_limit_s=10.0,
):
    """
    employees: [{"name": str, "unavailable": set(int)}]
      unavailable hour h means employee cannot work during [h, h+1)

    Coverage behavior:
      - hard max: coverage[h] <= max_workers_per_hour
      - soft min: coverage[h] + understaff[h] >= min_workers_per_hour
    """
    day_end_hour = day_start_hour + day_length_hours
    hours = list(range(day_start_hour, day_end_hour))

    all_shifts = build_candidate_shifts(day_start_hour, day_length_hours, allowed_shift_lengths)

    feasible_shifts = []
    for e in employees:
        unavailable = set(e.get("unavailable", set()))
        feasible = [s for s in all_shifts if not overlaps_unavailability(s, unavailable)]
        feasible_shifts.append(feasible)

    model = cp_model.CpModel()

    # Decision vars: x[e, s] = 1 if employee e works shift s
    x = {}
    for e_idx, shifts in enumerate(feasible_shifts):
        for s_idx, s in enumerate(shifts):
            x[(e_idx, s_idx)] = model.NewBoolVar(f"x_e{e_idx}_s{s[0]}_{s[1]}")

    # Optional: at most one shift per employee
    if max_one_shift_per_employee:
        for e_idx, shifts in enumerate(feasible_shifts):
            model.Add(sum(x[(e_idx, s_idx)] for s_idx in range(len(shifts))) <= 1)

    # Coverage vars + understaff vars (soft min, hard max)
    coverage_vars = {}
    understaff = {}

    for h in hours:
        cover_terms = []
        for e_idx, shifts in enumerate(feasible_shifts):
            for s_idx, (start, end) in enumerate(shifts):
                if start <= h < end:
                    cover_terms.append(x[(e_idx, s_idx)])

        cov = model.NewIntVar(0, len(employees), f"coverage_{h}")
        model.Add(cov == (sum(cover_terms) if cover_terms else 0))
        coverage_vars[h] = cov

        # hard cap
        model.Add(cov <= max_workers_per_hour)

        # soft min
        understaff[h] = model.NewIntVar(0, 1000, f"understaff_{h}")
        model.Add(cov + understaff[h] >= min_workers_per_hour)

    # Work hours per employee (for fairness)
    work_hours = []
    for e_idx, shifts in enumerate(feasible_shifts):
        wh = model.NewIntVar(0, 24, f"workhours_e{e_idx}")
        model.Add(
            wh == sum(
                x[(e_idx, s_idx)] * (shifts[s_idx][1] - shifts[s_idx][0])
                for s_idx in range(len(shifts))
            )
        )
        work_hours.append(wh)

    # Fairness: minimize spread (max - min)
    max_work = model.NewIntVar(0, 24, "max_work")
    min_work = model.NewIntVar(0, 24, "min_work")
    model.AddMaxEquality(max_work, work_hours)
    model.AddMinEquality(min_work, work_hours)
    spread = model.NewIntVar(0, 24, "spread")
    model.Add(spread == max_work - min_work)

    # Total coverage (to avoid unnecessary 2-worker blocks)
    total_coverage = model.NewIntVar(0, 10000, "total_coverage")
    model.Add(total_coverage == sum(coverage_vars[h] for h in hours))

    # Total understaff (primary objective)
    total_understaff = model.NewIntVar(0, 10000, "total_understaff")
    model.Add(total_understaff == sum(understaff[h] for h in hours))

    # Objective: strongly minimize understaff, then fairness, then keep coverage lean
    model.Minimize(total_understaff * 100000 + spread * 1000 + total_coverage)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(solver_time_limit_s)
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "no_solution"}

    # Extract assignments
    assignments = []
    for e_idx, shifts in enumerate(feasible_shifts):
        chosen = None
        for s_idx, s in enumerate(shifts):
            if solver.Value(x[(e_idx, s_idx)]) == 1:
                chosen = s
                break
        assignments.append(chosen)

    coverage_by_hour = {h: int(solver.Value(coverage_vars[h])) for h in hours}
    understaff_by_hour = {h: int(solver.Value(understaff[h])) for h in hours}

    return {
        "status": "ok",
        "day_start_hour": day_start_hour,
        "day_end_hour": day_end_hour,
        "min_workers_per_hour": int(min_workers_per_hour),
        "max_workers_per_hour": int(max_workers_per_hour),
        "fairness_spread": int(solver.Value(spread)),
        "total_understaff": int(solver.Value(total_understaff)),
        "coverage_by_hour": coverage_by_hour,
        "understaff_by_hour": understaff_by_hour,
        "assignments": [
            {
                "employee": employees[i]["name"],
                "shift": (s[0], s[1]) if s else None,
                "work_hours": int(solver.Value(work_hours[i])),
                "unavailable": sorted(list(employees[i].get("unavailable", set()))),
            }
            for i, s in enumerate(assignments)
        ],
    }



# Demo datasets

DEMO_DATASETS = {
    "Feasible (clean schedule)": {
        "day_start": 8,
        "day_length": 12,
        "min_cov": 1,
        "max_cov": 2,
        "employees": [
            {"name": "Alex", "unavailable": {10}},
            {"name": "Brianna", "unavailable": {12}},
            {"name": "Carlos", "unavailable": {9, 14}},
            {"name": "Diana", "unavailable": {11}},
            {"name": "Ethan", "unavailable": {15}},
            {"name": "Faith", "unavailable": set()},
            {"name": "Gabriel", "unavailable": {13}},
        ],
        "note": "Should cover all hours with 1–2 workers. Good baseline demo.",
    },
    "Understaff demo (forced gap at 12 PM)": {
        "day_start": 8,
        "day_length": 12,
        "min_cov": 1,
        "max_cov": 2,
        "employees": [
            {"name": "Alex", "unavailable": {12}},
            {"name": "Brianna", "unavailable": {12}},
            {"name": "Carlos", "unavailable": {12}},
            {"name": "Diana", "unavailable": {12}},
            {"name": "Ethan", "unavailable": {12}},
            {"name": "Faith", "unavailable": {9, 15}},
        ],
        "note": "Everyone is unavailable at 12–1 PM, so you will see UNDERSTAFF there.",
    },
    "Understaff demo (2-hour blackout 11–1)": {
        "day_start": 8,
        "day_length": 12,
        "min_cov": 1,
        "max_cov": 2,
        "employees": [
            {"name": "Alex", "unavailable": {11, 12}},
            {"name": "Brianna", "unavailable": {11, 12}},
            {"name": "Carlos", "unavailable": {11, 12}},
            {"name": "Diana", "unavailable": {11, 12}},
            {"name": "Ethan", "unavailable": {11, 12}},
            {"name": "Faith", "unavailable": {9, 15}},
        ],
        "note": "You should see UNDERSTAFF for 11–12 and 12–1.",
    },
    "Busier day (min=2, max=2)": {
        "day_start": 8,
        "day_length": 12,
        "min_cov": 2,
        "max_cov": 2,
        "employees": [
            {"name": "Alex", "unavailable": {10}},
            {"name": "Brianna", "unavailable": {12}},
            {"name": "Carlos", "unavailable": {9, 14}},
            {"name": "Diana", "unavailable": {11}},
            {"name": "Ethan", "unavailable": {15}},
            {"name": "Faith", "unavailable": set()},
            {"name": "Gabriel", "unavailable": {13}},
            {"name": "Hannah", "unavailable": {16}},
            {"name": "Ivan", "unavailable": {8, 17}},
        ],
        "note": "Requires 2 workers every hour (exactly 2). Understaff may appear if impossible.",
    },
}



# Tkinter UI

class SchedulerUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Worker Scheduler (4–5h shifts + class blocks)")
        self.geometry("1080x800")

        self.employees = []   # [{"name": str, "unavailable": set(int)}]
        self.last_result = None  # store last solver result for CSV export

        # ===== Top controls =====
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Day start (hour):").grid(row=0, column=0, sticky="w")
        self.day_start_var = tk.IntVar(value=8)
        ttk.Spinbox(top, from_=0, to=23, textvariable=self.day_start_var, width=6).grid(row=0, column=1, padx=6)

        ttk.Label(top, text="Day length (hours):").grid(row=0, column=2, sticky="w")
        self.day_length_var = tk.IntVar(value=12)
        ttk.Spinbox(top, from_=1, to=24, textvariable=self.day_length_var, width=6).grid(row=0, column=3, padx=6)

        ttk.Label(top, text="Min workers/hour:").grid(row=0, column=4, sticky="w")
        self.min_cov_var = tk.IntVar(value=1)
        ttk.Spinbox(top, from_=0, to=50, textvariable=self.min_cov_var, width=6).grid(row=0, column=5, padx=6)

        ttk.Label(top, text="Max workers/hour:").grid(row=0, column=6, sticky="w")
        self.max_cov_var = tk.IntVar(value=2)
        ttk.Spinbox(top, from_=0, to=50, textvariable=self.max_cov_var, width=6).grid(row=0, column=7, padx=6)

        ttk.Button(top, text="Run Scheduler", command=self.run_scheduler).grid(row=0, column=8, padx=10)
        ttk.Button(top, text="Export Coverage CSV", command=self.export_coverage_csv).grid(row=0, column=9, padx=6)

        # ===== Demo selector row =====
        demo_row = ttk.Frame(self, padding=(10, 0, 10, 10))
        demo_row.pack(fill="x")

        ttk.Label(demo_row, text="Demo dataset:").pack(side="left")

        self.demo_var = tk.StringVar(value=list(DEMO_DATASETS.keys())[0])
        self.demo_combo = ttk.Combobox(
            demo_row,
            textvariable=self.demo_var,
            values=list(DEMO_DATASETS.keys()),
            state="readonly",
            width=42,
        )
        self.demo_combo.pack(side="left", padx=8)

        ttk.Button(demo_row, text="Load Selected Demo", command=self.load_selected_demo).pack(side="left")

        self.demo_note = ttk.Label(demo_row, text="", foreground="#444")
        self.demo_note.pack(side="left", padx=12, fill="x", expand=True)

        self.demo_combo.bind("<<ComboboxSelected>>", lambda _e: self.update_demo_note())
        self.update_demo_note()

        # ===== Employee entry =====
        entry = ttk.Labelframe(self, text="Add Employee", padding=10)
        entry.pack(fill="x", padx=10, pady=6)

        ttk.Label(entry, text="Name:").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar()
        ttk.Entry(entry, textvariable=self.name_var, width=24).grid(row=0, column=1, padx=6)

        ttk.Label(entry, text="Class hours (comma-separated, 0–23):").grid(row=0, column=2, sticky="w")
        self.unavail_var = tk.StringVar()
        ttk.Entry(entry, textvariable=self.unavail_var, width=35).grid(row=0, column=3, padx=6)

        ttk.Label(entry, text="Example: 12 blocks 12–1 PM. 9,15 blocks 9–10 AM & 3–4 PM").grid(
            row=1, column=2, columnspan=2, sticky="w"
        )

        ttk.Button(entry, text="Add", command=self.add_employee).grid(row=0, column=4, padx=10)

        # ===== Employee list =====
        mid = ttk.Frame(self, padding=10)
        mid.pack(fill="both", expand=True)

        self.emp_tree = ttk.Treeview(mid, columns=("name", "unavailable"), show="headings", height=8)
        self.emp_tree.heading("name", text="Employee")
        self.emp_tree.heading("unavailable", text="Class/Unavailable Hours (24h ints)")
        self.emp_tree.column("name", width=200)
        self.emp_tree.column("unavailable", width=560)
        self.emp_tree.pack(side="left", fill="both", expand=True)

        emp_scroll = ttk.Scrollbar(mid, orient="vertical", command=self.emp_tree.yview)
        self.emp_tree.configure(yscrollcommand=emp_scroll.set)
        emp_scroll.pack(side="left", fill="y")

        btns = ttk.Frame(mid)
        btns.pack(side="left", fill="y", padx=10)
        ttk.Button(btns, text="Remove Selected", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="Clear All", command=self.clear_all).pack(fill="x", pady=2)

        # ===== Results split view (PanedWindow) =====
        results = ttk.Labelframe(self, text="Results", padding=10)
        results.pack(fill="both", expand=True, padx=10, pady=6)

        self.paned = ttk.PanedWindow(results, orient=tk.VERTICAL)
        self.paned.pack(fill="both", expand=True)

        # Top pane: Assignments
        assignments_frame = ttk.Frame(self.paned)
        self.paned.add(assignments_frame, weight=1)

        self.res_tree = ttk.Treeview(
            assignments_frame,
            columns=("employee", "label", "shift", "work_hours", "unavailable"),
            show="headings",
            height=12,
        )
        for col, w in [
            ("employee", 170),
            ("label", 90),
            ("shift", 240),
            ("work_hours", 90),
            ("unavailable", 360),
        ]:
            self.res_tree.heading(col, text=col.replace("_", " ").title())
            self.res_tree.column(col, width=w)

        res_scroll = ttk.Scrollbar(assignments_frame, orient="vertical", command=self.res_tree.yview)
        self.res_tree.configure(yscrollcommand=res_scroll.set)

        self.res_tree.pack(side="left", fill="both", expand=True)
        res_scroll.pack(side="right", fill="y")

        # Bottom pane: Coverage text (with red understaff lines) + scrollbar
        coverage_frame = ttk.Frame(self.paned)
        self.paned.add(coverage_frame, weight=1)

        self.coverage_text = tk.Text(coverage_frame, height=16, wrap="none")
        cov_scroll = ttk.Scrollbar(coverage_frame, orient="vertical", command=self.coverage_text.yview)
        self.coverage_text.configure(yscrollcommand=cov_scroll.set)

        # Tag for understaff lines
        self.coverage_text.tag_configure("understaff", foreground="red")

        self.coverage_text.pack(side="left", fill="both", expand=True)
        cov_scroll.pack(side="right", fill="y")

    # -------- Demo handling --------
    def update_demo_note(self):
        key = self.demo_var.get()
        note = DEMO_DATASETS.get(key, {}).get("note", "")
        self.demo_note.config(text=note)

    def load_selected_demo(self):
        key = self.demo_var.get()
        data = DEMO_DATASETS.get(key)
        if not data:
            return

        self.clear_all()

        self.day_start_var.set(data["day_start"])
        self.day_length_var.set(data["day_length"])
        self.min_cov_var.set(data["min_cov"])
        self.max_cov_var.set(data["max_cov"])

        for e in data["employees"]:
            self.employees.append({"name": e["name"], "unavailable": set(e["unavailable"])})
            self.emp_tree.insert("", "end", values=(e["name"], ",".join(str(h) for h in sorted(e["unavailable"]))))

        messagebox.showinfo("Demo Loaded", f"Loaded: {key}\n\nClick 'Run Scheduler' to generate the schedule.")

    # -------- Employee CRUD --------
    def add_employee(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Missing name", "Please enter an employee name.")
            return

        unavail_raw = self.unavail_var.get().strip()
        unavailable = set()
        if unavail_raw:
            try:
                parts = [p.strip() for p in unavail_raw.split(",") if p.strip() != ""]
                unavailable = set(int(p) for p in parts)
                for h in unavailable:
                    if h < 0 or h > 23:
                        raise ValueError("Hours must be 0–23.")
            except Exception:
                messagebox.showerror("Invalid hours", "Enter class hours like: 12 or 9,15 (integers 0–23).")
                return

        self.employees.append({"name": name, "unavailable": unavailable})
        self.emp_tree.insert("", "end", values=(name, ",".join(str(h) for h in sorted(unavailable))))

        self.name_var.set("")
        self.unavail_var.set("")

    def remove_selected(self):
        sel = self.emp_tree.selection()
        if not sel:
            return
        idxs = [self.emp_tree.index(item) for item in sel]
        for item in sel:
            self.emp_tree.delete(item)
        for i in sorted(idxs, reverse=True):
            if 0 <= i < len(self.employees):
                self.employees.pop(i)

    def clear_all(self):
        self.employees.clear()
        self.last_result = None

        for item in self.emp_tree.get_children():
            self.emp_tree.delete(item)

        for item in self.res_tree.get_children():
            self.res_tree.delete(item)

        self.coverage_text.delete("1.0", tk.END)

    # -------- Run scheduler + render --------
    def run_scheduler(self):
        if len(self.employees) == 0:
            messagebox.showerror("No employees", "Add at least one employee.")
            return

        day_start = int(self.day_start_var.get())
        day_len = int(self.day_length_var.get())
        min_cov = int(self.min_cov_var.get())
        max_cov = int(self.max_cov_var.get())

        if min_cov < 0 or max_cov < 0:
            messagebox.showerror("Invalid coverage", "Min/Max workers/hour must be >= 0.")
            return
        if min_cov > max_cov:
            messagebox.showerror("Invalid coverage", "Min workers/hour cannot be greater than Max workers/hour.")
            return

        result = schedule_workers_softmin_hardmax(
            self.employees,
            day_start_hour=day_start,
            day_length_hours=day_len,
            allowed_shift_lengths=(4, 5),
            min_workers_per_hour=min_cov,
            max_workers_per_hour=max_cov,
            max_one_shift_per_employee=True,
            solver_time_limit_s=10.0,
        )

        # Clear result views
        for item in self.res_tree.get_children():
            self.res_tree.delete(item)
        self.coverage_text.delete("1.0", tk.END)

        if result["status"] != "ok":
            self.last_result = None
            messagebox.showerror("No solution", "Solver did not find a schedule (unexpected with soft min).")
            return

        self.last_result = result

        day_end = result["day_end_hour"]

        # Assignments table
        for a in result["assignments"]:
            if a["shift"] is None:
                self.res_tree.insert(
                    "",
                    "end",
                    values=(a["employee"], "-", "NOT SCHEDULED", a["work_hours"], ",".join(map(str, a["unavailable"]))),
                )
            else:
                s0, s1 = a["shift"]
                lab = label_shift(day_start, day_end, s0)
                shift_str = f"{format_hour(s0)} – {format_hour(s1)}"
                self.res_tree.insert(
                    "",
                    "end",
                    values=(a["employee"], lab, shift_str, a["work_hours"], ",".join(map(str, a["unavailable"]))),
                )

        # Coverage summary (with red understaff lines)
        header_lines = [
            f"Coverage constraints: MIN={result['min_workers_per_hour']} (soft), MAX={result['max_workers_per_hour']} (hard)",
            f"Total understaff (sum across hours): {result['total_understaff']}",
            f"Fairness spread (max work hrs - min work hrs): {result['fairness_spread']}",
            "",
            "Coverage by hour:",
        ]
        self.coverage_text.insert(tk.END, "\n".join(header_lines) + "\n")

        for h in range(day_start, day_end):
            cov = result["coverage_by_hour"][h]
            short = result["understaff_by_hour"][h]
            line = f"  {format_hour(h)} – {format_hour(h+1)} : {cov} worker(s)"
            if short > 0:
                line += f"   UNDERSTAFF +{short}"
                # Insert with tag to make it red
                self.coverage_text.insert(tk.END, line + "\n", ("understaff",))
            else:
                self.coverage_text.insert(tk.END, line + "\n")

    # -------- Export coverage CSV --------
    def export_coverage_csv(self):
        if not self.last_result or self.last_result.get("status") != "ok":
            messagebox.showerror("Nothing to export", "Run the scheduler first, then export coverage to CSV.")
            return

        result = self.last_result
        day_start = result["day_start_hour"]
        day_end = result["day_end_hour"]

        default_name = "coverage_export.csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile=default_name,
            title="Save Coverage CSV",
        )
        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "block_start_hour_24h",
                        "block_end_hour_24h",
                        "block_start_time",
                        "block_end_time",
                        "coverage",
                        "understaff",
                        "min_workers_soft",
                        "max_workers_hard",
                    ]
                )
                for h in range(day_start, day_end):
                    cov = result["coverage_by_hour"][h]
                    short = result["understaff_by_hour"][h]
                    writer.writerow(
                        [
                            h,
                            h + 1,
                            format_hour(h),
                            format_hour(h + 1),
                            cov,
                            short,
                            result["min_workers_per_hour"],
                            result["max_workers_per_hour"],
                        ]
                    )
            messagebox.showinfo("Export complete", f"Saved coverage CSV:\n{path}")
        except Exception as e:
            messagebox.showerror("Export failed", f"Could not write CSV:\n{e}")


if __name__ == "__main__":
    app = SchedulerUI()
    app.mainloop()
