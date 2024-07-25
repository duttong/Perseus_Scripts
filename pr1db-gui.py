#! /usr/bin/env python

import sys
import time
import concurrent.futures
import argparse
from datetime import datetime, timedelta

from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QLineEdit, QCheckBox, 
                             QPushButton, QVBoxLayout, QHBoxLayout, QListWidget, 
                             QTextEdit, QProgressBar)
from PyQt5.QtGui import QTextCursor, QKeySequence

from pr1_export import PR1_GCwerks_Export
from pr1_gcwerks2db import PR1_db


class PR1_DBGUI():
    def __init__(self):
        #super().__init__()
        self.pr1db = PR1_db()

        app = QApplication(sys.argv)

        # Set the look and feel of the app
        app.setStyleSheet("""
            QWidget {
                background-color: mistyrose;
            }
            QLabel {
                font-family: Helvetica;
                font-size: 16px;
            }
            QHBoxLayout {
                font-family: Helvetica;
                font-size: 16px;
            }
            QPushButton {
                font-family: Helvetica;
                font-size: 16px;
                background-color: lightgrey;
                border: 2px solid black;
            }
            QProgressBar {
                font-family: Helvetica;
                font-size: 16px;
                background-color: lightcoral;
            }
            QCheckBox {
                font-family: Helvetica;
                font-size: 16px;
            }
            QListWidget {
                font-family: Helvetica;
                font-size: 12px;
                background-color: seashell;
            }
            QListWidget::item:selected {
                background-color: lightcoral;
                color: white;
            }
            QComboBox {
                font-family: Helvetica;
                font-size: 16px;
            }
            QComboBox::item:selected {
                background-color: lightcoral;
                color: white;
            }
        """)

        window = QWidget()

        # Set the title and initial size of the main window
        window.setWindowTitle('Persus (PR1) HATS DB Update')
        window.setGeometry(100, 100, 600, 600)

        # Create a layout
        layout = QVBoxLayout()

        # Label and list widget for gas selection
        gas_label = QLabel('Select Analytes (ctrl-a for all)')
        layout.addWidget(gas_label)
        self.gas_list = MyListWidget()
        self.gas_list.setSelectionMode(QListWidget.MultiSelection)
        gases = sorted(self.pr1db.analytes)
        try:
            gases.remove('1,2-DCE')
        except ValueError:
            pass
        self.gas_list.addItems(gases)
        layout.addWidget(self.gas_list)

        # Connect the doubleClicked signal to the slot
        self.gas_list.doubleClicked.connect(self.clear_selection)

       # Setting up the date inputs
        self.start_date_label = QLabel("Start Date (YYMM):")
        self.start_date_input = QLineEdit()
        self.end_date_label = QLabel("End Date (YYMM):")
        self.end_date_input = QLineEdit()
        
        # Default values
        today = datetime.today()
        default_end_date = today.strftime('%y%m')
        default_start_date = (today - timedelta(days=60)).strftime('%y%m')

        self.start_date_input.setText(default_start_date)
        self.end_date_input.setText(default_end_date)

        # Layout setup
        date_layout = QHBoxLayout()
        date_layout.addWidget(self.start_date_label)
        date_layout.addWidget(self.start_date_input)
        date_layout.addWidget(self.end_date_label)
        date_layout.addWidget(self.end_date_input)

        layout.addLayout(date_layout)
        window.setLayout(layout)

        # Checkbox for "extract gcwerks first"
        self.extract_checkbox = QCheckBox('Re-extract from GCwerks First')
        layout.addWidget(self.extract_checkbox)

        # Execute button
        self.execute_button = QPushButton('Execute DB Update')
        self.execute_button.clicked.connect(self.execute_process)
        layout.addWidget(self.execute_button)

        # TextEdit for stdout display
        self.output_display = QTextEdit()
        self.output_display.setReadOnly(True)
        layout.addWidget(self.output_display)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0,100)
        layout.addWidget(self.progress_bar)
        self.progress_bar.setValue(0)

        # Set the layout to the main window
        window.setLayout(layout)
        window.show()
        sys.exit(app.exec_())

    def clear_selection(self):
        self.gas_list.clearSelection()

    def execute_process(self):
        # Redirect stdout
        sys.stdout = self.OutputWrapper(self.output_display)

        # Get selected gases
        selected_gases = [item.text() for item in self.gas_list.selectedItems()]

        # start and end date ranges.
        t0 = self.pr1db.convert_date_format(self.start_date_input.text())
        t1 = self.pr1db.convert_date_format(self.end_date_input.text())

        def progress_callback(progress):
            self.progress_bar.setValue(progress)

        # Check if "extract gcwerks first" is checked
        extract_first = self.extract_checkbox.isChecked()
        if extract_first:
            PR1_GCwerks_Export().export_gc_data(t0, selected_gases, progress=progress_callback)

        # insert into db tables
        for n, gas in enumerate(selected_gases):
            progress = int(n/len(selected_gases)*100)
            progress_callback(progress)
            print(f"Loading {gas} for {t0} to {t1}")
            df = self.pr1db.load_gcwerks(gas, t0, t1)
            self.pr1db.tmptbl_fill(df)             # create and fill in temp data table with GCwerks results
            self.pr1db.tmptbl_update_analysis()    # insert and update any rows in hats.analysis with new data
            self.pr1db.tmptbl_update_raw_data()    # update the hats.raw_data table with area, ht, w, rt
            print(f"Done inserting {gas} data.")

        # Clear the selection in the gas list
        self.gas_list.clearSelection()
        print('DONE\n')
        progress_callback(100)
        time.sleep(2)
        progress_callback(0)


    class OutputWrapper:
        def __init__(self, text_edit):
            self.text_edit = text_edit

        def write(self, message):
            # Append message without adding extra line feed
            self.text_edit.moveCursor(QTextCursor.End)
            self.text_edit.insertPlainText(message)
            self.text_edit.ensureCursorVisible()

        def flush(self):
            pass


