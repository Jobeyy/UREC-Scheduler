# Worker Scheduler (Tkinter + OR-Tools)

A desktop scheduling tool that assigns employees to **4–5 hour shifts** across a **12-hour work day** (configurable), while respecting **class/unavailability blocks** and enforcing hourly staffing limits.

Built with:
- **Python**
- **Google OR-Tools (CP-SAT)** for constraint solving
- **Tkinter** for the UI

---

## Features

- **Shift generation:** only **4-hour** and **5-hour** shifts (configurable)
- **Unavailability support:** employees can’t be scheduled during 1-hour “class blocks”
- **Hourly coverage limits:**
  - **Max workers/hour = hard constraint** (never exceeded)
  - **Min workers/hour = soft constraint** (allowed to fall short, shown as `UNDERSTAFF`)
- **Understaff visibility:** uncovered hours are shown in **red**
- **Shift labels:** `Opening`, `Mid`, `Closing` (based on shift start)
- **Multiple demo datasets:** select and load pre-built examples from a dropdown
- **CSV export:** export hourly coverage + understaffing to a `.csv` file
- **Split view UI:** assignments on top, coverage output on bottom (resizable)

---

## How it Works (High Level)

1. The day is modeled as **hour blocks** (e.g., 8–9, 9–10, …).
2. The app generates all possible **4h/5h shifts** that fit inside the day.
3. For each employee, shifts overlapping any **unavailable hour** are removed.
4. The solver chooses at most **one shift per employee** (can be changed).
5. Coverage is computed per hour:
   - `coverage[h] <= max_workers` (hard)
   - `coverage[h] + understaff[h] >= min_workers` (soft)
6. Objective (priority order):
   1. **Minimize total understaff**
   2. Improve **fairness** (minimize difference between max and min assigned hours)
   3. Avoid unnecessary over-coverage (keep coverage lean)

---

## Requirements

- Python **3.10+** recommended
- `ortools` installed

---

## Installation

```bash
# (optional) create and activate a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
