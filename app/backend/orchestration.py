"""Platform job execution. Adapter calls happen outside SQLite transactions."""
from pathlib import Path
from threading import Event, Thread

from backend.demo import DemoAdapters, OLD, NEW, ENDPOINT, endpoint, evidence
from backend.store import uid, now
from shared.schemas import (
    APIChange, AnalysisResult, Dependency, DiscoveryInput, DiscoveryResult, Job,
    MigrationRequest, MigrationResult, PublicationAuthorization, RepositoryConnection,
    RepositorySnapshot, Risk, ScanRequest, ScanResult, VerificationResult,
)


def dump(model): return model.model_dump(mode='json')


class Orchestrator:
    def __init__(self, store, queue, adapters_factory, demo):
        self.store, self.queue = store, queue
        self.adapters_factory, self.demo = adapters_factory, demo

    def _record(self, db, id, kind, org=None):
        value = self.store.get(db, id, kind, org)
        if value is None: raise ValueError(f'{kind.upper()}_UNAVAILABLE')
        return value

    def _active(self, db, enrollment_id, org, repo_id=None):
        enrollment = self._record(db, enrollment_id, 'enrollment', org)
        if enrollment['status'] != 'active': raise ValueError('CONSENT_REVOKED')
        if repo_id:
            repo = self._record(db, repo_id, 'repository', org)
            linked = db.execute('SELECT 1 FROM enrollment_repositories WHERE enrollment_id=? AND repository_id=?',
                                (enrollment_id,repo_id)).fetchone()
            if repo['status'] != 'connected' or not linked: raise ValueError('REPOSITORY_REVOKED')
            return enrollment, repo
        return enrollment, None

    def _put_once(self, db, kind, org, value, parent=None):
        old = self.store.get(db, value['id'], kind, org)
        if old is None: self.store.put(db, kind, org, value, parent)
        elif old != value: raise ValueError('RESULT_ID_CONFLICT')

    def _artifact(self, db, org, content, risk, snapshot):
        value={'id':uid(),'content':content[:32000],'enrollment_id':risk['enrollment_id'],
               'repository_id':snapshot['repository_connection_id'],'purged':False}
        self.store.put(db,'artifact',org,value,risk['id'])
        return value['id']

    def _adapter(self, org):
        # The fixture requires its trusted consumer identity at construction. Other
        # adapters receive validated typed inputs through the shared protocols.
        if self.adapters_factory is DemoAdapters:
            return DemoAdapters(consumer_org_id=org)
        if getattr(self.adapters_factory, 'accepts_consumer_org_id', False):
            # Real-module adapters (backend.integrated) also need the consumer identity.
            return self.adapters_factory(consumer_org_id=org)
        return self.adapters_factory()

    def run(self, claim):
        stop=Event()
        def renew():
            while not stop.wait(max(1,self.queue.lease_seconds/3)):
                if not self.queue.heartbeat(claim): return
        heartbeat=Thread(target=renew,daemon=True)
        heartbeat.start()
        try:
            with self.store.read() as db:
                job=self._record(db,claim.job_id,'job')
                task=self._record(db,claim.job_id+'-task','task')
                org=job['owner_org_id']
                kind=job['kind']
                if not self.demo: raise ValueError('ADAPTER_UNAVAILABLE')
                prepared=self._prepare(db,kind,task,org)
            if not self.queue.stage(claim,'executing_fixture'): return
            adapters=self._adapter(org)
            result=self._execute(adapters,kind,prepared,task,org)

            def persist(db):
                self._persist(db,kind,prepared,task,org,result)

            self.queue.finish(claim,persist,result['id'])
        except Exception as exc:
            code=str(exc) if str(exc) in {
                'CONSENT_REVOKED','REPOSITORY_REVOKED','ADAPTER_UNAVAILABLE',
                'PROJECT_UNAVAILABLE','RISK_UNAVAILABLE','SNAPSHOT_UNAVAILABLE',
                'VERIFICATION_UNAVAILABLE','RESULT_ID_CONFLICT'} else 'ADAPTER_FAILED'
            self.queue.fail(claim,code,retryable=code not in {'CONSENT_REVOKED','REPOSITORY_REVOKED','ADAPTER_UNAVAILABLE'})
        finally:
            stop.set()
            heartbeat.join(timeout=1)

    def _prepare(self,db,kind,task,org):
        if kind=='discovery':
            return {'project':self._record(db,task['project_id'],'project',org)}
        if kind=='scan':
            enrollment,repo=self._active(db,task['enrollment_id'],org,task['repository_id'])
            project=self._record(db,enrollment['api_project_id'],'project')
            return {'enrollment':enrollment,'repo':repo,'project':project}
        if kind=='analysis':
            project=self._record(db,task['project_id'],'project',org)
            enrolled=[]
            for enrollment in self.store.list(db,'enrollment',parent=project['id']):
                if enrollment['status']!='active' or not enrollment['share_summary']: continue
                scans=self.store.list(db,'scan',enrollment['consumer_org_id'],enrollment['id'])
                latest={}
                for scan in scans:
                    snapshot=self.store.get(db,scan['repository_snapshot_id'],'snapshot',enrollment['consumer_org_id'])
                    repo=self.store.get(db,snapshot['repository_connection_id'],'repository',enrollment['consumer_org_id']) if snapshot else None
                    if repo and repo['status']=='connected': latest[repo['id']]=scan
                enrolled.append((enrollment,[Dependency.model_validate(d) for s in latest.values() for d in s['dependencies']]))
            return {'project':project,'enrolled':enrolled}
        if kind in ('verification','migration'):
            risk=self._record(db,task['risk_id'],'risk',org)
            snapshot=self._record(db,risk['repository_snapshot_id'],'snapshot',org)
            _,repo=self._active(db,risk['enrollment_id'],org,snapshot['repository_connection_id'])
            prepared={'risk':risk,'snapshot':snapshot,'repo':repo}
            if kind=='migration':
                prepared['verification']=self._record(db,task['verification_id'],'verification',org)
                if task.get('publish'):
                    prepared['migration']=self._record(db,task['migration_id'],'migration',org)
                    grant=db.execute('SELECT allow_pr_write FROM enrollment_repositories WHERE enrollment_id=? AND repository_id=?',
                                     (risk['enrollment_id'],repo['id'])).fetchone()
                    prepared['allow_pr_write']=bool(grant and grant['allow_pr_write'])
                else:
                    prepared['change']=self._record(db,risk['change_id'],'change')
            return prepared
        raise ValueError('ADAPTER_UNAVAILABLE')

    def _execute(self,adapters,kind,prepared,task,org):
        if kind=='discovery':
            project=prepared['project']
            result=DiscoveryResult.model_validate(adapters.discover_endpoints(
                DiscoveryInput(service_id=project['service_id'],snapshot_id=OLD,
                               openapi_documents=['fixture://demo'],frameworks=[]),{},None))
            if result.service_id!=project['service_id'] or any(e.service_id!=project['service_id'] for e in result.endpoints):
                raise ValueError('ADAPTER_FAILED')
            return {'id':uid(),'discovery':dump(result)}
        if kind=='scan':
            project,repo,enrollment=prepared['project'],prepared['repo'],prepared['enrollment']
            snapshot=RepositorySnapshot.model_validate(adapters.fetch_repository(RepositoryConnection.model_validate(repo),task['ref']))
            if snapshot.repository_connection_id!=repo['id'] or snapshot.commit_sha!=task['ref']:
                raise ValueError('ADAPTER_FAILED')
            result=ScanResult.model_validate(adapters.scan_repository(
                ScanRequest(repository_snapshot_id=snapshot.id,enrollment_id=enrollment['id'],
                            service_id=project['service_id'],
                            endpoint_catalog=adapters.endpoint_catalog(project) if hasattr(adapters,'endpoint_catalog') else [endpoint()],
                            supported_languages=['python','javascript','typescript']),snapshot,Path('.')))
            if result.repository_snapshot_id!=snapshot.id or any(
                d.consumer_org_id!=org or d.enrollment_id!=enrollment['id'] or
                d.repository_snapshot_id!=snapshot.id or d.service_id!=project['service_id']
                for d in result.dependencies): raise ValueError('ADAPTER_FAILED')
            return {'id':uid(),'snapshot':dump(snapshot),'scan':dump(result)}
        if kind=='analysis':
            project=prepared['project']
            declared=[APIChange.model_validate(c) for c in task['declared_changes']]
            if hasattr(adapters,'contract_snapshots'):
                # Real-module adapters: discovery snapshots + detect_changes publish the change
                # (with its openapi evidence) instead of trusting the declaration verbatim.
                old,new=adapters.contract_snapshots(project)
                changes=adapters.detect_changes(old,new,declared or adapters.declared_changes(project,old,new),
                                                api_project_id=project['id'])
            else:
                changes=declared or [APIChange(
                id=uid(),api_project_id=project['id'],service_id=project['service_id'],endpoint_id=ENDPOINT,
                old_snapshot_id=OLD,new_snapshot_id=NEW,field_path='response.amount',kind='units_changed',
                old_semantics='USD cents; 1000 = $10',new_semantics='USD dollars; 10 = $10',
                structural_breaking=False,evidence=[evidence('declared_change',False,None,'Fictional units change; not Stripe')])]
            analyses=[]
            for change in changes:
                dependencies=[d for _,items in prepared['enrolled'] for d in items]
                analysis=AnalysisResult.model_validate(adapters.analyze_impact(change,dependencies))
                allowed={e['id']:e['consumer_org_id'] for e,_ in prepared['enrolled']}
                if any(r.change_id!=change.id or allowed.get(r.enrollment_id)!=r.consumer_org_id
                       for r in analysis.risks): raise ValueError('ADAPTER_FAILED')
                analyses.append((dump(change),dump(analysis)))
            return {'id':uid(),'analyses':analyses}
        risk,snapshot=prepared['risk'],prepared['snapshot']
        if kind=='verification':
            plan=adapters.build_verification_plan(Risk.model_validate(risk),RepositorySnapshot.model_validate(snapshot))
            result=VerificationResult.model_validate(adapters.verify_regression(plan,Path('.')))
            if result.risk_id!=risk['id'] or result.repository_snapshot_id!=snapshot['id']:
                raise ValueError('ADAPTER_FAILED')
            return {'id':result.id,'verification':dump(result),'logs':adapters.private_artifacts()['logs']}
        verification=prepared['verification']
        if task.get('publish'):
            migration=prepared['migration'];repo=prepared['repo']
            if not prepared['allow_pr_write'] or migration['status']!='patch_validated' or task['expected_commit']!=snapshot['commit_sha']:
                raise ValueError('CONSENT_REVOKED')
            # Fixture publication is deliberately unavailable. Live publication needs
            # an outbox operation and a fresh remote SHA check before this boundary.
            raise ValueError('ADAPTER_UNAVAILABLE')
        change=prepared['change']
        result=MigrationResult.model_validate(adapters.generate_migration(
            MigrationRequest(risk_id=risk['id'],verification_id=verification['id'],
                             repository_snapshot_id=snapshot['id'],target_contract_snapshot_id=change['new_snapshot_id'],mode='preview'),
            Risk.model_validate(risk),VerificationResult.model_validate(verification),Path('.')))
        if result.risk_id!=risk['id'] or result.repository_snapshot_id!=snapshot['id'] or result.target_contract_snapshot_id!=change['new_snapshot_id']:
            raise ValueError('ADAPTER_FAILED')
        artifacts=adapters.private_artifacts()
        return {'id':result.id,'migration':dump(result),'patch':artifacts['patch'],'logs':artifacts['logs']}

    def _persist(self,db,kind,prepared,task,org,result):
        # This runs under the fenced commit. Consent is checked again after work.
        if kind in ('scan','verification','migration'):
            enrollment=prepared['enrollment'] if kind=='scan' else prepared['risk']
            repo_id=prepared['repo']['id']
            self._active(db,enrollment['id'] if kind=='scan' else enrollment['enrollment_id'],org,repo_id)
        if kind=='discovery':
            project=self._record(db,prepared['project']['id'],'project',org)
            self.store.put(db,'discovery',org,{'id':result['id'],**result['discovery']},project['id'])
        elif kind=='scan':
            snapshot=result['snapshot'];scan=result['scan']
            self._put_once(db,'snapshot',org,snapshot,prepared['repo']['id'])
            self.store.put(db,'scan',org,{'id':result['id'],**scan},prepared['enrollment']['id'])
            for dependency in scan['dependencies']:
                self._put_once(db,'dependency',org,dependency,prepared['enrollment']['id'])
        elif kind=='analysis':
            project=self._record(db,prepared['project']['id'],'project',org)
            for change,analysis in result['analyses']:
                self._put_once(db,'change',org,change,project['id'])
                for risk in analysis['risks']:
                    enrollment=self._record(db,risk['enrollment_id'],'enrollment',risk['consumer_org_id'])
                    if enrollment['status']=='active' and enrollment['share_summary']:
                        self._put_once(db,'risk',risk['consumer_org_id'],risk,enrollment['id'])
                assessment_id=result['id'] if change==result['analyses'][0][0] else uid()
                self.store.put(db,'assessment',org,{'id':assessment_id,'change_id':change['id'],
                    'analysis_mode':analysis['analysis_mode']},project['id'])
        elif kind=='verification':
            v=result['verification']
            if result['logs']: v['artifact_ids']=[self._artifact(db,org,result['logs'],prepared['risk'],prepared['snapshot'])]
            self._put_once(db,'verification',org,v,prepared['risk']['id'])
        elif kind=='migration' and not task.get('publish'):
            m=result['migration']
            m['validated_against_verification_id']=prepared['verification']['id']
            if result['patch']: m['patch_artifact_id']=self._artifact(db,org,result['patch'],prepared['risk'],prepared['snapshot'])
            if m['validation'] and result['logs']:
                m['validation']['artifact_ids']=[self._artifact(db,org,result['logs'],prepared['risk'],prepared['snapshot'])]
            self._put_once(db,'migration',org,m,prepared['risk']['id'])
            self.store.put(db,'readiness',org,{'id':uid(),'risk_id':prepared['risk']['id'],
                'migration_id':m['id'],'approval':'pending' if m['status']=='patch_validated' else 'unknown',
                'merge':'not_merged','deployment':'unknown','deployment_evidence':None},prepared['risk']['id'])