class MyListWidget(QListWidget):
    def keyPressEvent(self, event):
        if event.matches(QKeySequence.SelectAll):
            self.selectAll()
        else:
            super().keyPressEvent(event)


def today_yymm():
    return (datetime.now()).strftime('%y%m')

def get_default_yymm():
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

def process_gas(gas, start_date, end_date):
    pr1 = PR1_db()
    df = pr1.load_gcwerks(gas, start_date, stop_date=end_date)
    pr1.tmptbl_fill(df)             # create and fill in temp data table with GCwerks results
    pr1.tmptbl_update_analysis()    # insert and update any rows in hats.analysis with new data
    pr1.tmptbl_update_raw_data()    # update the hats.raw_data table with area, ht, w, rt
    pr1.tmptbl_update_ancillary_data()  # updates the hats.ancillary table with p, p0, pnet, and t1 values

# the parallel stuff can cause issues when two process try to insert into a table at the same time.
# switched back to the original in series method. 240724
def run_in_parallel(start_date, molecules):
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = []
        for gas in molecules:
            futures.append(executor.submit(process_gas, gas, start_date))
            time.sleep(1)  # Introduce a 1-second delay between submissions
        for future in concurrent.futures.as_completed(futures):
            future.result()  # This will raise an exception if the callable raised

"""
def run_in_parallel(start_date, molecules):
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [executor.submit(process_gas, gas, start_date) for gas in molecules]
        for future in concurrent.futures.as_completed(futures):
            future.result()  # This will raise an exception if the callable raised
"""

def main():

    parser = argparse.ArgumentParser(description='Insert Perseus GCwerks data into the HATS database for the selected date range. If no start_date is specified, the command will process the last 30 days of data.')
    #parser.add_argument('date', nargs='?', default=get_default_yymm(), help='Date in the format YYMM')
    parser.add_argument('-d0', '--date0', type=str, default=get_default_yymm(), help='Start date in the form YYMM')
    parser.add_argument('-d1', '--date1', type=str, default=today_yymm(), help='End date in the form YYMM')
    parser.add_argument('-m', '--molecules', type=str, default='All',
                        help='Comma-separated list of molecules. Add quotes around the list if spaces are used. Default all molecules.')
    parser.add_argument('-x', '--extract', action='store_true', help='Re-extract data from GCwerks first.')
    parser.add_argument('--list', action='store_true', help='List all available molecule names.')
    parser.add_argument('--batch', action='store_true', help=f'Batch process all gases starting at the YYMM date {get_default_yymm()}.')
    parser.add_argument('--gui', action='store_true', help='Open GUI')

    args = parser.parse_args()

    pr1 = PR1_db()
    yymm = pr1.convert_date_format(args.date0)          # start date
    yymm_end = pr1.convert_date_format(args.date1)      # end date

    if args.batch:
        # batch process all molecules
        if args.extract:
            PR1_GCwerks_Export().export_gc_data(yymm, pr1.molecules)
        #run_in_parallel(yymm, molecules)
        for molecule in pr1.molecules:
            process_gas(molecule, yymm, yymm_end)
        quit()
    elif args.list:
        molecules_c = [m.replace(',', '') for m in pr1.molecules]       # remove commas from mol names
        print(f"Valid molecule names: {', '.join(molecules_c)}")
        quit()

    # launch the gui?
    if (args.date0==get_default_yymm() and args.date1 == today_yymm() and args.molecules == 'All') or args.gui:
        PR1_DBGUI()
        quit()

    molecules = pr1.molecules if args.molecules == 'All' else parse_molecules(args.molecules)
    if args.extract:
        PR1_GCwerks_Export().export_gc_data(yymm, molecules)

    #run_in_parallel(yymm, molecules)
    for molecule in molecules:
        process_gas(molecule, yymm, yymm_end)
    

if __name__ == '__main__':
    main()