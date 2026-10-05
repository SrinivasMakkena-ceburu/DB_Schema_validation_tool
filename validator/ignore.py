"""Accepted drift: findings matching an IgnoreRule are set aside, not counted, not fixed."""

SECTIONS = ("schema", "migrations", "pending")


def apply_ignore_rules(report, rules, database_id, project_id):
    ignored = []
    for section in SECTIONS:
        keep = []
        for f in report.get(section, []):
            rule = next((r for r in rules if r.matches(f, database_id, project_id)), None)
            if rule:
                ignored.append({**f, "section": section, "rule_id": rule.pk, "rule": rule.pattern,
                                "rule_note": rule.note})
            else:
                keep.append(f)
        report[section] = keep
    report["ignored"] = ignored
    return report
