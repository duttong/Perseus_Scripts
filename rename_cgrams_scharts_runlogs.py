#! /usr/bin/env python

"""
GUI for renaming Perseus-1 run files.

Each run produces a file with the same name in three places:
    /data/Perseus-1/YY/chromatograms/channel0/
    /data/Perseus-1/YY/strip-chart/
    /data/Perseus-1/YY/run.log/

This tool searches for run names, lets the operator enter new names, shows a
final check, and then renames the file in all three directories together.
Every rename is written to a yearly log file in logs/rename_files.log/, in the
same format as logs/operations.log/, so changes can be traced or undone.
Afterwards the operator can re-index GCwerks and remove its results directory,
then reintegrate for all time in GCwerks.
"""

import os
import re
import sys
import argparse
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import Qt, QProcess
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QApplication, QWidget, QDialog, QLabel, QLineEdit,
                             QComboBox, QPushButton, QVBoxLayout, QHBoxLayout,
                             QListWidget, QListWidgetItem, QAbstractItemView,
                             QTableWidget, QTableWidgetItem, QHeaderView,
                             QMessageBox)

DEFAULT_ROOT = Path('/data/Perseus-1')
DEFAULT_GCWERKS_BIN = Path('/data/gcwerks-3/bin')

# Sub-directories (relative to a YY directory) that hold the run files.
SUBDIRS = {
    'chromatograms': Path('chromatograms/channel0'),
    'strip-chart': Path('strip-chart'),
    'run.log': Path('run.log'),
}
ALL_YEARS = 'All years'
MAX_RESULTS = 2000
# Leading YYMMDD.HHMM timestamp of a run name.
PREFIX_RE = re.compile(r'^\d{6}\.\d{4}')

STYLE = """
    QWidget { background-color: mistyrose; }
    QLabel, QPushButton, QLineEdit, QComboBox, QListWidget, QTableWidget {
        font-family: Helvetica;
        font-size: 16px;
    }
    QLineEdit, QListWidget, QTableWidget, QComboBox { background-color: white; }
    QPushButton { background-color: lightgray; padding: 6px 12px; }
"""


class OpsLog:
    """Yearly log files in the same format as logs/operations.log/:

        Rename files log 2026
        ---------------------

        260930.1818<TAB>(iv)<TAB>First line of entry
        <TAB><TAB><TAB>continuation line

        <TAB>.1845<TAB>(iv)<TAB>Later entry on the same day
    """

    TITLE = 'Rename files log'
    DATE_RE = re.compile(r'^(\d{6})\.\d{4}\t', re.MULTILINE)

    def __init__(self, logdir):
        self.logdir = Path(logdir)

    def write(self, initials, lines, now=None):
        """Append one entry. lines[0] goes on the time-stamped line, the rest are continuation lines."""
        now = now or datetime.now()
        if not self.logdir.is_dir():
            self.logdir.mkdir(parents=True)
            os.chmod(self.logdir, 0o775)  # group-writable so other operators can log too
        path = self.logdir / str(now.year)

        text = path.read_text() if path.exists() else ''
        if not text:
            title = f'{self.TITLE} {now.year}'
            prefix = f'\n{title} \n{"-" * len(title)}\n\n'
        elif text.endswith('\n\n'):
            prefix = ''
        else:
            prefix = '\n' if text.endswith('\n') else '\n\n'

        # Like operations.log, later entries on the same day show only .HHMM.
        dates = self.DATE_RE.findall(text)
        today = now.strftime('%y%m%d')
        stamp = f'\t.{now:%H%M}' if dates and dates[-1] == today else f'{today}.{now:%H%M}'

        entry = f'{stamp}\t({initials})\t{lines[0]}\n'
        entry += ''.join(f'\t\t\t{line}\n' for line in lines[1:])
        with open(path, 'a') as f:
            f.write(prefix + entry + '\n')
        if not text:
            os.chmod(path, 0o664)
        return path


