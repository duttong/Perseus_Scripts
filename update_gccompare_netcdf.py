#!/usr/bin/env python3
"""
Create GCCompare netCDF files from the HATS database.

Python replacement for update_hats_flask_data_ncdf.m, extended to cover every
HATS instrument family.  Output follows gccompare_netcdf_file_creation_info.txt:

    <outdir>/<PREFIX>_<SITE>_<suffix>/<lowercase compound>_air.nc
        dimension  r
        time        float64  seconds since 1970-01-01 00:00:00 UTC
        <CMPD>_C    float64  mole fraction

Instrument groups (--inst):
    PRS     PR1 + PR2 flask pairs           (prs_data_view)
            + M2 rows from PR1_M2_merged_files/ for H-1301, HFC-32, HFC-125, HFC-143a
    M_star  M1 + M3 + M4 flask pairs        (ng_data_view)
    F_ECD   FE3 + OTTO flask pairs          (ng_data_view)
    insitu  IE3, CATS-01..06, RITS-01..05   (ng_insitu_data_view / ng_insitu_mole_fractions)
            air1 + air2 inlets only (ng_port_info), hourly means stamped at HH:30
    All     all of the above (default)

For the ng_* sources, the analyte channel is chosen from hats.ng_preferred_channel
(an entry applies from its start_date until the next entry for the same
inst/parameter).

Database credentials come from ~/.my.cnf ([client] section) unless overridden.

Examples:
    ./update_gccompare_netcdf.py
    ./update_gccompare_netcdf.py --inst PRS --daterange 2014-01-01:2026-04-25
    ./update_gccompare_netcdf.py --inst insitu --daterange 20200101: --workers 6
    ./update_gccompare_netcdf.py --inst M_star --sites global_network --compounds agage
    ./update_gccompare_netcdf.py --no-rsync          # write locally, skip copy to harpe2
"""

import argparse
import datetime as dt
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import MySQLdb
import netCDF4
import numpy as np
import pandas as pd

log = logging.getLogger("gccompare")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTDIR = SCRIPT_DIR / "Other_non_IADV_files" / "AGAGE" / "netcdf"
# After writing, the contents of the output directory are copied here with rsync.
DEFAULT_RSYNC_DEST = "harpe2:/data/gccompare/noaa/"
M2_MERGED_DIR = SCRIPT_DIR / "PR1_M2_merged_files"

# --------------------------------------------------------------------------
# Instrument group definitions
# --------------------------------------------------------------------------
GROUPS = {
    # compound_insts: analyte_list instruments whose public/agage/internal_use flags define the
    # --compounds list for the group.  None = group is not filtered by --compounds; its analytes
    # come from analyte_list/ng_preferred_channel with channels picked from ng_preferred_channel.
    "PRS": dict(inst_nums=(58, 238), compound_insts=(58, 238),               # PR1, PR2
                prefix="PRS", suffix="flask"),
    "M_star": dict(inst_nums=(46, 54, 192), compound_insts=(192,),          # M1, M3, M4 (M4 list)
                   prefix="MSTAR", suffix="flask"),
    "F_ECD": dict(inst_nums=(193, 237), compound_insts=None,                # FE3, OTTO
                  prefix="FECD", suffix="flask"),
    "insitu": dict(inst_nums=(236, 239, 240, 241, 242, 243, 244,            # IE3, CATS-01..06
                              246, 247, 248, 249, 250),                     # RITS-01..05
                   compound_insts=None, prefix="INSITU", suffix="insitu"),
}
GROUP_ORDER = ["PRS", "M_star", "F_ECD", "insitu"]

# M2 data is only used for these compounds, taken from the merged PR1/M2 files
# produced by merge_PR1_M2_HATS_data.m (M2 values there are already scaled to PR1).
M2_COMPOUNDS = ("H-1301", "HFC-32", "HFC-125", "HFC-143a")

# In situ: only injections from these ng_port_info.serial_number inlets are used (air1 + air2
# are pooled into one series).  'air' is the single inlet on the old RITS systems.
INSITU_AIR_PORTS = ("air1", "air2", "air")

# In situ data are averaged into hourly bins; each mean is stamped at the hour start + this offset.
INSITU_HOUR_STAMP_OFFSET = pd.Timedelta(minutes=30)

