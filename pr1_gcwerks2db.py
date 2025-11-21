#!/usr/bin/env python

import pandas as pd
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pr1_export

class PR1_db(pr1_export.PR1_base):

    def __init__(self):
        super().__init__()
        self.pr1_start_date = '20150601'        # data before this date is not used.
        self.gcwerks_results = self.export_dir  # Path type
        self.sites = self.gml_sites()           # site codes and numbers
        self.analytes = self.pr1_analytes()     # PR1 analytes (dict of molecule and parameter number)
        self.analysis_table = 'analysis'        # set to a table like analysis_gsd for debugging
        self.raw_data_table = 'raw_data'        # raw_data_gsd for degugging
        self.ancillary_table = 'ancillary_data' # ancillary_data_gsd for debugging
        self.flags_table = 'flags_internal'
        self.pfplogs_path = Path('/data/Perseus-1/logs/pfp.log/')
        self.pfplogs = pd.DataFrame()

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
            df = pd.read_csv(file, na_values=[' nan'])
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

        # trim the last two rows if area and height are nan (this happens when the chrom is not finished)
        if pd.isna(df.iloc[-2]['area']) & pd.isna(df.iloc[-2]['ht']):
            df = df[:-2]
        elif pd.isna(df.iloc[-1]['area']) & pd.isna(df.iloc[-1]['ht']):
            df = df[:-1]

        # insure there are no duplicate index rows
        df = df.reset_index(drop=True)

        # add columns
        # parameter and event numbers
        df['pnum'] = self.analytes[gas].strip()
        df['event'] = '0'
        
        df['type'] = df['type'].str.strip()
        df['sample'] = df['sample'].str.strip()
        df['standard'] = df['standard'].str.strip()
        df['sample_ID'] = df['sample'].str.strip()
        
        # This section parses the sample field. This field is not standardized. It can have
        # a variety of info including sample site code, sample ID, and pair_ID for HATS flasks.
        # no dash in sample
        # blank and burn runs
        df.loc[df['sample'].str.count('-') == 0, 'site'] = ''
        df.loc[df['sample'].str.count('-') == 0, 'sample_ID'] = df['sample']
        # one dash in sample
        # BLD-badtestB
        # need to leave ALM-XXX and SX-XXXX alone
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

        # Every type that is not a flask (CCGG, PFP, HATS) set the site blank and the sample_ID = sample
        df.loc[~df['type'].str.upper().isin(['CCGG', 'PFP', 'HATS']), 'site'] = ''
        df.loc[~df['type'].str.upper().isin(['CCGG', 'PFP', 'HATS']), 'sample_ID'] = df['sample']

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

        # add psampnet
        df['psampnet'] = pd.to_numeric(df["psamp"], errors='coerce', downcast="float") - pd.to_numeric(df["psamp0"], errors='coerce', downcast="float")

        # load pfp.log data if not loaded yet
        self.pfplogs = self.load_pfp_logs()

        # sync pfp.log data to gcwerks data.
        df['dt'] = pd.to_datetime(df['time'])
        df = pd.merge(df, self.pfplogs, on='dt', how='left')
        
        # Put columns in order
        # columns 'PFP_mp_i', 'PFP_mp_f', 'pfp_sn', 'Flask' come from the pfplog files.
        columns = ['time', 'type', 'sample', 'site', 'site_num', 'sample_ID', 'event', 'standard', 
                   'serial_num', 'standard_num', 'lab_num', 'port', 'psamp0', 'psamp', 'psampnet', 'T1', 
                   'pnum', 'area', 'ht', 'rt', 'w', 'start_level', 'end_level', 'PFP_mp_i', 'PFP_mp_f', 'pfp_sn', 'Flask']
        df = df[columns]
        print(f'{gas} gcwerks results loaded.')
        return df
    
    def load_pfp_logs(self):
        """ The pfp logs have pfp manifold pressures. The date and time in these files
            match the gcwerks exported integration date and times. 
            The follow code loads all of the pfp log files into a single dataframe. """
        pfp_vars = []
        for pfplog in self.pfplogs_path.glob('????'):
            # the 1409 file has different columns than the other files. Skip it for now.
            if pfplog.name != '1409':
                pfp = pd.read_csv(pfplog, sep="\s+", skiprows=1,
                                  names=['date', 'time', 'pfp_sn', 'Flask', 'PFP_mp_i', 'PFP_mp_f'])
                pfp_vars.append(pfp)

        pfps = pd.concat(pfp_vars, axis=0)

        # the header of the pfp.log files is mixed throughout the data files.
        # strip out rows where the header is extra text
        pfps['Flask'] = pfps['Flask'].astype(str)
        pfps = pfps[~pfps['Flask'].str.contains('lask')]   # use 'lask' to catch 'Flask' and 'flask'

        # make a datetime column
        pfps['date'] = pfps['date'].astype(str)
        pfps['time'] = pfps['time'].astype(str)
        pfps['time'] = pfps['time'].str.zfill(4)  # fill in 0s 
        pfps['dt'] = pd.to_datetime(pfps['date'].astype(str) + pfps['time'].astype(str), format='%y%m%d%H%M')

        # drop rows where the Flask is nan
        #pfps = pfps[pfps['Flask'].str.isnumeric()]
        
        pfps['PFP_mp_i'] = pd.to_numeric(pfps['PFP_mp_i'], errors='coerce')
        pfps['PFP_mp_f'] = pd.to_numeric(pfps['PFP_mp_f'], errors='coerce')

        pfps = pfps[['dt', 'pfp_sn', 'Flask', 'PFP_mp_i', 'PFP_mp_f']].sort_values(by='dt').reset_index(drop=True)
        return pfps

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
                r.peak_area AS t1,
                r.peak_area AS start_level,
                r.peak_area AS end_level,
                r.peak_area AS pfp_mp_i,
                r.peak_area AS pfp_mp_f
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
            peak_area, peak_height, peak_width, peak_RT, start_level, end_level, p, p0, pnet, t1, pfp_mp_i, pfp_mp_f
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
                self.NULL(row.start_level), self.NULL(row.end_level),
                self.NULL(row.psamp), self.NULL(row.psamp0), self.NULL(row.psampnet), self.NULL(row.T1),
                self.NULL(row.PFP_mp_i), self.NULL(row.PFP_mp_f)
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
        '''
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
        '''
        sql = f"""
            UPDATE t_data t 
            JOIN {self.analysis_table} a on t.analysis_datetime = a.analysis_datetime 
            AND t.inst_num=a.inst_num
            SET t.analysis_num=a.num;
        """
        #print(self.db.doquery("Select count(*) from t_data;", numRows=0))
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
            FROM t_data t
            WHERE analysis_num = 0
            ON DUPLICATE KEY UPDATE 
                event_num=t.event_num;
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
            SET a.standards_num=t.standards_num, a.std_serial_num=t.std_serial_num, 
                a.port=t.port, a.sample_type=t.sample_type, a.site_num=t.site_num,
                a.sample_ID=t.sample_ID, a.event_num=t.event_num, a.lab_num=t.lab_num
            WHERE a.num=t.analysis_num and t.analysis_num!=0
        """
        updated = self.db.doquery(sql)
        if updated is not None:
            print(f'Updated {updated} records in hats.{self.analysis_table}.')

    def tmptbl_update_flags_internal(self):
        tag = '66'      # preliminary data tag

        # GSD added the "ON DUPLICATE KEY" portion on 240828
        sql = f"""
            INSERT INTO {self.flags_table} (analysis_num, parameter_num, iflag, comment, tag_num)
            SELECT
                t.analysis_num, t.parameter_num,
                '*', '', {tag}
            FROM t_data t
            WHERE t.analysis_num = 0
            ON DUPLICATE KEY UPDATE
                iflag = VALUES(iflag),
                comment = VALUES(comment),
                tag_num = VALUES(tag_num);
        """
        inserted = self.db.doquery(sql)
        if inserted is not None:
            print(f'Inserted {inserted} new records into hats.{self.flags_table}.')

    def tmptbl_update_raw_data(self):
        # update area, height, w, rt, etc with data from the t_data temporary table
        sql = f"""
            INSERT INTO {self.raw_data_table} (analysis_num, parameter_num, peak_area, peak_height, peak_width, peak_RT, start_level, end_level)
            SELECT
                t.analysis_num,
                t.parameter_num,
                t.peak_area,
                t.peak_height,
                t.peak_width,
                t.peak_RT,
                t.start_level,
                t.end_level
            FROM
                t_data t
            ON DUPLICATE KEY UPDATE
                peak_area=VALUES(peak_area), peak_height=VALUES(peak_height),
                peak_width=VALUES(peak_width), peak_RT=VALUES(peak_RT),
                start_level=VALUES(start_level), end_level=VALUES(end_level);
        """
        updated = self.db.doquery(sql)
        if updated is not None:
            print(f'Updated {updated} rows in hats.{self.raw_data_table}')

    def tmptbl_update_ancillary_data(self):
        # Inserts or updates four parameters p, p0, pnet, and t1 into the ancillary_data table
        # added PFP_mp_i and PFP_mp_f
        parameters = [(9, 'pfp_mp_f'), (10, 'pfp_mp_i'), (26, 'pnet'), (27, 'p0'), (28, 'p'), (29, 't1')]

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
    return (datetime.now() - timedelta(days=30)).strftime('%y%m')


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
    parser.add_argument('date', nargs='?', default=get_default_date(), help='Date in the format YYMM')
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
    
    for n, gas in enumerate(molecules):
        df = pr1.load_gcwerks(gas, start_date)
        #print(df.loc[df['time'] > '2024-08-28 03:11:00'])
        pr1.tmptbl_fill(df)             # create and fill in temp data table with GCwerks results

        #tmp = pd.DataFrame(pr1.tmptbl_output())
        #print(tmp.loc[tmp.analysis_num >= 317010][['analysis_datetime', 'analysis_num', 'sample_type']])

        pr1.tmptbl_update_flags_internal()  # need to call this before analysis rows are added.
        pr1.tmptbl_update_analysis()    # insert and update any rows in hats.analysis with new data
        pr1.tmptbl_update_raw_data()    # update the hats.raw_data table with area, ht, w, rt
        pr1.tmptbl_update_ancillary_data()  # updates the hats.ancillary table with p, p0, pnet, and t1 values


if __name__ == '__main__':
    main()