class RunFiles:
    """File-system logic: finding and renaming run files across the three sub-directories."""

    def __init__(self, root=DEFAULT_ROOT):
        self.root = Path(root)

    def years(self):
        """Two-digit YY directories that contain at least one of the run sub-directories, newest first."""
        years = []
        for d in self.root.iterdir():
            if re.fullmatch(r'\d{2}', d.name) and any((d / sub).is_dir() for sub in SUBDIRS.values()):
                years.append(d.name)
        # Treat 70-99 as 19xx so e.g. a stray '70' (1970 clock reset) sorts last, not first.
        return sorted(years, key=lambda y: int(y) + (1900 if int(y) >= 70 else 2000), reverse=True)

    def dir_for(self, year, key):
        return self.root / year / SUBDIRS[key]

    def search(self, term, years):
        """Return {(year, name): set(of subdir keys containing it)} for names containing term (case-insensitive)."""
        term = term.lower()
        found = {}
        for year in years:
            for key in SUBDIRS:
                d = self.dir_for(year, key)
                if not d.is_dir():
                    continue
                with os.scandir(d) as it:
                    for entry in it:
                        if term in entry.name.lower() and entry.is_file():
                            found.setdefault((year, entry.name), set()).add(key)
        return found

    def locations(self, year, name):
        """Subdir keys in which year/name currently exists."""
        return [key for key in SUBDIRS if (self.dir_for(year, key) / name).is_file()]

    def validate(self, renames):
        """Check a list of (year, old, new) renames. Returns a list of error strings (empty if OK)."""
        errors = []
        targets = {}
        for year, old, new in renames:
            label = f'{year}/{old}'
            if not new:
                errors.append(f'{label}: new name is empty.')
                continue
            if new == old:
                errors.append(f'{label}: new name is the same as the old name.')
                continue
            if '/' in new or new.startswith('.') or any(c.isspace() for c in new):
                errors.append(f'{label}: "{new}" contains a slash, whitespace, or starts with a dot.')
                continue
            if (year, new) in targets:
                errors.append(f'{label}: "{new}" is also the new name for {year}/{targets[(year, new)]}.')
                continue
            targets[(year, new)] = old
            if not self.locations(year, old):
                errors.append(f'{label}: file no longer exists.')
            for key in SUBDIRS:
                if (self.dir_for(year, key) / new).exists():
                    errors.append(f'{label}: "{new}" already exists in {year}/{SUBDIRS[key]}.')
        return errors

    def rename(self, year, old, new):
        """Rename year/old -> year/new in every sub-directory where it exists.
        If any rename fails, the ones already done for this file are rolled back.
        Returns the list of subdir keys renamed."""
        done = []
        try:
            for key in self.locations(year, old):
                d = self.dir_for(year, key)
                if (d / new).exists():  # os.rename would silently overwrite
                    raise FileExistsError(f'{d / new} already exists')
                os.rename(d / old, d / new)
                done.append(key)
        except OSError:
            for key in reversed(done):
                d = self.dir_for(year, key)
                os.rename(d / new, d / old)
            raise
        return done


class NewNamesDialog(QDialog):
    """Two columns: current file name and an editable box for the new name."""

    def __init__(self, selections, parent=None, previous=None, initials=''):
        super().__init__(parent)
        self.setWindowTitle('Provide New File Names')
        self.selections = selections  # list of (year, name)
        self.resize(1000, 150 + 40 * min(len(selections), 15))

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Edit the new file name for each run. '
                                'The file will be renamed in chromatograms, strip-chart, and run.log.'))

        self.table = QTableWidget(len(selections), 3)
        self.table.setHorizontalHeaderLabels(['Year', 'Current File Name', 'New File Name'])
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)

        self.edits = []
        for row, (year, name) in enumerate(selections):
            for col, text in enumerate((year, name)):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, col, item)
            # Pre-fill with the current name (or the previous attempt) so small edits are easy.
            edit = QLineEdit(previous[row] if previous else name)
            self.table.setCellWidget(row, 2, edit)
            self.edits.append(edit)
        layout.addWidget(self.table)

        initials_row = QHBoxLayout()
        initials_row.addWidget(QLabel('Your initials (for the log):'))
        self.initials_box = QLineEdit(initials)
        self.initials_box.setMaxLength(4)
        self.initials_box.setFixedWidth(80)
        initials_row.addWidget(self.initials_box)
        initials_row.addStretch()
        layout.addLayout(initials_row)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        submit = QPushButton('Submit')
        submit.setDefault(True)
        submit.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(submit)
        layout.addLayout(buttons)

    def new_names(self):
        return [e.text().strip() for e in self.edits]

    def initials(self):
        return self.initials_box.text().strip().lower()


