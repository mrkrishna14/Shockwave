"""Run from workspace root: python -m tests.migration.demo is not required.
Use: python tests/migration/demo.py --output migration/demo-artifacts
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('demo_support', ROOT / 'tests/verification/support.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'migration/demo-artifacts')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    results = []
    for language in ('python', 'node'):
        with tempfile.TemporaryDirectory(prefix='shockwave-demo-') as temp:
            s = support.setup(Path(temp), language)
            verification = s.runner.verify_regression(s.plan, s.root)
            migration = s.migrations.generate_migration(support.request(verification), s.risk, verification, s.root)
            assert verification.status == 'verified_regression'
            assert migration.status == 'patch_validated'
            receipt = s.migrations.validated[migration.id]
            old = s.runner.execute(s.plan, s.root, False, edits=receipt.edits)
            assert old.result.exit_code == 1
            folder = args.output / language
            shutil.copytree(s.artifacts.root, folder, dirs_exist_ok=True)
            (folder / 'migration.patch').write_bytes(s.artifacts.get(migration.patch_artifact_id))
            results.append({'mode': 'trusted_fixture_demo', 'language': language,
                'service_id': 'demo-payments', 'verification': verification, 'migration': migration,
                'patched_old_contract': old.result, 'live_pr_acceptance': 'incomplete'})
    (args.output / 'results.json').write_text(json.dumps(results, default=vars, indent=2) + '\n')
    print(json.dumps([{'language': r['language'], 'verification': r['verification'].status,
        'migration': r['migration'].status, 'target_contract': r['migration'].target_contract_snapshot_id,
        'old_contract_after_patch': 'fails_expected', 'live_pr_acceptance': 'incomplete'} for r in results], indent=2))


if __name__ == '__main__':
    main()
