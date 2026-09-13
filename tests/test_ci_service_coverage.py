import pytest

from scripts.check_ci_service_coverage import REQUIRED, check


def test_missing_or_skipped_services_fail_ci(tmp_path):
    report = tmp_path / 'tests.xml'
    report.write_text('<testsuite/>', encoding='utf-8')
    with pytest.raises(RuntimeError, match='no collected'):
        check(report)
    cases = ''.join(f'<testcase classname="tests.{name}" name="check"/>' for name in REQUIRED)
    report.write_text('<testsuite>' + cases + '</testsuite>', encoding='utf-8')
    check(report)
    report.write_text('<testsuite>' + cases.replace('name="check"/>',
                      'name="check"><skipped/></testcase>', 1) + '</testsuite>', encoding='utf-8')
    with pytest.raises(RuntimeError, match='skipped'):
        check(report)
