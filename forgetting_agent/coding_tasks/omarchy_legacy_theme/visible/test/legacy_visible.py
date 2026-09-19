"""Editable public examples; not the authoritative parent-side grade."""
import json
import sys
from pathlib import Path

from legacy_observe import observe

failed = 0
for case in json.loads(Path("test/legacy-cases.json").read_text()):
    try:
        actual = observe(case["fixture"])
    except Exception as error:
        print(f"INCONCLUSIVE: {error}")
        sys.exit(2)
    differences = [key for key, value in case["expected"].items() if actual.get(key) != value]
    if differences:
        failed += 1
        print(f"FAIL {case['args'][0]}: {', '.join(differences)}")
    else:
        print(f"PASS {case['args'][0]}")
sys.exit(1 if failed else 0)
