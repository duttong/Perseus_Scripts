#!/usr/bin/env python

import pandas as pd
import time
from pathlib import Path
import argparse
from pathlib import Path
from datetime import datetime, timedelta

import pr1_export

class PR1_db(pr1_export.PR1_base):

    def __init__(self):
        super().__init__()
        self.pr1_start_date = '20150601'        # data before this date is not used.
        self.gcwerks_results = Path('/hats/gc/pr1/results/')
        self.sites = self.gml_sites()           # site codes and numbers
        self.analytes = self.pr1_analytes()     # PR1 analytes (dict of molecule and parameter number)
        self.analysis_table = 'analysis'        # set to a table like analysis_gsd for debugging
        self.raw_data_table = 'raw_data'        # raw_data_gsd for degugging
        self.ancillary_table = 'ancillary_data' # ancillary_data_gsd for debuggin

    def load_gcwerks(self, gas, start_date, stop_date='end'):
        """ Loads GCwerks data for a given gas and returns it as a DataFrame.
            The data files should be stored at self.gcwerks_results path.
            Times should be in YYMM format.
        """
        pr1_start_dt = pd.to_datetime(self.pr1_start_date)
        start_dt = pd.to_datetime(start_date, format='%y%m')
        if stop_date == 'end':
            stop_dt = pd.to_datetime(datetime.today())
        else:
            stop_dt = pd.to_datetime(stop_date, format='%y%m') + pd.offsets.MonthEnd(0)

        if start_dt < pr1_start_dt:
            print(f'Start date "{start_date}" selected was too early, using "{self.pr1_start_date}"')
            start_dt = pr1_start_dt
        
        if gas not in self.molecules:
            print(f'Incorrect gas {gas} name.')
            return

        file = self.gcwerks_results / f'data_{gas}.csv'
        if file.exists():
            df = pd.read_csv(file)
            df.columns = [col.replace(f'{gas}_', '').strip() for col in df.columns]
        else:
            print(f'Missing GCwerks output file: {file}')
            return

        # Make a datetime column and set it as the index
        df = df.assign(dt=pd.to_datetime(df.time))
        df.set_index('dt', inplace=True)

        # trim the dataframe to use start_date (start_dt is the datetime value)
        if stop_date == 'end':
            df = df.loc[start_dt:]
        else:
            df = df.loc[start_dt:stop_dt]

        # parameter and event numbers
        df['pnum'] = self.analytes[gas].strip()
        df['event'] = '0'
        
        df['type'] = df['type'].str.strip()
        df['sample'] = df['sample'].str.strip()
        df['standard'] = df['standard'].str.strip()

        # This section parses the sample field. This field is not standardized. It can have
        # a variety of info including sample site code, sample ID, and pair_ID for HATS flasks.
        # no dash in sample
        # blank and burn runs
        df.loc[df['sample'].str.count('-') == 0, 'site'] = ''
        df.loc[df['sample'].str.count('-') == 0, 'sample_ID'] = df['sample']
        # one dash in sample
        # BLD-badtestB
        pattern = r'^([A-Z]+)-?([A-Za-z0-9]+)?$'
        df.loc[df['sample'].str.count('-') == 1, 'site'] = df['sample'].str.extract(pattern)[0]
        df.loc[df['sample'].str.count('-') == 1, 'sample_ID'] = df['sample'].str.extract(pattern)[1]
        # two dashes in sample
        # SMO-1223-34333 and BLD-badtestA-00
        pattern = r'^([A-Z]{3})-([A-Za-z0-9]+)-?([A-Za-z0-9]+)?$'
        extracted = df['sample'].str.extract(pattern)
        df.loc[(df['sample'].str.count('-') == 2), 'site'] = extracted[0]
        df.loc[(df['sample'].str.count('-') == 2), 'sample_ID'] = extracted[1] + "-" + extracted[2]
        df.loc[(df['sample'].str.count('-') == 2) & (df['type'] == 'HATS'), 'sample_ID'] = extracted[1]
        df.loc[(df['sample'].str.count('-') == 2) & (df['type'] == 'HATS'), 'event'] = extracted[2]
        # this is needed if the previous line if the "pattern" regex doesn't find a extracted group 2
        df.loc[df['event'].isnull(), 'event'] = 0 

        # lookup site number, 0 if not found
        df['site_num'] = df['site'].map(self.sites).fillna(0).astype(int)

        # Data processing steps...
        standards = self.pr1_standards()
        standards_num = {k: v[0] for k, v in standards.items()}
        standards_sn = {k: v[1] for k, v in standards.items()}
        # standardized serial number
        df['serial_num'] = df['standard'].map(standards_sn).fillna('unknown')
        # unique number assigned to a serial number (this column is slatted to go away)
        df['standard_num'] = df['standard'].map(standards_num).fillna(0)
        
        # code lab_num not sure what it is used for?
        lab_num_mapping = {
            'PFP': 1,
            'CCGG': 1,
            'tank': 1,
            'Tank': 2,
            'FLASK': 2,
            'HATS': 2
        }
        # Map the 'type' column to 'lab_num' using the mapping dictionary
        df['lab_num'] = df['type'].str.upper().map(lab_num_mapping).fillna(0).astype(int)

        # clean up ancillary data
        ancillary_table = {'psamp0':27, 'psamp':28, 'psampnet':26, 'T1':29}
        # add pnet
        df['psampnet'] = pd.to_numeric(df["psamp"], errors='coerce', downcast="float") - pd.to_numeric(df["psamp0"], errors='coerce', downcast="float")

        # Put columns in order
        columns = ['time', 'type', 'sample', 'site', 'site_num', 'sample_ID', 'event', 'standard', 
                   'serial_num', 'standard_num', 'lab_num', 'port', 'psamp0', 'psamp', 'psampnet', 'T1', 
                   'pnum', 'area', 'ht', 'rt', 'w']
        df = df[columns]
        print(f'{gas} gcwerks results loaded.')

        return df
    
    def tmptbl_create(self):
        """ Create a temporary table for manipulation and insertion. 
             Drop the temporary table if it already exists
        """
        drop_table_query = f"DROP TEMPORARY TABLE IF EXISTS t_data;"
        self.db.doquery(drop_table_query)

        # SQL to create a temporary table
        create_table_query = f"""
            CREATE TEMPORARY TABLE t_data AS
            SELECT 
                a.num, 
                a.analysis_datetime, 
                a.inst_num, 
                a.sample_ID, 
                a.site_num, 
                a.sample_type, 
                a.port, 
                a.standards_num, 
                a.std_serial_num, 
                a.event_num, 
                a.lab_num,   
                r.analysis_num, 
                r.parameter_num, 
                r.peak_area, 
                r.peak_height, 
                r.peak_width, 
                r.peak_RT,
                r.peak_area AS p,
                r.peak_area AS p0,
                r.peak_area AS pnet,
                r.peak_area AS t1
            FROM 
                analysis a 
                JOIN raw_data r ON r.analysis_num = a.num
            WHERE 
                1 = 0;  # This ensures the table is created empty
        """
        self.db.doquery(create_table_query)

    @staticmethod
    def NULL(val):
        return None if pd.isnull(val) else val

    def tmptbl_fill(self, df):
        """ Create and fill the temporary table with data from the df """

        self.tmptbl_create()    # create temporary table

        sql_insert = """ 
        INSERT INTO t_data (
            analysis_num, analysis_datetime, inst_num, sample_ID, site_num, sample_type, port, 
            standards_num, std_serial_num, event_num, lab_num, parameter_num,
            peak_area, peak_height, peak_width, peak_RT, p, p0, pnet, t1
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        );
        """

        pnum = df['pnum'].values[0]     # parameter number

        params = []
        for _, row in df.iterrows():
            anum = 0
            sid = row.sample_ID if not pd.isnull(row.sample_ID) else ''
            p0 = (
                anum, row.time, self.inst_num, sid, row.site_num, row.type, row.port,
                row.standard_num, row.serial_num, row.event, row.lab_num, pnum,
                self.NULL(row.area), self.NULL(row.ht), self.NULL(row.w), self.NULL(row.rt),
                self.NULL(row.psamp), self.NULL(row.psamp0), self.NULL(row.psampnet), self.NULL(row.T1)
            )
            if row.type != 'unknown' and row.type != 'TEST':
                params.append(p0)
                if self.db.doMultiInsert(sql_insert, params): 
                    params=[]

        self.db.doMultiInsert(sql_insert, params, all=True)

        self.tmptbl_get_eventnum()       # get event_num info from ccgg.flask_event table
        self.tmptbl_get_analnum()        # fill in previous anaysis data

    def tmptbl_get_eventnum(self):
        """
        Update the `event_num` in the temporary table `t_data` based on matching criteria 
        from the `ccgg.flask_event` table.
        """
        sql = """
            UPDATE t_data t
            SET event_num = (
                SELECT num
                FROM ccgg.flask_event
                WHERE id = t.sample_ID 
                AND site_num = t.site_num 
                AND date < t.analysis_datetime
                ORDER BY date DESC
                LIMIT 1
            )
            WHERE t.sample_type != 'HATS';
        """
        self.db.doquery(sql)

    def tmptbl_get_analnum(self):
        """
        Update the `analysis_num` in the temporary table `t_data` based on matching criteria 
        from the `analysis` table.
        """
        sql = f"""
            UPDATE t_data t
            SET analysis_num = (
                SELECT num
                FROM {self.analysis_table}
                WHERE analysis_datetime = t.analysis_datetime 
                AND inst_num = t.inst_num 
                AND event_num = t.event_num
            );
        """
        self.db.doquery(sql)

    def tmptbl_insert_analysis_debug(self):
        # Returns missing rows
        sql = f"""
            SELECT DISTINCT 
                analysis_datetime, inst_num, sample_ID, site_num, sample_type, port, 
                standards_num, std_serial_num, event_num, lab_num
            FROM t_data 
            WHERE analysis_num = 0;
        """
        inserted = self.db.doquery(sql)
        return inserted

    def tmptbl_insert_analysis(self):
        """
        Insert missing analysis rows into the `analysis` table from the temporary table `t_data`
        where `analysis_num` is 0, then update `analysis_num` in `t_data`.
        """
        sql = f"""
            INSERT INTO {self.analysis_table} (
                analysis_datetime, inst_num, sample_ID, site_num, sample_type, port, 
                standards_num, std_serial_num, event_num, lab_num
            )
            SELECT DISTINCT 
                analysis_datetime, inst_num, sample_ID, site_num, sample_type, port, 
                standards_num, std_serial_num, event_num, lab_num
            FROM t_data 
            WHERE analysis_num = 0;
        """
        inserted = self.db.doquery(sql)

        if inserted is not None:
            print(f'Inserted {inserted} new records into hats.{self.analysis_table}.')
            self.tmptbl_get_analnum()

    def tmptbl_update_analysis(self):
        """
        Update analysis rows using data from the temporary table `t_data`.
        """

        self.tmptbl_insert_analysis()    # insert any missing/new analysis rows

        sql = f"""
            UPDATE hats.{self.analysis_table} a, t_data t 
            SET a.standards_num=t.standards_num, a.std_serial_num=t.std_serial_num, a.port=t.port, a.sample_type=t.sample_type
            WHERE a.num=t.analysis_num and t.analysis_num!=0
        """
        updated = self.db.doquery(sql)
        if updated is not None:
            print(f'Updated {updated} records in hats.{self.analysis_table}.')

    def tmptbl_update_raw_data(self):
        # update area, height, w, rt, etc with data from the t_data temporary table
        sql = f"""
            INSERT INTO {self.raw_data_table} (analysis_num, parameter_num, peak_area, peak_height, peak_width, peak_RT)
            SELECT
                t.analysis_num,
                t.parameter_num,
                t.peak_area,
                t.peak_height,
                t.peak_width,
                t.peak_RT
            FROM
                t_data t
            ON DUPLICATE KEY UPDATE
                peak_area = IF(VALUES(peak_area) <> {self.raw_data_table}.peak_area, VALUES(peak_area), {self.raw_data_table}.peak_area),
                peak_height = IF(VALUES(peak_height) <> {self.raw_data_table}.peak_height, VALUES(peak_height), {self.raw_data_table}.peak_height),
                peak_width = IF(VALUES(peak_width) <> {self.raw_data_table}.peak_width, VALUES(peak_width), {self.raw_data_table}.peak_width),
                peak_RT = IF(VALUES(peak_RT) <> {self.raw_data_table}.peak_RT, VALUES(peak_RT), {self.raw_data_table}.peak_RT);
        """
        updated = self.db.doquery(sql)
        if updated is not None:
            print(f'Updated {updated} rows in hats.{self.raw_data_table}')

    def tmptbl_update_ancillary_data(self):
        # Inserts or updates four parameters p, p0, pnet, and t1 into the ancillary_data table
        parameters = [(26, 'pnet'), (27, 'p0'), (28, 'p'), (29, 't1')]

        for param_num, column in parameters:
            sql = f"""
                INSERT INTO {self.ancillary_table} (analysis_num, ancillary_num, value)
                SELECT t.analysis_num, {param_num}, t.{column} FROM t_data t
                ON DUPLICATE KEY UPDATE
                analysis_num=VALUES(analysis_num), ancillary_num={param_num}, value=VALUES(value);
            """
            self.db.doquery(sql)
        
    def tmptbl_output(self):
        return pd.DataFrame(self.db.doquery("SELECT * from t_data;"))