class ConfirmDialog(QDialog):
    """Final check before renaming. Accepted = Confirm, rejected = Return to Editing."""

    def __init__(self, renames, files, initials, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Please Check Renamed Files')
        self.resize(1100, 200 + 40 * min(len(renames), 15))

        layout = QVBoxLayout(self)
        heading = QLabel('Please Check Renamed Files')
        heading.setStyleSheet('font-size: 22px; font-weight: bold;')
        layout.addWidget(heading)

        table = QTableWidget(len(renames), 4)
        table.setHorizontalHeaderLabels(['Year', 'Current File Name', 'New File Name', 'Found In'])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        prefix_changed = False
        for row, (year, old, new) in enumerate(renames):
            where = files.locations(year, old)
            cells = [year, old, new, ', '.join(where)]
            for col, text in enumerate(cells):
                table.setItem(row, col, QTableWidgetItem(text))
            # Highlight a changed YYMMDD.HHMM timestamp -- usually a typo.
            old_p, new_p = PREFIX_RE.match(old), PREFIX_RE.match(new)
            if (old_p and old_p.group()) != (new_p and new_p.group()):
                prefix_changed = True
                table.item(row, 2).setBackground(QColor('khaki'))
            if len(where) < len(SUBDIRS):
                table.item(row, 3).setBackground(QColor('khaki'))
        layout.addWidget(table)

        if prefix_changed:
            layout.addWidget(QLabel('Highlighted: the date/time (YYMMDD.HHMM) part of the name changed.'))
        layout.addWidget(QLabel('Files will be renamed in: ' + ', '.join(str(p) for p in SUBDIRS.values())))
        layout.addWidget(QLabel(f'Logged as: ({initials})'))

        buttons = QHBoxLayout()
        buttons.addStretch()
        back = QPushButton('Return to Editing')
        back.clicked.connect(self.reject)
        confirm = QPushButton('Confirm')
        confirm.clicked.connect(self.accept)
        buttons.addWidget(back)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)