# Preference order of analyte_list instruments for the canonical compound name
# (PR1-style hyphenated names, which match the existing GCCompare files).
NAME_SOURCE_INSTS = (58, 238, 192, 47)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def parse_date(s, end=False):
    s = s.strip()
    if not s:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, fmt)
            # end date is inclusive: use the start of the following day as an exclusive bound
            return d + dt.timedelta(days=1) if end else d
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"bad date '{s}' (use YYYYMMDD or YYYY-MM-DD)")


def parse_daterange(s):
    if s.lower() == "all":
        return (None, None)
    if ":" not in s:
        raise argparse.ArgumentTypeError("--daterange must look like start:end (either side may be empty)")
    a, b = s.split(":", 1)
    start, end = parse_date(a), parse_date(b, end=True)
    if start and end and start >= end:
        raise argparse.ArgumentTypeError("--daterange start must be before end")
    return (start, end)


def date_clause(col, daterange, params):
    """Append SQL date-range conditions for column `col`; values go into `params`."""
    start, end = daterange
    sql = ""
    if start:
        sql += f" AND {col} >= %s"
        params.append(start)
    if end:
        sql += f" AND {col} < %s"
        params.append(end)
    return sql


def normalize_name(name):
    """Map instrument-specific display names onto PR1-style names (CFC11 -> CFC-11, h1211 -> H-1211)."""
    n = name.strip()
    if n.upper() == "OCS":
        return "COS"
    m = re.fullmatch(r"[Hh]-?(\d{4})", n)
    if m:
        return f"H-{m.group(1)}"
    m = re.fullmatch(r"(HCFC|CFC|HFC|HFO|PFC)-?(\d.*)", n, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1).upper()}-{m.group(2)}"
    return n


class DB:
    """Thread-local MySQL connections built from ~/.my.cnf."""

    def __init__(self, cnf, host=None, db=None):
        self.kw = dict(read_default_file=os.path.expanduser(cnf), read_default_group="client")
        if host:
            self.kw["host"] = host
        if db:
            self.kw["db"] = db
        self._local = threading.local()

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = MySQLdb.connect(**self.kw)
            self._local.conn = c
        return c

    def query(self, sql, params=None):
        cur = self.conn().cursor()
        try:
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description]
            return pd.DataFrame.from_records(cur.fetchall(), columns=cols)
        finally:
            cur.close()


