"""Subject identity must not change with prose, typography or table membership."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from group_msg_monitor.institution_match import resolve_institution
from group_msg_monitor.relay_core import institution_key


class InstitutionMatchTests(unittest.TestCase):
    def resolve(self, text, labels=('富国基金（10.30）', '建信基金', '国联安')):
        rows = {institution_key(label): {'label': label, 'row': i + 2}
                for i, label in enumerate(labels)}
        return resolve_institution(text, rows, institution_key)

    def test_body_mentions_never_change_subject(self):
        for heading in ('【富国基金】9-10月打分', '[富国基金]季度服务', '富国基金权益服务打分'):
            for body in ('【本周五】前完成[抱拳]', '请参考【建信基金】的服务记录', '国联安服务很好'):
                with self.subTest(heading=heading, body=body):
                    result = self.resolve('#Group Note\n#接龙\n' + heading + '\n' + body)
                    self.assertEqual((result['status'], result['key']), ('matched', '富国'))
                    self.assertTrue(all(e['scope'] == heading for e in result['evidence']))

    def test_missing_subject_cannot_redirect_to_institution_in_body(self):
        result = self.resolve('#接龙\n【富国基金】打分\n请参考【建信基金】记录', ('建信基金',))
        self.assertEqual(result['status'], 'missing')
        self.assertIsNone(result['key'])
        self.assertEqual(result['unknown_subjects'], ['富国基金'])

    def test_joint_subject_is_not_resolved_by_filtering_out_missing_rows(self):
        for title in ('【富国基金】【建信基金】打分', '富国基金、建信基金季度打分',
                      '【富国基金】和建信基金打分', '富国基金与【建信基金】打分',
                      '建信基金、【富国基金】打分'):
            for rows in (('富国基金', '建信基金'), ('富国基金',)):
                with self.subTest(title=title, rows=rows):
                    result = self.resolve(title, rows)
                    self.assertEqual(result['status'], 'ambiguous')
                    self.assertIsNone(result['key'])

    def test_explicit_fields_work_after_greeting_and_generic_title(self):
        result = self.resolve('#接龙\n各位老师好！\n服务打分通知\n机构：国联安\n截止【本周五】')
        self.assertEqual((result['status'], result['key']), ('matched', '国联安'))
        self.assertEqual(result['evidence'][0]['role'], 'institution_field')

    def test_conflicting_field_and_title_stops_update(self):
        result = self.resolve('【富国基金】打分\n机构：建信基金')
        self.assertEqual(result['status'], 'ambiguous')

    def test_equivalent_subject_and_field_are_one_identity(self):
        result = self.resolve('【富国基金】打分\n机构：富国基金权益')
        self.assertEqual((result['status'], result['key']), ('matched', '富国'))
        missing = self.resolve('【富国基金】打分\n机构：富国基金权益', ('建信基金',))
        self.assertEqual(missing['status'], 'missing')

    def test_short_institution_name_does_not_match_longer_name(self):
        result = self.resolve('国联安9-10月服务打分', ('国联基金',))
        self.assertEqual(result['status'], 'missing')
        self.assertIsNone(result['key'])

    def test_unstructured_prose_is_not_guessed(self):
        result = self.resolve('请老师联系富国基金客户\n参考建信基金服务记录')
        self.assertIsNone(result['key'])

    def test_inline_marker_and_explicit_label(self):
        result = self.resolve('#接龙 机构名称：富国基金')
        self.assertEqual((result['status'], result['key']), ('matched', '富国'))

    def test_typographic_brackets_alone_are_not_institution_evidence(self):
        result = self.resolve('【本周五】前打完[抱拳]')
        self.assertEqual(result['status'], 'unrecognized')
        self.assertEqual(result['evidence'], [])

    def test_date_heading_cannot_promote_later_body_mention_on_same_line(self):
        for text in ('【本周五】请参考【富国基金】记录', '【注意】【富国基金】打分'):
            result = self.resolve(text)
            self.assertEqual(result['status'], 'unrecognized')
            self.assertIsNone(result['key'])


if __name__ == '__main__':
    unittest.main()
