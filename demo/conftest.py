# The consumer templates are standalone repositories with their own test suites (each has a
# `tests` package of its own). The root suite must not import them; tests/demo runs each
# template's suite in a separate process instead.
collect_ignore = ["templates"]