# --------------------------------------------------------------------------
# Metadata: compound names, preferred channels, sites
# --------------------------------------------------------------------------
class Meta:
    def __init__(self, db):
        al = db.query("SELECT inst_num, display_name, CAST(param_num AS UNSIGNED) AS param_num, "
                      "channel, disp_order, public, agage, internal_use FROM hats.analyte_list")
        al["channel"] = al["channel"].fillna("")
        self.analyte_list = al

        par = db.query("SELECT num, formula FROM gmd.parameter")
        self.names = {}
        for p in par.itertuples():
            self.names[int(p.num)] = normalize_name(p.formula)
        # analyte_list names: lowest priority first so preferred instruments overwrite
        prio = {inst: i for i, inst in enumerate(NAME_SOURCE_INSTS)}
        al_sorted = al.assign(_p=al["inst_num"].map(prio).fillna(99)).sort_values("_p", ascending=False)
        for r in al_sorted.itertuples():
            self.names[int(r.param_num)] = normalize_name(r.display_name)

        pc = db.query("SELECT inst_num, parameter_num, start_date, channel FROM hats.ng_preferred_channel")
        self.pref = {}
        for (inst, par_num), g in pc.groupby(["inst_num", "parameter_num"]):
            g = g.sort_values("start_date")
            starts = pd.to_datetime(g["start_date"]).values.astype("datetime64[ns]")
            self.pref[(int(inst), int(par_num))] = (starts, g["channel"].to_numpy())

        self.sites = db.query("SELECT code, global_network, ftp, agage FROM hats.sites")

    def name(self, param_num):
        return self.names.get(int(param_num), f"param{int(param_num)}")

    def flagged_params(self, inst_nums, flag):
        """param_nums with analyte_list.<flag> = 1 for any of these instruments (NULL = not flagged)."""
        al = self.analyte_list
        return sorted(al.loc[al.inst_num.isin(inst_nums) & (al[flag] == 1), "param_num"].astype(int).unique())

    def flagged_compounds(self, inst_nums, flag):
        """Canonical compound names with analyte_list.<flag> = 1 for any of these instruments."""
        return {self.name(p) for p in self.flagged_params(inst_nums, flag)}

    def site_codes(self, site_filter):
        return set(self.sites.loc[self.sites[site_filter] == 1, "code"].str.upper())

    def fallback_channel(self, inst, param, present):
        """Channel to use when ng_preferred_channel has no entry for (inst, param)."""
        counts = pd.Series(present).value_counts()
        if len(counts) == 1:
            return counts.index[0]
        al = self.analyte_list
        cand = al[(al.inst_num == inst) & (al.param_num == param) & al.channel.isin(counts.index)]
        if len(cand):
            ch, how = cand.sort_values("disp_order").iloc[0]["channel"], "analyte_list"
        else:
            ch, how = counts.index[0], "most frequent"
        # a handful of stray rows on another channel is routine (old M1/M3 data); only warn otherwise
        level = logging.WARNING if counts.get(ch, 0) < 0.99 * counts.sum() else logging.DEBUG
        log.log(level, "inst %s %s: no ng_preferred_channel entry; channels %s; using %r (%s)",
                inst, self.name(param), dict(counts), ch, how)
        return ch

    def select_channels(self, df, time_col):
        """Keep only rows measured on the preferred channel for their inst/parameter/date."""
        if df.empty:
            return df
        keep = np.zeros(len(df), dtype=bool)
        times = df[time_col].to_numpy(dtype="datetime64[ns]")
        chans = df["channel"].fillna("").to_numpy()
        for (inst, par), idx in df.groupby(["inst_num", "parameter_num"]).indices.items():
            key = (int(inst), int(par))
            if key in self.pref:
                starts, pchans = self.pref[key]
                # entry in effect at each sample time; times before the first entry use the first one
                pos = np.searchsorted(starts, times[idx], side="right") - 1
                pos[pos < 0] = 0
                keep[idx] = chans[idx] == pchans[pos]
            else:
                ch = self.fallback_channel(key[0], key[1], chans[idx])
                if ch is not None:
                    keep[idx] = chans[idx] == ch
        return df[keep]

    def preferred_channel_candidates(self, inst, param):
        """All channels that could be selected for (inst, param); None = no restriction."""
        key = (inst, param)
        if key in self.pref:
            return sorted(set(self.pref[key][1]))
        return None


# --------------------------------------------------------------------------
# Data loaders: each returns DataFrame[site, time, compound, value]
# --------------------------------------------------------------------------
def load_prs(db, meta, daterange, site_filter, compound_flag):
    insts = GROUPS["PRS"]["inst_nums"]
    params = meta.flagged_params(GROUPS["PRS"]["compound_insts"], compound_flag)
    if not params:
        return pd.DataFrame(columns=["site", "time", "compound", "value"])

    p = list(insts) + params
    sql = (
        "SELECT dv.parameter_num, MIN(dv.site) AS site, MIN(dv.sample_datetime) AS time, "
        "AVG(dv.value) AS value "
        "FROM hats.prs_data_view dv "
        f"WHERE dv.inst_num IN ({','.join(['%s'] * len(insts))}) "
        f"AND dv.parameter_num IN ({','.join(['%s'] * len(params))}) "
        "AND dv.sample_type = 'HATS' "
        "AND dv.rejected_other_than_preliminary != 1 "
        "AND dv.background = 1 "
        "AND dv.value IS NOT NULL "
    )
    sql += date_clause("dv.sample_datetime", daterange, p)
    sql += " GROUP BY dv.PairID, dv.parameter_num"
    t0 = time.time()
    df = db.query(sql, p)
    log.info("PRS: %d pair means from prs_data_view (%.1fs)", len(df), time.time() - t0)
    df["compound"] = df["parameter_num"].map(meta.name)
    df = df[["site", "time", "compound", "value"]]

    m2 = load_m2_merged(daterange)
    if len(m2):
        log.info("PRS: adding %d M2 rows from %s", len(m2), M2_MERGED_DIR.name)
        df = pd.concat([df, m2], ignore_index=True)
    return filter_sites(df, meta, site_filter)


