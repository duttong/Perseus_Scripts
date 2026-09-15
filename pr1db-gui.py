#! /usr/bin/env python

import contextlib
import sys
import traceback
import concurrent.futures
import argparse
import re
from datetime import datetime, timedelta

from PyQt5.QtCore import QObject, QThread, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QLineEdit, QCheckBox,
                             QPushButton, QVBoxLayout, QHBoxLayout, QListWidget, 
                             QTextEdit, QProgressBar)
from PyQt5.QtGui import QTextCursor, QKeySequence

from pr1_export import PRS_GCwerks_Export
from pr1_gcwerks2db import PRS_db


class SignalOutput:
    """File-like object that safely forwards worker output to the GUI."""

    def __init__(self, signal):
        self.signal = signal

    def write(self, message):
        if message:
            self.signal.emit(message)

    def flush(self):
        pass


class AnalyteWorker(QObject):
    message = pyqtSignal(str)
    ready = pyqtSignal(list)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def run(self):
        try:
            self.message.emit('Connecting to the HATS database and loading PRS analytes...\n')
            db = PRS_db()
            self.ready.emit(sorted(db.analytes))
            self.message.emit(f'Loaded {len(db.analytes)} analytes.\n')
        except Exception:
            self.failed.emit(traceback.format_exc())
        finally:
            self.finished.emit()


class UpdateWorker(QObject):
    message = pyqtSignal(str)
    progress = pyqtSignal(int)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, gases, start_date, end_date, extract_first):
        super().__init__()
        self.gases = gases
        self.start_date = start_date
        self.end_date = end_date
        self.extract_first = extract_first

    def run(self):
        try:
            with contextlib.redirect_stdout(SignalOutput(self.message)):
                db = PRS_db()
                total_steps = len(self.gases) * (2 if self.extract_first else 1)
                completed_steps = 0

                if self.extract_first:
                    self.message.emit(f'Exporting {len(self.gases)} analytes from GCwerks...\n')

                    def export_progress(percent):
                        self.progress.emit(int(percent / 100 * len(self.gases) / total_steps * 100))

                    PRS_GCwerks_Export().export_gc_data(
                        self.start_date, self.gases, progress=export_progress)
                    completed_steps = len(self.gases)
                    self.progress.emit(int(completed_steps / total_steps * 100))

                for gas in self.gases:
                    self.message.emit(f'Loading {gas} from {self.start_date} to {self.end_date}...\n')
                    df = db.load_gcwerks(gas, self.start_date, self.end_date)
                    if df is None or df.empty:
                        self.message.emit(f'No rows to update for {gas}; skipped.\n')
                    else:
                        db.tmptbl_fill(df)
                        db.tmptbl_update_flags_internal()
                        db.tmptbl_update_analysis()
                        db.tmptbl_update_raw_data()
                        db.tmptbl_update_ancillary_data()
                        self.message.emit(f'Done inserting {gas} data.\n')
                    completed_steps += 1
                    self.progress.emit(int(completed_steps / total_steps * 100))
        except Exception:
            self.failed.emit(traceback.format_exc())
        finally:
            self.finished.emit()