class GCwerksReset:
    """Runs `run-index -gcdir <root>` and then `rm -rf <root>/results`, one after the other,
    without freezing the GUI. A small window shows which step is running."""

    def __init__(self, root, gcwerks_bin, parent, log=lambda lines: None):
        self.root = Path(root)
        self.gcwerks_bin = Path(gcwerks_bin)
        self.parent = parent
        self.log = log  # called with a list of lines when the steps finish or fail
        self.steps = [
            ('Running GCwerks run-index ...', self.gcwerks_bin, './run-index', ['-gcdir', str(self.root)]),
            (f'Removing {self.root / "results"} ...', self.root, 'rm', ['-rf', 'results']),
        ]
        self.process = None

        self.busy = QDialog(parent)
        self.busy.setWindowTitle('Working')
        self.busy.setWindowFlags(self.busy.windowFlags() & ~Qt.WindowCloseButtonHint)
        self.busy.setModal(True)
        layout = QVBoxLayout(self.busy)
        self.busy_label = QLabel()
        self.busy_label.setStyleSheet('font-size: 18px; padding: 20px;')
        layout.addWidget(self.busy_label)
        self.busy.resize(500, 120)

    def start(self):
        self.busy.show()
        self.next_step()

    def next_step(self):
        if not self.steps:
            self.busy.close()
            self.done()
            return
        label, cwd, program, args = self.steps.pop(0)
        self.busy_label.setText(label)
        self.process = QProcess(self.parent)
        self.process.setWorkingDirectory(str(cwd))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.finished.connect(lambda code, status: self.step_finished(label, code, status))
        self.process.errorOccurred.connect(lambda err: self.step_error(label, err))
        self.process.start(program, args)

    def step_finished(self, label, code, status):
        if status != QProcess.NormalExit or code != 0:
            output = bytes(self.process.readAll()).decode(errors='replace').strip()
            self.fail(f'{label}\nfailed with exit code {code}.\n\n{output[-3000:]}')
            return
        self.next_step()

    def step_error(self, label, err):
        if err == QProcess.FailedToStart:
            self.fail(f'{label}\ncould not be started: {self.process.errorString()}')

    def fail(self, msg):
        self.steps = []
        self.busy.close()
        self.log(['GCwerks results removal FAILED:'] + [l for l in msg.splitlines() if l.strip()][:10])
        QMessageBox.critical(self.parent, 'GCwerks reset failed',
                             msg + '\n\nThe remaining steps were not run.')

    def done(self):
        self.log([f'Ran run-index -gcdir {self.root} and removed {self.root / "results"}.',
                  'GCwerks needs reintegrating for all time.'])
        box = QMessageBox(self.parent)
        box.setWindowTitle('Done')
        box.setText('Results removed.  Please reintegrate for all time in GCWerks')
        box.setStyleSheet('QLabel { font-size: 28px; font-weight: bold; }')
        box.setStandardButtons(QMessageBox.Close)
        box.exec_()
        # Closing this final window ends the program and returns to the terminal.
        QApplication.quit()


