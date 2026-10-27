import argparse
import json
import re
import unittest
from pathlib import Path

import requests

from scripts.migrate_clayton_dashboard import translate

DASHBOARD = (
    Path(__file__).resolve().parents[1]
    / "provisioning/dashboards/clayton/locomotive-batteries.json"
)


class TranslationTest(unittest.TestCase):
    def test_flag_filter_precedes_aggregation(self):
        result = translate(
            'r["_measurement"] == ${Battery}\n'
            'r["_field"] =~ /^warning_[1-2].*/\n'
            'r["_value"] == 1\n'
            "aggregateWindow(every: v.windowPeriod, fn: mean)"
        )
        self.assertIn("max_over_time(", result["expr"])
        self.assertTrue(result["expr"].endswith("== 1"))

    def test_last_seen_uses_sample_timestamp_not_state_value(self):
        result = translate(
            "range(start: -90d, stop: now())\n"
            'r["_measurement"] == ${_68battery015_alone}\n'
            'r["_field"] == "debug_1_current_state"\n'
            "|> last()"
        )
        self.assertIn('job="LB105"', result["expr"])
        self.assertIn("tlast_over_time(", result["expr"])
        self.assertTrue(result["expr"].endswith("* 1000"))
        self.assertTrue(result["instant"])

    def test_string_filters_and_median_keep_signal_names(self):
        result = translate(
            '_data = "cell_voltage"\n'
            'r["_measurement"] == ${Battery}\n'
            'r["_field"] =~ /BMS[1-6].*/\n'
            'r["_field"] !~ /.*fault.*/\n'
            'strings.containsStr(v: r["_field"], substr: _data)\n'
            "aggregateWindow(every: v.windowPeriod, fn: median)"
        )
        self.assertIn("quantile_over_time(0.5,", result["expr"])
        self.assertIn('bus="CAN1"', result["expr"])
        self.assertIn("(?:cell_voltage)", result["expr"])
        self.assertIn("__name__!~", result["expr"])
        self.assertTrue(result["expr"].endswith("keep_metric_names"))

    def test_missing_selectors_fail_instead_of_querying_everything(self):
        with self.assertRaises(ValueError):
            translate('r["_measurement"] == ${Battery}')

    def test_running_increase_keeps_counter_names_for_legends_and_units(self):
        result = translate(
            'r["_measurement"] == ${Battery}\n'
            'r["_field"] =~ /PLC_00_.*/\n'
            "|> increase()\n"
            "aggregateWindow(every: v.windowPeriod, fn: mean)"
        )
        self.assertIn("label_copy(increase(", result["expr"])
        self.assertIn('"__name__", "signal"', result["expr"])
        self.assertEqual(result["legendFormat"], "{{signal}}")
        self.assertIn("delta - range_first(delta)", result["expr"])


def panels(items):
    for panel in items:
        yield panel
        yield from panels(panel.get("panels", []))


def check_live(server):
    session = requests.Session()
    session.trust_env = False

    def query(expression, timestamp=None):
        response = session.get(
            server.rstrip("/") + "/api/v1/query",
            params={"query": expression, "time": timestamp, "nocache": "1"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "success":
            raise RuntimeError(data)
        return data["data"]["result"]

    latest = query(
        'max(tlast_over_time(debug_1_current_state{job=~"LB10[1-5]"}[90d])) by (job)'
    )
    timestamps = {r["metric"]["job"]: float(r["value"][1]) for r in latest}
    if len(timestamps) != 5:
        raise AssertionError(
            f"Expected data for five batteries, found {timestamps}"
        )
    dashboard = json.loads(DASHBOARD.read_text())
    checked = 0
    missing = []
    for panel in panels(dashboard["panels"]):
        for target in panel.get("targets", []):
            has_inputs = False
            for job, timestamp in timestamps.items():
                fixed_job = re.search(r'job="(LB10[1-5])"', target["expr"])
                if fixed_job and fixed_job[1] != job:
                    continue
                expression = (
                    target["expr"]
                    .replace("${Battery}", job)
                    .replace("$__interval", "10m")
                    .replace("${__range_s}", "3600")
                )
                result = query(expression, timestamp)
                # Inactive-only fault rows are correctly absent; their base
                # selectors must still match recorded signals.
                selector = re.search(r'\{job="[^}]+\}', expression)
                if selector is None:
                    raise AssertionError(f"Missing selector: {expression}")
                matched = query(
                    f"count_over_time({selector[0]}[10m]) keep_metric_names",
                    timestamp,
                )
                if not matched:
                    missing.append((panel["id"], target["refId"], job))
                    checked += 1
                    continue
                has_inputs = True
                if not result and not expression.endswith("== 1"):
                    raise AssertionError(
                        f"Panel {panel['id']} / {target['refId']} / {job}: "
                        "query returned no data"
                    )
                checked += 1
            if not has_inputs:
                raise AssertionError(
                    f"Panel {panel['id']} / {target['refId']}: "
                    "no recorded inputs for any battery"
                )
    print(f"Validated {checked} dashboard queries across all five batteries")
    if missing:
        print(
            f"No recorded samples in test window for {len(missing)} cases: {missing}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Check Clayton dashboard migration"
    )
    parser.add_argument(
        "--server", help="Also validate queries against live VM"
    )
    args = parser.parse_args()
    result = unittest.TextTestRunner().run(
        unittest.defaultTestLoader.loadTestsFromTestCase(TranslationTest)
    )
    if not result.wasSuccessful():
        raise SystemExit(1)
    if args.server:
        check_live(args.server)
