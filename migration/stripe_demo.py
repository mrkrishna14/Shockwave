"""Offline SDK demo with explicit scanner fixtures; no GitHub writes or Stripe requests.

Run with PYTHONPATH=app python -m migration.stripe_demo --output .shockwave-demo
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from shared.schemas import APIChange, Dependency, Evidence, MigrationRequest, RepositorySnapshot
from analyzer.impact import analyze_impact
from integrations.storage import ArtifactStore
from migration.patches import ApprovedTarget
from migration.service import MigrationService
from verification.runner.profiles import Scenario, TrustedSnapshot
from verification.runner.service import RunnerService
from verification.scenarios.plans import PlanConfig, build_verification_plan

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ID = 'stripe.payment-intent-amount-units.simulated.typescript'
NOTICE = ('SIMULATED Stripe change; not an announced Stripe change. Scanner evidence is '
          'a team-authored fixture, not output of a working repository scanner. '
          'Execution is restricted to this trusted team-owned fixture, not sandboxed.')


def identity(name):
    return str(uuid5(NAMESPACE_URL, 'shockwave:stripe-demo:' + name))


@dataclass
class DemoContext:
    root: Path
    artifacts: ArtifactStore
    runner: RunnerService
    migrations: MigrationService
    plan: object
    risk: object
    target: ApprovedTarget
    snapshot: RepositorySnapshot


def prepare_demo(output: Path, *, root: Path | None = None) -> DemoContext:
    root = (root or ROOT / 'demo/stripe-checkout').resolve()
    if not (root / 'node_modules/stripe/package.json').is_file():
        raise ValueError('Run npm ci --ignore-scripts --prefix demo/stripe-checkout first')
    trusted = TrustedSnapshot.register_team_fixture(root, 'a' * 40)
    snapshot = RepositorySnapshot(id=identity('snapshot'), repository_connection_id=identity('connection'),
        commit_sha=trusted.commit_sha, fetched_at=datetime.now(timezone.utc), content_hash=trusted.content_hash)
    source = root / 'src/checkout.ts'
    expression = 'payment.amount / 100'
    line = next(i for i, text in enumerate(source.read_text().splitlines(), 1) if expression in text)
    evidence = Evidence(id=identity('source-evidence'), kind='source', visibility='consumer_private',
        source_uri='fixture://team-owned-stripe-checkout/src/checkout.ts', revision=trusted.content_hash,
        file='src/checkout.ts', line_start=line, line_end=line,
        excerpt='FIXTURE scanner evidence: ' + expression)
    dependency = Dependency(id=identity('dependency'), consumer_org_id=identity('consumer'),
        enrollment_id=identity('enrollment'), repository_snapshot_id=snapshot.id,
        service_id='stripe', endpoint_id=identity('endpoint'), language='typescript',
        field_path='response.amount', origin={'kind':'sdk_response','library':'stripe',
        'call':'stripe.paymentIntents.retrieve'}, usage={'operation':'divide','operand':100,'destination':'display'},
        confidence='direct', evidence=[evidence])
    change = APIChange(id=identity('change'), api_project_id=identity('project'), service_id='stripe',
        endpoint_id=dependency.endpoint_id, old_snapshot_id=identity('current-contract'),
        new_snapshot_id=identity('simulated-contract'), field_path='response.amount', kind='units_changed',
        old_semantics='amount in USD cents (minor units)', new_semantics='amount in USD dollars (major units)',
        structural_breaking=False, evidence=[Evidence(id=identity('change-evidence'), kind='declared_change',
        visibility='provider_owned', source_uri='fixture://stripe-simulated-change', revision='demo-v1',
        json_pointer='/paths/~1v1~1payment_intents~1{intent}/get', excerpt=NOTICE)])
    change.evidence.append(Evidence(id=identity('openapi-evidence'), kind='openapi',
        visibility='public', source_uri='https://github.com/stripe/openapi/blob/'
        '969d8c308ea65b6dbdef4b6b4a71904027a010b8/latest/openapi.spec3.json',
        revision='969d8c308ea65b6dbdef4b6b4a71904027a010b8',
        json_pointer='/paths/~1v1~1payment_intents~1{intent}/get',
        excerpt='Current Stripe PaymentIntent retrieve operation; the proposed units change is simulated.'))
    analysis = analyze_impact(change, [dependency])
    if len(analysis.risks) != 1:
        raise ValueError('Expected one predicted conversion risk from the scanner fixture')
    risk = analysis.risks[0]
    plan = build_verification_plan(risk, snapshot,
        config=PlanConfig(frozenset([snapshot.repository_connection_id])))
    scenario = Scenario.from_catalog(plan, change.new_snapshot_id)
    artifacts = ArtifactStore(output / 'artifacts')
    runner = RunnerService(artifacts, {snapshot.id:trusted}, {scenario.id:scenario},
        consent_check=lambda candidate: candidate == snapshot.id)
    target = ApprovedTarget(risk.id, snapshot.id, snapshot.commit_sha, 'stripe', 'src/checkout.ts',
        line, expression, hashlib.sha256(source.read_bytes()).hexdigest(), change.new_snapshot_id, plan)
    migrations = MigrationService(runner, artifacts, {risk.id:target},
        consent_check=lambda candidate: candidate == snapshot.id)
    return DemoContext(root, artifacts, runner, migrations, plan, risk, target, snapshot)


def run_demo(output: Path):
    context = prepare_demo(output)
    verification = context.runner.verify_regression(context.plan, context.root)
    request = MigrationRequest(risk_id=context.risk.id, verification_id=verification.id,
        repository_snapshot_id=context.snapshot.id,
        target_contract_snapshot_id=context.target.target_contract_snapshot_id, mode='preview')
    migration = context.migrations.generate_migration(request, context.risk, verification, context.root)
    result = {'notice':NOTICE, 'sdk':'stripe@23.0.0', 'github_publication':'not_attempted',
        'commit_note':'a repeated placeholder SHA identifies the local fixture; this is not a GitHub snapshot',
        'risk':context.risk.model_dump(mode='json'), 'verification':verification.model_dump(mode='json'),
        'migration':migration.model_dump(mode='json')}
    output.mkdir(parents=True, exist_ok=True)
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    if migration.patch_artifact_id:
        (output / 'migration.patch').write_bytes(context.artifacts.get(migration.patch_artifact_id))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.shockwave-demo'))
    args = parser.parse_args()
    result = run_demo(args.output.resolve())
    print(NOTICE)
    print(json.dumps({'verification':result['verification']['status'],
        'migration':result['migration']['status'], 'output':str(args.output.resolve())}, indent=2))
    return 0 if result['migration']['status'] == 'patch_validated' else 1


if __name__ == '__main__':
    raise SystemExit(main())