def load_m2_merged(daterange):
    """M2 rows from PR1_M2_merged_files/<cmpd>_M2_PR1_MS_flask.txt (merge_PR1_M2_HATS_data.m output)."""
    frames = []
    for cmpd in M2_COMPOUNDS:
        f = M2_MERGED_DIR / f"{cmpd}_M2_PR1_MS_flask.txt"
        if not f.exists():
            log.warning("M2 merged file missing: %s", f)
            continue
        rows = []
        with open(f, errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 10 or line.startswith("#") or parts[-1] != "M2":
                    continue
                site, _dec, ymd, hhmm, _wd, _ws, mf = parts[:7]
                try:
                    t = dt.datetime.strptime(ymd + hhmm, "%Y%m%d%H:%M")
                    rows.append((site.upper(), t, float(mf)))
                except ValueError:
                    continue
        d = pd.DataFrame(rows, columns=["site", "time", "value"])
        d["compound"] = cmpd
        frames.append(d)
    if not frames:
        return pd.DataFrame(columns=["site", "time", "compound", "value"])
    df = pd.concat(frames, ignore_index=True)
    start, end = daterange
    if start:
        df = df[df.time >= start]
    if end:
        df = df[df.time < end]
    return df


def load_ng_flask(db, meta, group, daterange, site_filter):
    insts = GROUPS[group]["inst_nums"]
    p = list(insts)
    sql = (
        "SELECT inst_num, parameter_num, channel, site, sample_datetime AS time, PairID, value "
        "FROM hats.ng_data_view "
        f"WHERE inst_num IN ({','.join(['%s'] * len(insts))}) "
        "AND sample_type = 'HatsFlask' "
        "AND rejected = 0 "
        "AND background = 1 "
        "AND value IS NOT NULL "
    )
    sql += date_clause("sample_datetime", daterange, p)
    t0 = time.time()
    df = db.query(sql, p)
    log.info("%s: %d aliquot rows from ng_data_view (%.1fs)", group, len(df), time.time() - t0)
    if df.empty:
        return pd.DataFrame(columns=["site", "time", "compound", "value"])
    df["time"] = pd.to_datetime(df["time"])
    df["value"] = df["value"].astype(float)
    df = meta.select_channels(df, "time")
    df["compound"] = df["parameter_num"].map(meta.name)
    # flask-pair mean, one value per sampling event and compound
    df = (df.groupby(["PairID", "compound"], sort=False)
            .agg(site=("site", "first"), time=("time", "min"), value=("value", "mean"))
            .reset_index())
    log.info("%s: %d pair means after channel selection", group, len(df))
    return filter_sites(df[["site", "time", "compound", "value"]], meta, site_filter)


def load_insitu(db, meta, daterange, site_filter, workers):
    insts = GROUPS["insitu"]["inst_nums"]
    al = meta.analyte_list

    # Only ambient-air injections: ports whose ng_port_info serial_number is an air inlet.
    # Port numbers are per instrument (air1 is port 4 on CATS, 3 on IE3), so match inst + port.
    ports = db.query(
        f"SELECT DISTINCT inst_num, port_num FROM hats.ng_port_info "
        f"WHERE serial_number IN ({','.join(['%s'] * len(INSITU_AIR_PORTS))})", list(INSITU_AIR_PORTS))
    air_ports = ports.groupby("inst_num")["port_num"].apply(lambda s: sorted(int(x) for x in s)).to_dict()

    tasks = []
    for inst in insts:
        if inst not in air_ports:
            log.warning("insitu inst %s: no %s ports in ng_port_info; skipped",
                        inst, "/".join(INSITU_AIR_PORTS))
            continue
        params = sorted(al.loc[al.inst_num == inst, "param_num"].astype(int).unique())
        pref_params = {k[1] for k in meta.pref if k[0] == inst}
        for par in sorted(set(params) | pref_params):
            tasks.append((inst, par))

    def fetch(inst, par):
        aps = air_ports[inst]
        p = [inst, par] + aps
        sql = ("SELECT inst_num, parameter_num, channel, site, analysis_time AS time, mole_fraction AS value "
               "FROM hats.ng_insitu_data_view "
               "WHERE inst_num = %s AND parameter_num = %s "
               f"AND port IN ({','.join(['%s'] * len(aps))}) "
               "AND rejected = 0 AND background = 1 AND mole_fraction IS NOT NULL ")
        chans = meta.preferred_channel_candidates(inst, par)
        if chans:
            sql += f"AND channel IN ({','.join(['%s'] * len(chans))}) "
            p += chans
        sql += date_clause("analysis_time", daterange, p)
        df = db.query(sql, p)
        if df.empty:
            return df
        df["time"] = pd.to_datetime(df["time"])
        df["value"] = df["value"].astype(float)
        df = meta.select_channels(df, "time")
        df["compound"] = meta.name(par)
        return df[["site", "time", "compound", "value"]]

    t0 = time.time()
    frames = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, i, p): (i, p) for i, p in tasks}
        for f in as_completed(futs):
            inst, par = futs[f]
            d = f.result()
            log.debug("insitu inst %s %s: %d rows", inst, meta.name(par), len(d))
            if len(d):
                frames.append(d)
    log.info("insitu: %d (inst, parameter) queries in %.1fs", len(tasks), time.time() - t0)
    if not frames:
        return pd.DataFrame(columns=["site", "time", "compound", "value"])
    df = filter_sites(pd.concat(frames, ignore_index=True), meta, site_filter)

    # Hourly means per site/compound (all in situ instruments at a site pooled),
    # time-stamped at the middle of the hour.
    n_raw = len(df)
    df["time"] = df["time"].dt.floor("h")
    df = df.groupby(["site", "compound", "time"], sort=False, as_index=False)["value"].mean()
    df["time"] += INSITU_HOUR_STAMP_OFFSET
    log.info("insitu: %d measurements -> %d hourly means", n_raw, len(df))
    return df


