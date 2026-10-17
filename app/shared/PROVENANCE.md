# Canonical contract provenance

`schemas.py`, `contracts.schema.json`, and `demo-identities.json` are copied without
changes from Ishan's branch at commit
`5f6d06f6cae758775665cb5a2944586b856b9ab5`, under the same `app/shared/` path.

This imports the team's existing contracts; it does not introduce a second schema
implementation. Run with `PYTHONPATH=app` from the repository root. `pytest.ini`
adds that path automatically for tests. Keep this directory aligned with Ishan's
canonical files when merging the platform branch.
