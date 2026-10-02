#!/usr/bin/env python
""" Replace T1 = nan in the Perseus-1 (PR1) run.log files with a mean T1 taken
    from the strip-chart file of the same run.

    For every file in /data/Perseus-1/YY/run.log/ with a "T1  nan" line:
      1. /data/Perseus-1/YY/runfile.log/<file> gives the time (seconds into the
         run) of the "samplelog T1 T1" command that should have recorded T1.
      2. /data/Perseus-1/YY/strip-chart/<file> is exported to text with the
         GCwerks stripchart-export program and the T1 readings within
         +/- 5 seconds of that time are averaged.
      3. The unmodified run.log file is copied to
         /data/Perseus-1/YY/run.log/original/ (unless --no-backup is used).
      4. The nan in the run.log file is replaced with the mean.

    All year directories are done unless one is selected with -y.

    Only the T1 line is changed. Runs that are missing a runfile.log or
    strip-chart file, or have too few T1 readings in the window, are skipped
    and listed at the end.

    Use -n to see what would be changed without writing anything.
"""

import re
import shutil
import argparse
import subprocess
import tempfile
from pathlib import Path

import numpy as np

DEFAULT_ROOT = Path('/data/Perseus-1')
DEFAULT_GCWERKS_BIN = Path('/data/gcwerks-3/bin')


class RunlogT1Fix:

    FIELD = 'T1'
    BACKUP_DIR = 'original'     # directory in run.log for the unmodified files
    # "T1  nan" line in a run.log file
    NAN_LINE = re.compile(r'^(T1[ \t]+)nan[ \t]*$', re.IGNORECASE | re.MULTILINE)
    # "416 samplelog T1 T1" line in a runfile.log file
    SAMPLELOG = re.compile(r'^\s*([\d.]+)\s+samplelog\s+T1\s+T1\s*$', re.MULTILINE)

    def __init__(self, year, root=DEFAULT_ROOT, gcwerks_bin=DEFAULT_GCWERKS_BIN,
                 halfwidth=5.0, min_points=5, backup=True, verbose=False):
        self.root = Path(root)
        self.year = f'{int(year) % 100:02d}'
        self.runlog_path = self.root / self.year / 'run.log'
        self.runfile_path = self.root / self.year / 'runfile.log'
        self.stripchart_path = self.root / self.year / 'strip-chart'
        self.backup_path = self.runlog_path / self.BACKUP_DIR
        self.gcwerks_bin = Path(gcwerks_bin)
        self.halfwidth = halfwidth              # seconds either side of the samplelog time
        self.min_points = min_points            # fewest T1 readings accepted for a mean
        self.backup = backup                    # copy the unmodified files to backup_path
        self.verbose = verbose
        self.skipped = []                       # (file, reason)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def years(root=DEFAULT_ROOT):
        """ Two digit year directories under root that have a run.log directory. """
        return sorted(d.name for d in Path(root).iterdir()
                      if re.fullmatch(r'\d\d', d.name) and (d / 'run.log').is_dir())

    def skip(self, file, reason):
        self.skipped.append((file, reason))

    def nan_files(self):
        """ run.log files with a T1 = nan line, sorted by name. """
        files = []
        for f in sorted(self.runlog_path.iterdir()):
            if f.is_file() and self.NAN_LINE.search(f.read_text(errors='replace')):
                files.append(f)
        return files

    def samplelog_time(self, file):
        """ Seconds into the run of the "samplelog T1 T1" command, or None. """
        runfile = self.runfile_path / file
        if not runfile.exists():
            self.skip(file, 'no runfile.log file')
            return None
        times = self.SAMPLELOG.findall(runfile.read_text(errors='replace'))
        if len(times) != 1:
            self.skip(file, f'{len(times)} "samplelog T1 T1" lines in runfile.log')
            return None
        return float(times[0])

    def stripchart_T1(self, file):
        """ T1 trace from the strip-chart file as arrays of (seconds, T1), or None.

            stripchart-export writes <file>.txt into the current directory with
            a "time, <field>" header ahead of each field's readings.
        """
        if not (self.stripchart_path / file).exists():
            self.skip(file, 'no strip-chart file')
            return None
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [str(self.gcwerks_bin / 'stripchart-export'), str(self.root), '-file', file, self.FIELD]
            proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, errors='replace')
            out = Path(tmp) / f'{file}.txt'
            if proc.returncode != 0 or not out.exists():
                self.skip(file, f'stripchart-export failed (exit {proc.returncode})')
                return None
            lines = out.read_text(errors='replace').splitlines()

        secs, vals, field = [], [], None
        for line in lines:
            parts = [p.strip() for p in line.split(',')]
            if len(parts) != 2:
                continue
            if parts[0] == 'time':
                field = parts[1]
            elif field == self.FIELD:
                try:
                    secs.append(float(parts[0]))
                    vals.append(float(parts[1]))
                except ValueError:
                    continue
        if not secs:
            self.skip(file, 'no T1 readings in strip-chart file')
            return None
        return np.array(secs), np.array(vals)

    def mean_T1(self, file):
        """ (samplelog time, mean, std, n) of T1 around the samplelog time, or None. """
        t0 = self.samplelog_time(file)
        if t0 is None:
            return None
        trace = self.stripchart_T1(file)
        if trace is None:
            return None
        secs, vals = trace
        window = vals[(secs >= t0 - self.halfwidth) & (secs <= t0 + self.halfwidth)]
        window = window[np.isfinite(window)]
        if len(window) < self.min_points:
            self.skip(file, f'only {len(window)} T1 readings within {self.halfwidth:g} s of {t0:g} s')
            return None
        return t0, window.mean(), window.std(ddof=1), len(window)

    # ------------------------------------------------------------------ update
    def save_original(self, runlog):
        """ Copy an unmodified run.log file to the backup directory. A copy that
            is already there is left alone. """
        self.backup_path.mkdir(exist_ok=True)
        dest = self.backup_path / runlog.name
        if not dest.exists():
            shutil.copy2(runlog, dest)

    def fix_file(self, runlog, mean, dry_run=False):
        """ Replace the nan on the T1 line of a run.log file with mean (2 decimals,
            the same as the T1 values GCwerks writes). """
        text = runlog.read_text()
        new_text, n = self.NAN_LINE.subn(lambda m: f'{m.group(1)}{mean:.2f}', text)
        if n != 1:
            self.skip(runlog.name, f'{n} T1 nan lines in run.log')
            return False
        if not dry_run:
            if self.backup:
                self.save_original(runlog)
            runlog.write_text(new_text)
        return True

    def run(self, dry_run=False):
        """ Fix every run.log file with T1 = nan. Returns the number of files fixed. """
        files = self.nan_files()
        if files or self.verbose:
            print(f'{len(files)} run.log files with T1 = nan in {self.runlog_path}')
        fixed = 0
        for runlog in files:
            res = self.mean_T1(runlog.name)
            if res is None:
                continue
            t0, mean, std, n = res
            if not self.fix_file(runlog, mean, dry_run=dry_run):
                continue
            fixed += 1
            if self.verbose or dry_run:
                print(f'{runlog.name:50s} T1 = {mean:8.2f}  (sd {std:.2f}, n {n}, at {t0:g} s)')
        return fixed

    def report(self):
        if self.skipped:
            print(f'\nSkipped {len(self.skipped)} files in {self.runlog_path}:')
            for file, reason in self.skipped:
                print(f'  {file}: {reason}')