class PRS_DBGUI:
    def __init__(self):
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

        self.window = QWidget()

        # Set the title and initial size of the main window
        self.window.setWindowTitle('Perseus (PRS) HATS DB Update')
        self.window.setGeometry(100, 100, 600, 600)

        # Create a layout
        layout = QVBoxLayout()

        # Label and list widget for gas selection
        gas_label = QLabel('Select Analytes (ctrl-a for all)')
        layout.addWidget(gas_label)
        self.gas_list = MyListWidget()
        self.gas_list.setSelectionMode(QListWidget.MultiSelection)
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
        self.window.setLayout(layout)

        # Checkbox for "extract gcwerks first"
        self.extract_checkbox = QCheckBox('Re-extract from GCwerks First')
        self.extract_checkbox.setChecked(True)
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
        self.window.setLayout(layout)
        self.execute_button.setEnabled(False)
        self.window.show()
        self.load_analytes()
        sys.exit(app.exec_())

    def clear_selection(self):
        self.gas_list.clearSelection()

    def execute_process(self):
        # Get selected gases
        selected_gases = [item.text() for item in self.gas_list.selectedItems()]
        if not selected_gases:
            self.append_output('Select at least one analyte before starting.\n')
            return

        try:
            t0 = PRS_db.convert_date_format(self.start_date_input.text())
            t1 = PRS_db.convert_date_format(self.end_date_input.text())
            if not re.fullmatch(r'\d{4}', t0) or not re.fullmatch(r'\d{4}', t1):
                raise ValueError('dates must use YYMM format, for example 2609.')
            if t0 > t1:
                raise ValueError('Start date must not be after end date.')
        except ValueError as error:
            self.append_output(f'Invalid date: {error}\n')
            return

        self.execute_button.setEnabled(False)
        self.update_failed = False
        self.progress_bar.setValue(0)
        self.append_output(f'Starting PRS update for {", ".join(selected_gases)}.\n')
        self.update_thread = QThread(self.window)
        self.update_worker = UpdateWorker(selected_gases, t0, t1, self.extract_checkbox.isChecked())
        self.update_worker.moveToThread(self.update_thread)
        self.update_thread.started.connect(self.update_worker.run)
        self.update_worker.message.connect(self.append_output)
        self.update_worker.progress.connect(self.progress_bar.setValue)
        self.update_worker.failed.connect(self.show_error)
        self.update_worker.finished.connect(self.update_thread.quit)
        self.update_worker.finished.connect(self.update_worker.deleteLater)
        self.update_thread.finished.connect(self.update_complete)
        self.update_thread.finished.connect(self.update_thread.deleteLater)
        self.update_thread.start()

    def load_analytes(self):
        self.init_thread = QThread(self.window)
        self.init_worker = AnalyteWorker()
        self.init_worker.moveToThread(self.init_thread)
        self.init_thread.started.connect(self.init_worker.run)
        self.init_worker.message.connect(self.append_output)
        self.init_worker.ready.connect(self.set_analytes)
        self.init_worker.failed.connect(self.show_error)
        self.init_worker.finished.connect(self.init_thread.quit)
        self.init_worker.finished.connect(self.init_worker.deleteLater)
        self.init_thread.finished.connect(self.init_thread.deleteLater)
        self.init_thread.start()

    def set_analytes(self, gases):
        self.gas_list.addItems(gases)
        self.execute_button.setEnabled(True)

    def append_output(self, message):
        self.output_display.moveCursor(QTextCursor.End)
        self.output_display.insertPlainText(message)
        self.output_display.ensureCursorVisible()

    def show_error(self, details):
        self.update_failed = True
        self.append_output(f'ERROR:\n{details}\n')

    def update_complete(self):
        self.gas_list.clearSelection()
        self.execute_button.setEnabled(True)
        self.append_output('Update failed; see error details above.\n' if self.update_failed else 'DONE\n')


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
            #molecules = molecules.replace('1,2-DCE', '12-DCE')
            molecules = molecules.replace(' ','')   # remove spaces
            return molecules.split(',')
        except AttributeError:      # already a list. just return
            return molecules
    return []

def process_gas(gas, start_date, end_date):
    prs = PRS_db()
    df = prs.load_gcwerks(gas, start_date, stop_date=end_date)
    if df is None or df.empty:
        print(f'No rows to update for {gas}; skipped.')
        return
    prs.tmptbl_fill(df)             # create and fill in temp data table with GCwerks results
    prs.tmptbl_update_flags_internal() # need to call this before analysis rows are added.
    prs.tmptbl_update_analysis()    # insert and update any rows in hats.analysis with new data
    prs.tmptbl_update_raw_data()    # update the hats.raw_data table with area, ht, w, rt
    prs.tmptbl_update_ancillary_data()  # updates the hats.ancillary table with p, p0, pnet, and t1 values

def run_in_parallel(molecules, start_date, end_date):
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = []
        for gas in molecules:
            futures.append(executor.submit(process_gas, gas, start_date, end_date))
        for future in concurrent.futures.as_completed(futures):
            future.result()  # This will raise an exception if the callable raised


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

    prs = PRS_db()
    yymm = prs.convert_date_format(args.date0)          # start date
    yymm_end = prs.convert_date_format(args.date1)      # end date

    if args.batch:
        # batch process all molecules
        if args.extract:
            PRS_GCwerks_Export().export_gc_data(yymm, prs.molecules)
        run_in_parallel(prs.molecules, yymm, yymm_end)
        quit()

    elif args.list:
        molecules_c = [m.replace(',', '') for m in prs.molecules]       # remove commas from mol names
        print(f"Valid molecule names: {', '.join(molecules_c)}")
        quit()

    # launch the gui?
    if (args.date0==get_default_yymm() and args.date1 == today_yymm() and args.molecules == 'All') or args.gui:
        PRS_DBGUI()
        quit()

    molecules = prs.molecules if args.molecules == 'All' else parse_molecules(args.molecules)
    if args.extract:
        PRS_GCwerks_Export().export_gc_data(yymm, molecules)

    run_in_parallel(molecules, yymm, yymm_end)
    #for molecule in molecules:
    #    process_gas(molecule, yymm, yymm_end)
    

if __name__ == '__main__':
    main()
