"""Fail CI if required real-service test groups are missing or skipped."""
import sys
import xml.etree.ElementTree as ET

REQUIRED = ('test_auth_postgres', 'test_postgres_submission_store',
            'test_redis_job_queue', 'test_admin_store')


def check(path):
    cases = list(ET.parse(path).iter('testcase'))
    problems = []
    for module in REQUIRED:
        selected = [case for case in cases if case.get('classname', '').endswith(module)]
        if not selected:
            problems.append(f'{module}: no collected cases')
        elif any(case.find('skipped') is not None for case in selected):
            problems.append(f'{module}: required service coverage was skipped')
        elif any(case.find('failure') is not None or case.find('error') is not None
                 for case in selected):
            problems.append(f'{module}: failed service coverage')
    if problems:
        raise RuntimeError('; '.join(problems))
    print('Required PostgreSQL/Redis/admin test groups ran without skips.')


if __name__ == '__main__':
    check(sys.argv[1])
