"""Independently verify the workbook produced by the real Windows GUI acceptance."""
import json
from pathlib import Path
import sys
import zipfile

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from group_msg_monitor.relay_core import file_hash, sheet_path


def main():
    gui = json.loads((ROOT / 'data/diagnostics/windows_live_gui.json').read_text('utf-8'))
    assert gui['status'] == 'passed' and gui['synthetic'] is False
    output = Path(gui['output'])
    work = output.parent.parent
    details = next((work / 'records').glob('*/details.json'))
    result = json.loads(details.read_text('utf-8'))
    backup = Path(result['backup'])
    assert file_hash(backup) == file_hash(ROOT / 'data/reference/要分名单 26Q3.xlsx')
    with zipfile.ZipFile(backup) as before, zipfile.ZipFile(output) as after:
        assert set(before.namelist()) == set(after.namelist())
        changed_members = [name for name in before.namelist() if before.read(name) != after.read(name)]
        assert changed_members == [sheet_path(before, 'Sheet1')], changed_members
    before_book = openpyxl.load_workbook(backup, data_only=False)
    after_book = openpyxl.load_workbook(output, data_only=False)
    expected = {change['cell'] for change in result['changes']}
    changed = set()
    for row in before_book['Sheet1']:
        for cell in row:
            actual = after_book['Sheet1'][cell.coordinate]
            assert cell.style_id == actual.style_id, cell.coordinate
            if cell.value != actual.value:
                changed.add(cell.coordinate)
                assert actual.value is None, cell.coordinate
    before_book.close()
    after_book.close()
    assert changed == expected, {'actual': sorted(changed), 'expected': sorted(expected)}
    report = {'status': 'passed', 'synthetic': False, 'cleared_cells_verified': len(changed),
              'changed_zip_members': changed_members, 'other_zip_members_unchanged': True,
              'styles_unchanged': True, 'baseline_matches_original_reference': True,
              'output': str(output)}
    (ROOT / 'data/diagnostics/windows_live_workbook.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