def get_default_date():
    return (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')


def parse_molecules(molecules):
    if molecules:
        try:
            molecules = molecules.replace('1,2-DCE', '12-DCE')
            molecules = molecules.replace(' ','')   # remove spaces
            return molecules.split(',')
        except AttributeError:      # already a list. just return
            return molecules
    return []


def main():
    pr1 = PR1_db()

    parser = argparse.ArgumentParser(description='Insert Perseus GCwerks data into HATS db for selected date range. If no start_date \
                                     is specifide then work on the last 30 days of data.')
    parser.add_argument('date', nargs='?', default=get_default_date(), help='Date in the format YYYYMMDD or YYYYMMDD.HHMM')
    parser.add_argument('-m', '--molecules', type=str, default=pr1.molecules,
                        help='Comma-separated list of molecules. Add quotes around the list if spaces are used. Default all molecules.')
    parser.add_argument('-x', '--extract', action='store_true', help='Re-extract data from GCwerks first.')
    parser.add_argument('--list', action='store_true', help='List all available molecule names.')

    args = parser.parse_args()

    if args.list:
        molecules_c = [m.replace(',', '') for m in pr1.molecules]       # remove commas from mol names
        print(f"Valid molecule names: {', '.join(molecules_c)}")
        quit()
    
    start_date = args.date
    molecules = parse_molecules(args.molecules)     # returns a list of molecules

    print(f"Start date: {start_date}")
    print("Processing the following molecules: ", molecules)

    if args.extract:
        pr1_export.PR1_GCwerks_Export().export_gc_data(start_date, molecules)
    
    for gas in molecules:
        df = pr1.load_gcwerks(gas, start_date)
        pr1.tmptbl_fill(df)             # create and fill in temp data table with GCwerks results
        pr1.tmptbl_update_analysis()    # insert and update any rows in hats.analysis with new data
        pr1.tmptbl_update_raw_data()    # update the hats.raw_data table with area, ht, w, rt
        pr1.tmptbl_update_ancillary_data()  # updates the hats.ancillary table with p, p0, pnet, and t1 values


if __name__ == '__main__':
    main()
