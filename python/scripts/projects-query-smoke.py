"""Executed in iBIS with a read-only transaction; never imports the test harness."""
import sys


def main(user_id):
    from Objects.Core import Configuration
    Configuration.createNewDatabase = False
    from cpms.translations import installGettext
    installGettext('/ibis/Web')
    from sqlalchemy import text
    from xobjects.container import container
    from xobjects.organization_user import OrganizationUser
    from cpmapi.projects.review import ProjectsReviewQuery, can_open
    from cpmapi.projects.review_columns import columns

    session = container.engine._session
    session.execute(text('SET TRANSACTION READ ONLY'))
    try:
        user = OrganizationUser.getUser(user_id)
        if user is None or not can_open(user):
            print('Smoke environment unavailable: fixture user cannot open Projects; check entitlement and rollout configuration')
            return 125
        selected = [column.id for column in columns(
            user.organization.businessUnitsEnabled, user.organization.enableNotaryBypass) if not column.fixed]
        for sort in ('projectName', 'contractDate'):
            for direction in ('asc', 'desc'):
                result = ProjectsReviewQuery.GET(user=user, body={
                    'version': 1, 'columns': selected,
                    'sort': {'field': sort, 'direction': direction},
                })
                if not result['rows']:
                    print('Smoke environment unavailable: fixture has no eligible Projects rows')
                    return 125
                assert result['totalResults'] >= len(result['rows'])
                assert len({row['id'] for row in result['rows']}) == len(result['rows'])
                print(f'PASS Projects query: {sort} {direction}, rows={len(result["rows"])}', flush=True)
        return 0
    finally:
        session.rollback()


if __name__ == '__main__':
    sys.exit(main(int(sys.argv[1])))