def filter_sites(df, meta, site_filter):
    df = df.dropna(subset=["site", "time", "value"]).copy()
    df["site"] = df["site"].str.upper()
    df = df[df["site"].isin(meta.site_codes(site_filter))]
    return df


# --------------------------------------------------------------------------
# netCDF output
# --------------------------------------------------------------------------
def write_nc(path, compound, times, values):
    tmp = path.with_suffix(".nc.tmp")
    with netCDF4.Dataset(tmp, "w", format="NETCDF4") as nc:
        nc.createDimension("r", len(times))
        vt = nc.createVariable("time", "f8", ("r",))
        vt.units = "seconds since 1970-01-01 00:00:00 UTC"
        vt[:] = times
        vc = nc.createVariable(f"{compound}_C", "f8", ("r",))
        vc[:] = values
    os.replace(tmp, path)


def write_group(df, group, outdir, dry_run):
    cfg = GROUPS[group]
    if df.empty:
        log.warning("%s: no data, nothing written", group)
        return 0
    df = df.sort_values(["site", "compound", "time"])
    # POSIX seconds; sample times in the db are UTC
    posix = (df["time"].values.astype("datetime64[ns]").astype("int64") / 1e9)
    df = df.assign(posix=posix)
    n = 0
    per_site = defaultdict(int)
    for (site, cmpd), g in df.groupby(["site", "compound"], sort=False):
        d = outdir / f"{cfg['prefix']}_{site}_{cfg['suffix']}"
        f = d / f"{cmpd.lower()}_air.nc"
        if not dry_run:
            d.mkdir(parents=True, exist_ok=True)
            write_nc(f, cmpd, g["posix"].to_numpy(), g["value"].to_numpy(dtype=float))
        n += 1
        per_site[site] += 1
        log.debug("%s %s: %d points -> %s", group, cmpd, len(g), f)
    log.info("%s: %s %d files across %d sites (%s)", group,
             "would write" if dry_run else "wrote", n, len(per_site),
             ", ".join(f"{s}:{c}" for s, c in sorted(per_site.items())))
    return n


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inst", default="All", type=str.lower,
                    choices=["all", "prs", "f_ecd", "m_star", "insitu"],
                    help="instrument group to process (default: All)")
    ap.add_argument("--daterange", default="all", type=parse_daterange,
                    help="start:end in YYYYMMDD or YYYY-MM-DD, end inclusive; either side may be "
                         "empty (default: all)")
    ap.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR,
                    help=f"output root (default: {DEFAULT_OUTDIR})")
    ap.add_argument("--sites", default="global_network", type=str.lower,
                    choices=["global_network", "ftp", "agage"],
                    help="only write sites flagged 1 in this hats.sites column (default: global_network)")
    ap.add_argument("--compounds", default="agage", type=str.lower,
                    choices=["public", "agage", "internal_use"],
                    help="only write compounds flagged 1 in this hats.analyte_list column (default: agage). "
                         "PRS uses the PR1/PR2 flags, M_star uses the M4 flags; F_ECD and insitu "
                         "are not filtered (channels from ng_preferred_channel)")
    ap.add_argument("--workers", type=int, default=4, help="parallel db connections for in situ queries")
    ap.add_argument("--cnf", default="~/.my.cnf", help="MySQL option file (default: ~/.my.cnf)")
    ap.add_argument("--db-host", help="override host from option file")
    ap.add_argument("--db-name", default="hats", help="database (default: hats)")
    ap.add_argument("--dry-run", action="store_true", help="query and report, but write nothing (no rsync)")
    ap.add_argument("--rsync-dest", default=DEFAULT_RSYNC_DEST,
                    help=f"rsync the contents of --outdir here after writing (default: {DEFAULT_RSYNC_DEST})")
    ap.add_argument("--no-rsync", action="store_true", help="write files locally only")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    lookup = {g.lower(): g for g in GROUP_ORDER}
    groups = GROUP_ORDER if args.inst == "all" else [lookup[args.inst]]

    t0 = time.time()
    db = DB(args.cnf, host=args.db_host, db=args.db_name)
    meta = Meta(db)
    s, e = args.daterange
    log.info("groups=%s sites=%s compounds=%s daterange=%s to %s outdir=%s", ",".join(groups),
             args.sites, args.compounds,
             s.date() if s else "start", (e - dt.timedelta(days=1)).date() if e else "end", args.outdir)

    total = 0
    for g in groups:
        compounds = None
        cinsts = GROUPS[g]["compound_insts"]
        if cinsts is not None:
            compounds = meta.flagged_compounds(cinsts, args.compounds)
            if not compounds:
                log.warning("%s: no compounds with %s=1 in analyte_list for inst_num %s; skipping",
                            g, args.compounds, ",".join(map(str, cinsts)))
                continue
            log.info("%s: %d %s compounds: %s", g, len(compounds), args.compounds,
                     ", ".join(sorted(compounds)))
        if g == "PRS":
            df = load_prs(db, meta, args.daterange, args.sites, args.compounds)
        elif g == "insitu":
            df = load_insitu(db, meta, args.daterange, args.sites, args.workers)
        else:
            df = load_ng_flask(db, meta, g, args.daterange, args.sites)
        if compounds is not None:
            df = df[df["compound"].isin(compounds)]
        total += write_group(df, g, args.outdir, args.dry_run)

    log.info("done: %d files in %.1f min", total, (time.time() - t0) / 60)

    if args.dry_run or args.no_rsync:
        return 0
    return rsync_outdir(args.outdir, args.rsync_dest)


def rsync_outdir(outdir, dest):
    """Copy the contents of outdir (e.g. PRS_CGO_flask/...) into dest. Never deletes remote files."""
    cmd = ["rsync", "-rlt", "--stats", "--exclude=*.tmp",
           "-e", "ssh -o BatchMode=yes -o ConnectTimeout=20",
           f"{str(outdir).rstrip('/')}/", dest]
    log.info("rsync: %s/ -> %s", outdir, dest)
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error("rsync failed (exit %d); files are still in %s\n%s", r.returncode, outdir, r.stderr.strip())
        return 1
    summary = [l for l in r.stdout.splitlines()
               if l.startswith(("Number of regular files transferred", "Total transferred file size"))]
    log.info("rsync done in %.1fs: %s", time.time() - t0, "; ".join(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
