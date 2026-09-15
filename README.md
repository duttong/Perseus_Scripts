# PRS (Perseus) Scripts

Tooling for the **Perseus (PRS)** GC-MS system — PR1 and PR2 — that exports
chromatograph data from GCwerks and loads it into the HATS next-generation
MySQL database.

PR1 is `inst_num = 58`; PR2 is `inst_num = 238`. The loader chooses the
instrument number by analysis timestamp (PR2 begins on 2026-04-23). The PR1
analyte list is currently shared by the PRS system.

## Files

| File | Role |
|---|---|
| `pr1_export.py` | Calls `gcexport` (one process per analyte) to dump GCwerks chromatograph data to CSV files in `/hats/gc/pr1/results/`. Defines `PRS_base` (DB connection, site/standard/analyte lookups) and `PRS_GCwerks_Export`. Backward-compatible `PR1_*` aliases remain. |
| `pr1_gcwerks2db.py` | Reads the exported CSVs and upserts into `hats.analysis`, `hats.raw_data`, `hats.ancillary_data`, and `hats.flags_internal`. Also reads PFP logs from `/data/Perseus-1/logs/pfp.log/`. |
| `pr1db-gui.py` | PyQt5 GUI wrapping the above — pick analytes, pick a date range, run export + DB update, and receive live status/progress without freezing the window. |

## Key constants

- GCwerks data: `/data/Perseus-1`
- Export output: `/hats/gc/pr1/results/`
- `gcexport` binary: `/hats/gc/gcwerks-3/bin/gcexport`
- Earliest usable PR1 data: **2015-06-01** (`pr1_start_date` in `PRS_db`)
- DB connection: `db_conn.HATS_ng()` from `/ccg/src/db/db_utils/db_conn.py`

## Usage

Export GCwerks data to CSV:

```bash
./pr1_export.py             # all molecules from 2201 (Jan 2022)
./pr1_export.py 2406        # all molecules from June 2024
./pr1_export.py 2406 -m "CFC11,CFC12,N2O"
./pr1_export.py --list      # list valid molecule names
```

Load CSVs into the HATS DB (see `pr1_gcwerks2db.py --help`).

GUI front-end (requires `$DISPLAY`):

```bash
./pr1db-gui.py
```

## History

These scripts previously lived in `duttong/itxbin` under `pr1/`; that
subdirectory was untracked from itxbin in commit `3a8eaad` and now lives
here as a standalone repo. Use this repo for all PRS-specific changes.
