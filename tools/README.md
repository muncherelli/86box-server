# tools

Helpers for development and releases; nothing here is installed on the host.

- `check-templates.sh <templates dir> <86Box tag>`: start each box template once in 86Box and check the hardware survives.
- `fake-netapply.py`: test-only stand-in for the root helper `netgroups-apply`.
- `tests/`: unit tests for `netgroups-apply` (`python3 -I -m unittest discover -s tools/tests -p 'test_*.py'`).
