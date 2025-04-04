#! /usr/bin/env python

import subprocess
import argparse
import sys
import re
from pathlib import Path
import pandas as pd
from tqdm import tqdm

class PR1_base:
    inst_num = 58  # PR1
    gcexport_path = "/hats/gc/gcwerks-3/bin/gcexport"
    export_dir = Path("/hats/gc/pr1/results")

    def __init__(self):
        sys.path.append('/ccg/src/db/')
        import db_utils.db_conn as db_conn
        self.db = db_conn.HATS_ng()
        self.molecules = self.pr1_molecules()
    
    def gml_sites(self):
        """Returns a dictionary of site codes and site numbers from gmd.site."""
        sql = "SELECT num, code FROM gmd.site;"
        df = pd.DataFrame(self.db.doquery(sql))
        site_dict = dict(zip(df['code'], df['num']))
        return site_dict

    def pr1_standards(self):
        """Returns a dictionary of standards files and a key used in the HATS db."""
        sql = "SELECT num, serial_number, std_ID FROM hats.standards"
        df = pd.DataFrame(self.db.doquery(sql))
        standards_dict = df.set_index('std_ID')[['num', 'serial_number']].T.to_dict('list')
        return standards_dict

    def pr1_analytes(self):
        """Returns a dictionary of PR1 analytes and parameter numbers."""
        sql = f"SELECT param_num, display_name FROM hats.analyte_list WHERE inst_num = {self.inst_num}"
        df = pd.DataFrame(self.db.doquery(sql))
        analytes_dict = dict(zip(df['display_name'], df['param_num']))
        #analytes_dict['12-DCE'] = analytes_dict['1,2-DCE']
        return analytes_dict
    
    def pr1_molecules(self):
        analytes = self.pr1_analytes()
        return list(analytes.keys())

    @staticmethod
    def convert_date_format(date_str):
        """
        Converts a date string from 'YYYYMMDD' or 'YYYYMMDD.HHMM' format to 'YYMM' format.
        If the date is already in 'YYMM' format, it returns the date as is.

        Parameters:
        date_str (str): The date string in 'YYYYMMDD', 'YYYYMMDD.HHMM', or 'YYMM' format.

        Returns:
        str: The date string in 'YYMM' format.
        """
        # Check if the length of the date string is 4 and assume it is in 'YYMM' format
        if len(date_str) == 4:
            return date_str
        
        # Extract the first 6 characters which correspond to 'YYYYMM'
        yyyymm = date_str[:6]
        
        # Convert to 'YYMM'
        yymm = yyyymm[2:]
        
        return yymm

class PR1_GCwerks_Export(PR1_base):

    def __init__(self):
        super().__init__()

    def export_gc_data(self, start_date, molecules, progress=None):
        """
        Calls the gcexport program for each molecule via the subprocess.Popen function. gcexport
        will extract data from gcwerks and send it to a .csv file.

        start_date should be in the form YYMM
        """
        
        processes = []
        for molecule in molecules:
            if molecule in self.molecules:
                filename = f"data_{molecule}.csv"
                params = f"time runtype tank stdtank port psamp0 psamp T1 {molecule}.area {molecule}.ht {molecule}.rt {molecule}.w"
                # params_extra = f"{params} {molecule}.skew {molecule}.rl.a {molecule}.rl.ht {molecule}.rl.report {molecule}.c.a {molecule}.c.ht {molecule}.c.report"
                command = f"{self.gcexport_path} /data/Perseus-1 -csv -nonan -mindate {start_date} {params} > {self.export_dir}/{filename}"
                #subprocess.run(command, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

                # Execute the command and redirect output to /dev/null
                process = subprocess.Popen(command, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                processes.append((process, molecule))
            else:
                print(f'Wrong molecule name: {molecule}')

        for process, molecule in tqdm(processes):
            if progress:
                progress(10)
            process.wait()

        """
        # convert 1,2-DCE file to the name 12-DCE. The old name causes problems due to the comma.
        old = self.export_dir / "data_1,2-DCE.csv"
        new = self.export_dir / "data_12-DCE.csv"

        if not old.exists():
            return
        with open(old, 'r') as file:
            lines = file.readlines()

        columns = lines[0].replace('1,2-DCE', '12-DCE')

        # Write to the new file with the updated header
        with open(new, 'w') as file:
            file.write(columns)
            file.writelines(lines[1:])

        old.unlink()
        new.chmod(0o664)
        """

    @staticmethod
    def verify_start_date(date_str):
        """Verify and convert start date to YYMM format."""
        if re.match(r'^\d{8}$', date_str):
            # Convert YYYYMMDD to YYMM
            return date_str[2:6]
        elif re.match(r'^\d{4}$', date_str):
            # Already in YYMM format
            return date_str
        else:
            raise ValueError(f"Invalid date format: {date_str}. Expected format is YYMM or YYYYMMDD.")            

    @staticmethod
    def parse_molecules(molecules):
        if molecules:
            try:
                molecules = molecules.replace('1,2-DCE', '12-DCE')
                molecules = molecules.replace(' ', '')   # remove spaces
                return molecules.split(',')
            except AttributeError:      # already a list. just return
                return molecules
        return []

    @staticmethod
    def main():
        pr1_export = PR1_GCwerks_Export()
        default_molecules = list(pr1_export.molecules)
        
        parser = argparse.ArgumentParser(description='Export Perseus data with specified start date.')
        parser.add_argument('start_date', type=str, nargs='?', default='2201',
                            help='Start date in the format YYMM (default: 2201)')
        parser.add_argument('-m', '--molecules', type=str, default=default_molecules,
                            help='Comma-separated list of molecules. Add quotes around the list if spaces are used. Default all molecules.')
        parser.add_argument('--list', action='store_true', help='List all available molecule names.')

        args = parser.parse_args()
        if args.list:
            print(f"Valid molecule names: {', '.join(default_molecules)}")
            quit()

        molecules = pr1_export.parse_molecules(args.molecules)     # returns a list of molecules
        start_date = pr1_export.verify_start_date(args.start_date)
        pr1_export.export_gc_data(start_date, molecules)


if __name__ == '__main__':
    PR1_GCwerks_Export.main()
