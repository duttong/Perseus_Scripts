#!/usr/bin/env python
""" Load the Perseus-1 (PR1) tank pressure logs into a temporary table that
    matches hats.ng_cylinder_pressures.

    Sources:
      /data/Perseus-1/logs/tank-press.log/YYYY  N2, He, LN2, tertiary, secondary readings
      /data/Perseus-1/logs/ports.log/YYMM       which std tank was on the instrument (Type = std)
      hats.standards                            level (tertiary/secondary/primary) of a std tank
      hats.ng_cylinder_types                    cylinder_type_num lookup

    Lines starting with '#' in the tank pressure log are LN2 fills or tank
    changes and are kept. N2 and He serial numbers are taken from the
    "New N2/He tank ... s/n XXXX" comments and carried forward until the next
    tank change.
"""

import re
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from hats_db import HATSdb


class TankPressureLog:

    # log column -> (ng_cylinder_types.abbr, cylinder label)
    COLUMNS = {
        'n2_psig':        ('nitrogen', 'N2'),
        'he_psig':        ('carrier',  'He'),
        'ln2_lb':         ('LN2',      'LN2(lbs)'),
        'tertiary_psig':  ('cal',      'tertiary'),
        'secondary_psig': ('cal',      'secondary'),
    }
    # order of the value fields in the log files (2015 on); aimx_torr is ignored
    LOG_FIELDS = ['n2_psig', 'he_psig', 'ln2_lb', 'aimx_torr', 'tertiary_psig', 'secondary_psig']

    DEFAULT_TIME = '1600'       # used when the time part of a timestamp is unreadable
    INST_CHANGE = datetime(2026, 4, 23, 19, 10)
    INST_BEFORE, INST_AFTER = 58, 238
    SYSTEM_NUM = 15
    SITE_NUM = 199

    def __init__(self, start_date=None, start_year=2015, verbose=False):
        self.hdb = HATSdb()
        self.db = self.hdb.db                   # db_conn object (for doMultiInsert)
        self.tank_log_path = Path('/data/Perseus-1/logs/tank-press.log/')
        self.ports_log_path = Path('/data/Perseus-1/logs/ports.log/')
        self.start_year = start_year            # first tank-press.log file read
        # only readings on or after start_date go to the database (default: last 30 days)
        self.start_date = start_date or datetime.now() - timedelta(days=30)
        self.verbose = verbose
        self.tmp_table = 't_cyl_press'
        self.skipped = []                       # (file, line, reason)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def parse_timestamp(date, time):
        """ Return a datetime from yymmdd and HHMM strings. An unreadable time
            uses DEFAULT_TIME; an unreadable date returns None.
        """
        try:
            day = datetime.strptime(date, '%y%m%d')
        except ValueError:
            return None
        for t in (time, TankPressureLog.DEFAULT_TIME):
            try:
                hm = datetime.strptime(t, '%H%M')
                return day.replace(hour=hm.hour, minute=hm.minute)
            except (ValueError, TypeError):
                continue

    @staticmethod
    def to_float(val):
        """ Numbers only; '-', 'E', ranges like '<100,>60', etc. become None. """
        try:
            return float(val)
        except ValueError:
            return None

    @staticmethod
    def norm_tank(name):
        """ Uppercase, drop punctuation and leading zeros of the number (ALM-67726 == ALM067726). """
        name = re.sub(r'[^A-Z0-9]', '', str(name).upper())
        return re.sub(r'(?<=[A-Z])0+(?=\d)', '', name)

    def skip(self, file, line, reason):
        self.skipped.append((file, line.rstrip(), reason))

    # ------------------------------------------------------- tank pressure log
    def parse_tank_line(self, file, line):
        """ Parse one line of the tank pressure log. Returns a dict or None. """
        body = line.rstrip('\n')
        event = body.lstrip().startswith('#')
        if event:
            body = body.replace('#', ' ', 1)    # blank it out to keep the column alignment

        # yymmdd, optional . or - separator, then whatever is attached as the time
        m = re.match(r'\s*(\d{6})[.\-]?(\S*)\s*(.*)$', body)
        if not m:
            return None                         # header, blank, or free text
        date, tpart, rest = m.groups()

        # time is 4 digits; anything stuck on after it (e.g. '1537-') is the first value
        tm = re.match(r'(\d{4})(.*)$', tpart)
        time, extra = (tm.group(1), tm.group(2)) if tm else (tpart, '')
        dt = self.parse_timestamp(date, time)
        if dt is None or dt.year != int(file):     # e.g. 350325 typed in the 2025 file
            self.skip(file, line, 'bad date')
            return None

        values, _, comment = rest.partition('#')
        tokens = ([extra] if extra else []) + values.split()
        tokens = [t for t in tokens if not re.fullmatch(r'[`\\]+', t)]   # stray keystrokes
        n = len(self.LOG_FIELDS)
        if len(tokens) >= n:
            fields = tokens[:n]
            # a few lines have a comment with no '#' after the values
            comment = ' '.join(tokens[n:] + [comment.strip()])
        else:
            # someone left a column empty (usually secondary); place values by column position
            fields = self.place_by_column(body[:m.start(3)], values)
            if fields is None:
                self.skip(file, line, f'{len(tokens)} fields')
                return None

        row = {'dt': dt, 'event': event, 'comment': comment.strip() or None, 'file': file}
        row.update({k: self.to_float(v) for k, v in zip(self.LOG_FIELDS, fields)})
        return row

    def place_by_column(self, prefix, values, width=16):
        """ Values are aligned in columns 16 characters apart (two 8-space tabs).
            Return the 6 fields with '-' for any empty column, or None if the
            positions don't make sense.
        """
        start = len(prefix.expandtabs(8))
        text = (prefix + values).expandtabs(8)
        toks = [(t.start(), t.group()) for t in re.finditer(r'\S+', text[start:])
                if not re.fullmatch(r'[`\\]+', t.group())]
        if not toks:
            return None
        idx = [round((p - toks[0][0]) / width) for p, _ in toks]
        if idx != sorted(set(idx)) or idx[-1] >= len(self.LOG_FIELDS):
            return None
        fields = ['-'] * len(self.LOG_FIELDS)
        for i, (_, tok) in zip(idx, toks):
            fields[i] = tok
        return fields

    def load_tank_logs(self):
        rows = []
        files = sorted(f for f in self.tank_log_path.iterdir()
                       if re.fullmatch(r'\d{4}', f.name) and int(f.name) >= self.start_year)
        for f in files:
            with open(f, errors='replace') as fh:
                for line in fh:
                    row = self.parse_tank_line(f.name, line)
                    if row:
                        rows.append(row)
        df = pd.DataFrame(rows).drop(columns='aimx_torr')
        # stable sort so rows sharing a timestamp stay in file order
        return df.sort_values('dt', kind='stable').reset_index(drop=True)

    # --------------------------------------------------------- N2 / He serials
    @staticmethod
    def tank_changes(comment):
        """ Find N2/He tank changes in a comment. Returns {'N2'|'He': serial or None}.
            A comment can report both gases, e.g. 'New N2 (Matheson #001), New He (Matheson #002)'.
        """
        changes = {}
        if not comment:
            return changes
        comment = re.sub(r'\b(N2|He)\s+(replaced)\b', r'\2 \1', comment)   # 'He replaced (#...)'
        for seg in re.split(r'(?i)(?=\b(?:new|replaced)\b)', comment):
            if not re.match(r'(?i)(new|replaced)\b', seg):
                continue
            # the gas has to be named before the first punctuation ('new set on scale. He ...' is not a change)
            head = re.split(r'[.,;(]', seg)[0]
            if re.search(r'\bN2\b|(?i:\bnitrogen\b)', head):
                gas = 'N2'
            elif re.search(r'\b(?:He|H3)\b|(?i:\bhelium\b)', head):
                gas = 'He'
            else:
                continue                        # new tertiary, new file, etc.

            # drop lot numbers so they are not mistaken for a serial
            s = re.sub(r'(?i)\blot\s*#?\s*:?\s*\w+(?:\s+\d+\b)*', '', seg)
            serial = None
            m = re.search(r'(?i)\b(?:s/n|sn|serial)\b\s*[:#]?\s*([\w-]+)', s)
            if m:
                if re.search(r'\d', m.group(1)):
                    serial = m.group(1)         # 's/n no sticker' stays None
            else:
                m = (re.search(r'(?i)(?:#|barcode)\s*:?\s*(\d{5,}\w*)', s)
                     or re.search(r'\b(\d{6,}\w*)\b', s))
                if m:
                    serial = m.group(1)
            changes[gas] = serial
        return changes

    def add_gas_serials(self, df):
        """ Carry each N2/He serial forward until the next tank change. """
        current = {'N2': None, 'He': None}
        n2, he = [], []
        self.gas_changes = []
        for dt, comment in zip(df.dt, df.comment):
            for gas, serial in self.tank_changes(comment).items():
                current[gas] = serial
                self.gas_changes.append((dt, gas, serial, comment))
            n2.append(current['N2'])
            he.append(current['He'])
        return df.assign(n2_serial=n2, he_serial=he)

    # ------------------------------------------------ tertiary / secondary serials
    def load_standards(self):
        """ Lookup of normalized std_ID and serial_number -> (serial_number, level).
            std_ID is checked first (ports.log often uses it, e.g. CC456895b).
        """
        st = self.hdb.to_df("SELECT std_ID, serial_number, level FROM hats.standards")
        by_id = {self.norm_tank(r.std_ID): (r.serial_number, r.level) for r in st.itertuples()}
        by_sn = {self.norm_tank(r.serial_number): (r.serial_number, r.level)
                 for r in st.itertuples() if r.serial_number}
        return by_id, by_sn

    def load_ports_std(self):
        """ Std tank installs from ports.log, resolved to serial_number and level. """
        by_id, by_sn = self.load_standards()
        rows, unmatched = [], set()
        files = sorted(f for f in self.ports_log_path.iterdir() if re.fullmatch(r'\d{4}', f.name))
        for f in files:
            with open(f, errors='replace') as fh:
                for line in fh:
                    if line.lstrip().startswith('#'):
                        continue
                    body = line.partition('#')[0]
                    # 'yymmdd.HHMM' or older 'yymmdd HHMM', then port and tank
                    m = re.match(r'\s*(\d{6})(?:\.(\S*)|\s+(\d{4}))?\s+(\d+)\s+(\S+)\s*(.*)$', body)
                    if not m:
                        continue
                    date, t1, t2, port, tank, rest = m.groups()
                    if 'std' not in (t.lower() for t in rest.split()):
                        continue
                    dt = self.parse_timestamp(date, t1 or t2)
                    if dt is None or dt.year != 2000 + int(f.name[:2]):
                        continue
                    key = self.norm_tank(tank)
                    match = by_id.get(key) or by_sn.get(key)
                    if not match:
                        unmatched.add(tank)
                        continue
                    serial, level = match
                    if level in ('tertiary', 'secondary'):
                        rows.append({'dt': dt, 'level': level, 'serial': serial, 'port': port})
        self.unmatched_stds = sorted(unmatched)
        return pd.DataFrame(rows).sort_values('dt', kind='stable').reset_index(drop=True)

    def add_std_serials(self, df):
        """ Serial of the most recent tertiary/secondary std installed at or before each reading. """
        stds = self.load_ports_std()
        self.std_changes = stds
        for level in ('tertiary', 'secondary'):
            s = (stds.loc[stds.level == level, ['dt', 'serial']]
                 .drop_duplicates('dt', keep='last')
                 .rename(columns={'serial': f'{level}_serial'}))
            df = pd.merge_asof(df, s, on='dt', direction='backward')
        return df

    # ------------------------------------------------------------ build rows
    def cylinder_type_nums(self):
        types = self.hdb.to_df("SELECT num, abbr FROM hats.ng_cylinder_types")
        return dict(zip(types.abbr, types.num))

    def build_rows(self, df):
        """ One row per non-null reading, in ng_cylinder_pressures layout. """
        type_nums = self.cylinder_type_nums()
        serial_col = {'n2_psig': 'n2_serial', 'he_psig': 'he_serial',
                      'tertiary_psig': 'tertiary_serial', 'secondary_psig': 'secondary_serial'}
        rows = []
        for r in df.itertuples():
            inst_num = self.INST_BEFORE if r.dt < self.INST_CHANGE else self.INST_AFTER
            for col, (abbr, cylinder) in self.COLUMNS.items():
                val = getattr(r, col)
                if val is None or pd.isnull(val):
                    continue
                serial = getattr(r, serial_col[col]) if col in serial_col else None
                rows.append({
                    'inst_num': inst_num,
                    'system_num': self.SYSTEM_NUM,
                    'site_num': self.SITE_NUM,
                    'cylinder_type_num': type_nums[abbr],
                    'cylinder': cylinder,
                    'serial_number': None if pd.isnull(serial) else serial,
                    'reading_datetime': r.dt,
                    'pressure': val,
                    'rejected': 0,
                    'comment': r.comment[:200] if r.comment else None,
                    'event': r.event,
                })
        out = pd.DataFrame(rows)

        # The table is unique on (inst_num, cylinder, reading_datetime). A fill is often logged
        # with the same time as the reading before it, so move the later line(s) forward a minute.
        key = ['inst_num', 'cylinder', 'reading_datetime']
        out['orig_datetime'] = out.reading_datetime
        while out.duplicated(key).any():
            dup = out.duplicated(key)
            out.loc[dup, 'reading_datetime'] += pd.Timedelta(minutes=1)
        self.shifted = out[out.reading_datetime != out.orig_datetime]
        return out.drop(columns='orig_datetime')

    def load(self):
        """ All logs are parsed so N2/He/std serials carry forward from tank changes
            before start_date; only rows from start_date on are returned.
        """
        df = self.load_tank_logs()
        df = self.add_gas_serials(df)
        df = self.add_std_serials(df)
        rows = self.build_rows(df)
        return rows[rows.reading_datetime >= self.start_date].reset_index(drop=True)

    # --------------------------------------------------------- temporary table
    def tmptbl_create(self):
        self.db.doquery(f"DROP TEMPORARY TABLE IF EXISTS {self.tmp_table};")
        self.db.doquery(f"CREATE TEMPORARY TABLE {self.tmp_table} LIKE hats.ng_cylinder_pressures;")

    def tmptbl_fill(self, rows):
        self.tmptbl_create()
        cols = ['inst_num', 'system_num', 'site_num', 'cylinder_type_num', 'cylinder',
                'serial_number', 'reading_datetime', 'pressure', 'rejected', 'comment']
        sql_insert = f"""
            INSERT INTO {self.tmp_table} ({', '.join(cols)})
            VALUES ({', '.join(['%s'] * len(cols))});
        """
        params = []
        for row in rows[cols].itertuples(index=False):
            params.append(tuple(row))
            if self.db.doMultiInsert(sql_insert, params):
                params = []
        self.db.doMultiInsert(sql_insert, params, all=True)

    def tmptbl_summary(self):
        return self.hdb.to_df(f"""
            SELECT cylinder, inst_num, COUNT(*) AS n,
                   COUNT(DISTINCT serial_number) AS n_serials,
                   SUM(serial_number IS NULL) AS no_serial,
                   MIN(reading_datetime) AS first, MAX(reading_datetime) AS last
            FROM {self.tmp_table}
            GROUP BY cylinder, inst_num ORDER BY cylinder, inst_num;
        """)

    # ------------------------------------------- compare and update ng_cylinder_pressures
    KEY_COLS = ['inst_num', 'cylinder', 'reading_datetime']
    # columns checked for changes; 'rejected' is left alone so manual rejections are kept
    DIFF_COLS = ['system_num', 'site_num', 'cylinder_type_num', 'serial_number', 'pressure', 'comment']
    TARGET = 'hats.ng_cylinder_pressures'

    def join_on(self):
        return ' AND '.join(f'p.{c} = t.{c}' for c in self.KEY_COLS)

    def differs(self):
        """ SQL that is true when any DIFF_COLS value differs (NULL safe, case sensitive). """
        return ' OR '.join(f'NOT (BINARY p.{c} <=> BINARY t.{c})' for c in self.DIFF_COLS)

    def new_rows(self):
        return self.hdb.to_df(f"""
            SELECT t.* FROM {self.tmp_table} t
            LEFT JOIN {self.TARGET} p ON {self.join_on()}
            WHERE p.num IS NULL
            ORDER BY t.reading_datetime, t.cylinder;
        """)

    def changed_rows(self):
        """ Existing rows whose values differ, with the old (db) and new (log) values. """
        cols = ', '.join(f'p.{c} AS {c}_db, t.{c} AS {c}_log' for c in self.DIFF_COLS)
        return self.hdb.to_df(f"""
            SELECT p.num, {', '.join(f't.{c}' for c in self.KEY_COLS)}, {cols}
            FROM {self.tmp_table} t
            JOIN {self.TARGET} p ON {self.join_on()}
            WHERE {self.differs()}
            ORDER BY t.reading_datetime, t.cylinder;
        """)

    def orphan_rows(self):
        """ Rows for this instrument in the db that are no longer in the logs (reported, not deleted). """
        return self.hdb.to_df(f"""
            SELECT p.* FROM {self.TARGET} p
            LEFT JOIN {self.tmp_table} t ON {self.join_on()}
            WHERE t.num IS NULL
              AND p.site_num = %s AND p.inst_num IN (%s, %s)
              AND p.reading_datetime >= %s
            ORDER BY p.reading_datetime, p.cylinder;
        """, [self.SITE_NUM, self.INST_BEFORE, self.INST_AFTER, self.start_date])

    def update_db(self):
        """ Update changed rows and insert new ones in one transaction. Returns (updated, inserted). """
        set_cols = ', '.join(f'p.{c} = t.{c}' for c in self.DIFF_COLS)
        ins_cols = self.KEY_COLS + self.DIFF_COLS + ['rejected']
        try:
            self.db.doquery("START TRANSACTION;", commit=False)
            updated = self.db.doquery(f"""
                UPDATE {self.TARGET} p
                JOIN {self.tmp_table} t ON {self.join_on()}
                SET {set_cols}
                WHERE {self.differs()};
            """, commit=False)
            inserted = self.db.doquery(f"""
                INSERT INTO {self.TARGET} ({', '.join(ins_cols)})
                SELECT {', '.join(f't.{c}' for c in ins_cols)}
                FROM {self.tmp_table} t
                LEFT JOIN {self.TARGET} p ON {self.join_on()}
                WHERE p.num IS NULL;
            """, commit=False)
            self.db.doquery("COMMIT;", commit=False)
        except Exception:
            self.db.doquery("ROLLBACK;", commit=False)
            raise
        return updated or 0, inserted or 0

    def tmptbl_output(self, start_dt):
        return self.hdb.to_df(f"""
            SELECT * FROM {self.tmp_table}
            WHERE reading_datetime >= %s
            ORDER BY reading_datetime, cylinder_type_num, cylinder;
        """, [start_dt])

    # ----------------------------------------------------------------- report
    def report(self):
        print(f'Skipped {len(self.skipped)} tank log lines with errors.')
        if self.verbose:
            for f, line, reason in self.skipped:
                print(f'  {f}: {reason:10s} {line}')

        missing = [c for c in self.gas_changes if c[2] is None]
        print(f'{len(self.gas_changes)} N2/He tank changes found, {len(missing)} without a readable serial.')
        if self.verbose:
            for dt, gas, serial, comment in self.gas_changes:
                print(f'  {dt:%Y-%m-%d %H:%M} {gas:2s} {str(serial):14s} {comment}')

        print(f'{len(self.std_changes)} tertiary/secondary std entries in ports.log.')
        if self.unmatched_stds:
            print(f'  std tanks not found in hats.standards (ignored): {", ".join(self.unmatched_stds)}')
        if self.verbose:
            for r in self.std_changes.itertuples():
                print(f'  {r.dt:%Y-%m-%d %H:%M} port {r.port:>3s} {r.level:9s} {r.serial}')

        if not self.shifted.empty:
            print(f'{len(self.shifted)} rows had the same cylinder and time as an earlier line; moved forward 1 minute.')
            if self.verbose:
                print(self.shifted[['orig_datetime', 'cylinder', 'pressure', 'comment']].to_string(index=False))


