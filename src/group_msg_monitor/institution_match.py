"""Resolve a message's subject to a workbook row using explicit evidence.

The workbook supplies identities, not the message's subject. A mention in
instructions is never evidence that the relay belongs to that institution.
Unknown and competing subjects must remain unresolved even when another
subject happens to exist in the workbook. No fuzzy or body-wide matching.
"""
from __future__ import annotations

import re


BRACKET = re.compile(r'【([^】\n]+)】|\[([^\]\n]+)\]')
FIELD = re.compile(r'^(?:机构|机构名称|打分机构|接龙机构)\s*[:：]\s*(.+)$')
BOUNDARY = r'(?=$|[\s\d（(：:，,。!！—\-、/&及与和]|[Qq][1-4]|权益|固收|量化|服务|打分|季度|开始|截止)'
ORGANIZATION = re.compile(r'(?:基金|证券|资管|资产管理|养老|保险|银行|信托|机构)(?:管理)?(?:有限责任公司|有限公司)?$')


def subject_scopes(head):
    """Accept subject position or explicitly labelled fields, never prose mentions."""
    scopes = []
    heading_seen = False
    for raw in head.splitlines():
        line = re.sub(r'^\[appmsg type=\d+\]\s*', '', raw).strip()
        line = re.sub(r'^#(?:Group Note|接龙)\s*', '', line).strip()
        if not line or line.startswith('#'):
            continue
        field = FIELD.fullmatch(line)
        if field:
            scopes.append(('institution_field', field.group(1)))
            heading_seen = True
            continue
        if heading_seen:
            continue
        # Greetings and standalone document headings carry no institution role.
        if re.fullmatch(r'(?:各位(?:老师|领导|同事|大佬)(?:们)?[，, ]*)?(?:大家好|好)[!！。~～]*', line):
            continue
        if re.fullmatch(r'(?:服务)?(?:打分|要分|接龙)(?:通知|提醒|名单)[：:!！。]*', line):
            continue
        scopes.append(('subject', line))
        heading_seen = True
    return scopes


def resolve_institution(head, rows, canonicalize):
    """Return matched/missing/ambiguous/unrecognized plus the evidence used."""
    aliases = {}
    for key, row in rows.items():
        label = re.sub(r'[（(][^()（）]*[)）]', '', row['label']).strip()
        variants = {label, key, key + '基金'}
        if key == '中邮':
            variants.add('中邮创业基金')
        for alias in variants:
            aliases.setdefault(alias, set()).add(key)
    evidence, unknown, keys, untyped = [], {}, set(), []

    def claim(candidate, role, scope, known=None):
        matched = known if known is not None else ({canonicalize(candidate)} & rows.keys())
        keys.update(matched)
        if not matched:
            unknown.setdefault(canonicalize(candidate), candidate)
        evidence.append({'role': role, 'scope': scope, 'candidate': candidate,
                         'target_keys': sorted(matched)})

    scopes = subject_scopes(head)
    for role, scope in scopes:
        subject = re.sub(r'^[^\u3400-\u9fffA-Za-z0-9【\[]+', '', scope)
        # Brackets are only typography. The content must denote a known row
        # or have an organizational noun before it becomes institution evidence.
        remainder = subject
        while remainder:
            match = BRACKET.match(remainder)
            if match:
                candidate = (match.group(1) or match.group(2)).strip()
                if canonicalize(candidate) in rows or ORGANIZATION.search(candidate):
                    claim(candidate, role, scope)
                else:
                    untyped.append(candidate)
                remainder = remainder[match.end():]
            else:
                matches = [(alias, target_keys) for alias, target_keys in aliases.items()
                           if re.match(re.escape(alias) + BOUNDARY, remainder)]
                if not matches:
                    # A missing subject is retained independently of the table.
                    candidate = re.split(r'\d|服务|打分|季度|开始|截止|[，,:：。、/&及与和]', remainder, maxsplit=1)[0].strip()
                    if not candidate:
                        break
                    claim(candidate, role, scope, set())
                    remainder = remainder[len(candidate):]
                else:
                    longest = max(len(alias) for alias, _ in matches)
                    best = [(alias, matched) for alias, matched in matches if len(alias) == longest]
                    claim(best[0][0], role, scope, set().union(*(m for _, m in best)))
                    remainder = remainder[longest:]
            joint = re.match(r'\s*[、/&及与和]\s*', remainder)
            if joint:
                remainder = remainder[joint.end():]
            elif remainder.lstrip().startswith(('【', '[')):
                remainder = remainder.lstrip()
            else:
                break
    status = ('ambiguous' if len(keys) + len(unknown) > 1 else
              'unrecognized' if untyped else
              'matched' if len(keys) == 1 and not unknown else
              'missing' if unknown else 'unrecognized')
    return {'status': status, 'key': next(iter(keys)) if status == 'matched' else None,
            'candidates': sorted(keys), 'unknown_subjects': sorted(unknown.values()),
            'evidence': evidence, 'untyped_subject_tokens': untyped,
            'subject_scopes': [s for _, s in scopes]}