def main():
    parser = argparse.ArgumentParser(
        description='Replace T1 = nan in the PR1 run.log files with a mean T1 from the strip-chart files.')
    parser.add_argument('-y', '--year',
                        help=f'Two digit year directory under {DEFAULT_ROOT} (default: all years)')
    parser.add_argument('-w', '--halfwidth', type=float, default=5.0,
                        help='Seconds either side of the samplelog time to average (default: 5)')
    parser.add_argument('-m', '--min-points', type=int, default=5,
                        help='Fewest T1 readings in the window needed to use the mean (default: 5)')
    parser.add_argument('--gcwerks-bin', default=DEFAULT_GCWERKS_BIN,
                        help=f'GCwerks bin directory (default: {DEFAULT_GCWERKS_BIN})')
    parser.add_argument('--no-backup', action='store_true',
                        help=f'Do not copy the unmodified files to run.log/{RunlogT1Fix.BACKUP_DIR}/')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='Show the T1 means but do not change the run.log files')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Print each file as it is updated')
    args = parser.parse_args()

    years = [args.year] if args.year else RunlogT1Fix.years()
    fixes = [RunlogT1Fix(year, gcwerks_bin=args.gcwerks_bin, halfwidth=args.halfwidth,
                         min_points=args.min_points, backup=not args.no_backup,
                         verbose=args.verbose) for year in years]
    fixed = sum(fix.run(dry_run=args.dry_run) for fix in fixes)
    for fix in fixes:
        fix.report()

    if args.dry_run:
        print(f'\nDry run, nothing written. {fixed} files would be updated.')
    else:
        print(f'\nUpdated {fixed} run.log files.')


if __name__ == '__main__':
    main()