def main():
    parser = argparse.ArgumentParser(
        description='Load the PR1 tank pressure logs into a temporary table like hats.ng_cylinder_pressures.')
    parser.add_argument('-s', '--start_date', '--start-date', type=str, metavar='YYMMDD',
                        help='Load readings from this date on (default: the last 30 days).')
    parser.add_argument('-y', '--start-year', type=int, default=2015,
                        help='First year of tank-press.log to read, for tank serials (default 2015).')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='List skipped lines, tank changes, and std tank installs.')
    parser.add_argument('--csv', type=str, help='Also write the rows to this CSV file for review.')
    parser.add_argument('--show', type=str, metavar='YYMMDD',
                        help='Print the temporary table rows from this date on.')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help=f'Compare to {TankPressureLog.TARGET} and report, but do not write.')
    args = parser.parse_args()

    start_date = datetime.strptime(args.start_date, '%y%m%d') if args.start_date else None
    tp = TankPressureLog(start_date=start_date, start_year=args.start_year, verbose=args.verbose)
    print(f'Loading readings from {tp.start_date:%Y-%m-%d %H:%M} on.')
    rows = tp.load()
    tp.report()

    if args.csv:
        rows.to_csv(args.csv, index=False)
        print(f'Wrote {len(rows)} rows to {args.csv}')

    tp.tmptbl_fill(rows)
    print(f'\nTemporary table {tp.tmp_table}:')
    print(tp.tmptbl_summary().to_string(index=False))

    if args.show:
        start_dt = datetime.strptime(args.show, '%y%m%d')
        print(f'\n{tp.tmp_table} rows from {start_dt:%Y-%m-%d}:')
        print(tp.tmptbl_output(start_dt).to_string(index=False))

    # compare to hats.ng_cylinder_pressures
    new, changed, orphans = tp.new_rows(), tp.changed_rows(), tp.orphan_rows()
    print(f'\nCompared to {tp.TARGET}: {len(new)} new, {len(changed)} changed, '
          f'{len(orphans)} in the db but not in the logs (not deleted).')
    if args.verbose:
        for label, df in (('New', new), ('Changed', changed), ('Not in logs', orphans)):
            if not df.empty:
                print(f'\n{label}:')
                print(df.to_string(index=False))

    if args.dry_run:
        print('Dry run, nothing written.')
    elif new.empty and changed.empty:
        print('Nothing to update.')
    else:
        updated, inserted = tp.update_db()
        print(f'{tp.TARGET}: updated {updated} rows, inserted {inserted} rows.')


if __name__ == '__main__':
    main()
