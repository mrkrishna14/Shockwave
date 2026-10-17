"""Single policy for current verification and migration evidence."""


def latest_scan_for_repository(store, db, enrollment_id, consumer_org_id, repository_id):
    scans=store.list(db,'scan',consumer_org_id,enrollment_id)
    for scan in reversed(scans):
        snapshot=store.get(db,scan['repository_snapshot_id'],'snapshot',consumer_org_id)
        if snapshot and snapshot['repository_connection_id']==repository_id:
            return scan
    return None


def evidence_state(store, db, risk):
    """Only results for the latest selected snapshot can count toward readiness."""
    consumer=risk['consumer_org_id']
    snapshot=store.get(db,risk['repository_snapshot_id'],'snapshot',consumer)
    if not snapshot: return {'current':False,'verified':False,'validated':False}
    latest=latest_scan_for_repository(store,db,risk['enrollment_id'],consumer,snapshot['repository_connection_id'])
    current=bool(latest and latest['repository_snapshot_id']==snapshot['id'])
    verifications=store.list(db,'verification',consumer,risk['id'])
    verification=verifications[-1] if verifications else None
    verified=bool(current and verification and verification['status']=='verified_regression'
                  and verification['repository_snapshot_id']==snapshot['id'])
    migrations=store.list(db,'migration',consumer,risk['id'])
    migration=migrations[-1] if migrations else None
    validation=migration.get('validation') if migration else None
    validated=bool(verified and migration and migration['status'] in ('patch_validated','pr_opened')
                   and migration['repository_snapshot_id']==snapshot['id']
                   and migration.get('validated_against_verification_id')==verification['id']
                   and validation and validation['exit_code']==0 and not validation['timed_out']
                   and not validation['infrastructure_error'] and validation['tests_run']>0)
    return {'current':current,'verified':verified,'validated':validated}
