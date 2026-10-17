Contract doubles of Noirit's consumer fixtures (examples/consumers/** is Noirit's).
Each directory is the root of one independent consumer repository. They exist so the
analyzer and scenario tests can run before the real fixtures land. When Noirit's
fixtures land, update ConsumerBinding entries in verification/scenarios/catalog.py
(consumer_file / module / function) and the dependency fixtures in
tests/analyzer/fixtures/demo_dependencies.json to match the real paths and lines.
