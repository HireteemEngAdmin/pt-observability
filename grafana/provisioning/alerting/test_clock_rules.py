#!/usr/bin/env python3
"""Runs the promtool unit tests for the `clock` alert group in rules.yml.

This repo has no CI, no lint and no schema check for rules.yml, and these
four rules are what pages a human at 3am. Rather than trust a hand-copied
snapshot of their expressions (which can silently drift out of sync with the
real file the moment someone edits a threshold), this script extracts each
clock rule's expr and threshold straight out of the committed rules.yml,
builds a native Prometheus rule file from them, and runs that through
`promtool test rules` against the scenarios in clock_rules_test_cases.yml.

Usage:
    python3 grafana/provisioning/alerting/test_clock_rules.py

Requires: PyYAML (`pip install pyyaml`) and Docker. Pins the same Prometheus
image/version this repo's docker-compose.yml runs, so a passing result here
means what it would mean against the real stack.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    sys.exit(
        "This script needs PyYAML, which the rest of this repo does not use.\n"
        "  pip install pyyaml"
    )

PROM_IMAGE = "prom/prometheus:v3.2.0"  # keep in sync with docker-compose.yml's prometheus service
GROUP_NAME = "clock"
HERE = Path(__file__).resolve().parent
RULES_FILE = HERE / "rules.yml"
TEST_CASES_FILE = HERE / "clock_rules_test_cases.yml"
GENERATED_NAME = "generated_rules.yml"  # must match clock_rules_test_cases.yml's rule_files entry

EVALUATOR_SYMBOLS = {"gt": ">", "lt": "<"}


def extract_clock_rules():
    doc = yaml.safe_load(RULES_FILE.read_text())
    for group in doc["groups"]:
        if group["name"] != GROUP_NAME:
            continue
        rules = []
        for rule in group["rules"]:
            a_node = next(d for d in rule["data"] if d["refId"] == "A")
            threshold_node = next(d for d in rule["data"] if d["refId"] == "THRESHOLD")
            evaluator = threshold_node["model"]["conditions"][0]["evaluator"]
            symbol = EVALUATOR_SYMBOLS[evaluator["type"]]
            param = evaluator["params"][0]
            rules.append({
                "alert": rule["title"],
                # `for: 0m`: this suite tests the alert condition itself, not
                # Grafana's `for` pending duration, which promtool's engine
                # does not model the same way Grafana does.
                "expr": f"{a_node['model']['expr']} {symbol} {param}",
                "for": "0m",
            })
        return rules
    raise SystemExit(f"no '{GROUP_NAME}' group found in {RULES_FILE}")


def main():
    if shutil.which("docker") is None:
        sys.exit(f"This script runs promtool inside {PROM_IMAGE}, so it needs Docker on PATH.")

    rules = extract_clock_rules()

    print(f"Extracted from {RULES_FILE}:")
    for rule in rules:
        print(f"  {rule['alert']}: {rule['expr']}")
    print()

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        generated_doc = {"groups": [{"name": "shipped", "rules": rules}]}
        (tmp / GENERATED_NAME).write_text(yaml.safe_dump(generated_doc, sort_keys=False))
        shutil.copy(TEST_CASES_FILE, tmp / TEST_CASES_FILE.name)

        cmd = [
            "docker", "run", "--rm", "--entrypoint", "promtool",
            "-v", f"{tmp}:/etc/prom-test",
            PROM_IMAGE, "test", "rules", f"/etc/prom-test/{TEST_CASES_FILE.name}",
        ]
        result = subprocess.run(cmd)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