class RenameGUI(QWidget):
    def __init__(self, files, opslog, gcwerks_bin=DEFAULT_GCWERKS_BIN):
        super().__init__()
        self.files = files
        self.opslog = opslog
        self.gcwerks_bin = gcwerks_bin
        self.initials = ''
        self.setWindowTitle('Rename Perseus-1 Chromatograms, Strip-charts and Run Logs')
        self.resize(900, 650)

        layout = QVBoxLayout(self)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel('Search:'))
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText('Any part of the file name, e.g. CC304855 or 14701')
        self.search_box.returnPressed.connect(self.do_search)
        search_row.addWidget(self.search_box, stretch=1)
        search_row.addWidget(QLabel('Year:'))
        self.year_box = QComboBox()
        years = self.files.years()
        self.year_box.addItems(years + [ALL_YEARS])
        search_row.addWidget(self.year_box)
        search_btn = QPushButton('Search')
        search_btn.clicked.connect(self.do_search)
        search_row.addWidget(search_btn)
        layout.addLayout(search_row)

        self.status = QLabel('Enter a search term and press Search.')
        layout.addWidget(self.status)

        self.results = QListWidget()
        self.results.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.results.itemSelectionChanged.connect(self.update_selected_count)
        layout.addWidget(self.results, stretch=1)

        bottom = QHBoxLayout()
        self.selected_label = QLabel('0 selected')
        bottom.addWidget(self.selected_label)
        bottom.addStretch()
        self.rename_btn = QPushButton('Provide new File names')
        self.rename_btn.setEnabled(False)
        self.rename_btn.clicked.connect(self.provide_names)
        bottom.addWidget(self.rename_btn)
        layout.addLayout(bottom)

    def do_search(self):
        term = self.search_box.text().strip()
        self.results.clear()
        if not term:
            self.status.setText('Please enter a search term.')
            return
        year = self.year_box.currentText()
        years = self.files.years() if year == ALL_YEARS else [year]

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            found = self.files.search(term, years)
        finally:
            QApplication.restoreOverrideCursor()

        keys = sorted(found, key=lambda k: (k[0], k[1]))
        for year, name in keys[:MAX_RESULTS]:
            where = found[(year, name)]
            text = f'{year}   {name}'
            if len(where) < len(SUBDIRS):
                text += f'    (only in: {", ".join(k for k in SUBDIRS if k in where)})'
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, (year, name))
            self.results.addItem(item)

        msg = f'{len(found)} matching run(s).'
        if len(found) > MAX_RESULTS:
            msg += f' Showing the first {MAX_RESULTS}; narrow the search.'
        msg += ' Ctrl-click or Shift-click to select several.'
        self.status.setText(msg)
        self.update_selected_count()

    def update_selected_count(self):
        n = len(self.results.selectedItems())
        self.selected_label.setText(f'{n} selected')
        self.rename_btn.setEnabled(n > 0)

    def provide_names(self):
        selections = [item.data(Qt.UserRole) for item in self.results.selectedItems()]
        selections.sort()
        previous = None
        while True:
            dlg = NewNamesDialog(selections, self, previous, self.initials)
            if dlg.exec_() != QDialog.Accepted:
                return
            previous = dlg.new_names()
            self.initials = dlg.initials()
            renames = [(y, old, new) for (y, old), new in zip(selections, previous)]

            errors = self.files.validate(renames)
            if not re.fullmatch(r'[a-z]{1,4}', self.initials):
                errors.insert(0, 'Please enter your initials (1-4 letters).')
            if errors:
                QMessageBox.warning(self, 'Please fix these problems', '\n'.join(errors))
                continue

            if ConfirmDialog(renames, self.files, self.initials, self).exec_() == QDialog.Accepted:
                self.commit(renames)
                return
            # "Return to Editing": loop back with the names the user typed.

    def commit(self, renames):
        ok, failed, log_lines = [], [], []
        for year, old, new in renames:
            try:
                done = self.files.rename(year, old, new)
                ok.append(f'{year}/{old}  ->  {new}')
                line = f'{year}/{old} -> {new}'
                if len(done) < len(SUBDIRS):
                    line += f'  (only in: {", ".join(done)})'
                log_lines.append(line)
            except OSError as e:
                failed.append(f'{year}/{old}: {e}')
                log_lines.append(f'FAILED, not renamed: {year}/{old} -> {new}: {e}')

        where = ', '.join(str(p) for p in SUBDIRS.values())
        self.write_log([f'Renamed {len(ok)} run file(s) in {where}:'] + log_lines)

        msg = f'Renamed {len(ok)} run(s):\n' + '\n'.join(ok)
        if failed:
            msg += f'\n\nFAILED ({len(failed)}), nothing changed for these:\n' + '\n'.join(failed)
            QMessageBox.critical(self, 'Rename finished with errors', msg)
        else:
            QMessageBox.information(self, 'Rename complete', msg)
        self.do_search()

        if ok:
            self.ask_gcwerks_reset()

    def ask_gcwerks_reset(self):
        answer = QMessageBox.question(
            self, 'Remove GCwerks results?', 'Remove GCwerks results?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.reset = GCwerksReset(self.files.root, self.gcwerks_bin, self, self.write_log)
            self.reset.start()

    def write_log(self, lines):
        try:
            self.opslog.write(self.initials, lines)
        except OSError as e:
            QMessageBox.warning(self, 'Could not write log',
                                f'Could not write to {self.opslog.logdir}:\n{e}\n\n'
                                'Please add this to the log by hand:\n\n' + '\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(
        description='GUI to rename Perseus-1 run files in chromatograms/channel0, strip-chart and run.log together.')
    parser.add_argument('--root', default=str(DEFAULT_ROOT),
                        help=f'Perseus data directory (default: {DEFAULT_ROOT})')
    parser.add_argument('--log', default=None,
                        help='Directory of yearly log files recording every rename '
                             '(default: <root>/logs/rename_files.log)')
    parser.add_argument('--gcwerks-bin', default=str(DEFAULT_GCWERKS_BIN),
                        help=f'Directory containing run-index (default: {DEFAULT_GCWERKS_BIN})')
    args = parser.parse_args()

    root = Path(args.root)
    logdir = Path(args.log) if args.log else root / 'logs' / 'rename_files.log'

    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    gui = RenameGUI(RunFiles(root), OpsLog(logdir), Path(args.gcwerks_bin))
    gui.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
