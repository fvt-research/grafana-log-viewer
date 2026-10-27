"""Convert the exported Clayton cloud dashboard and library panels to MetricsQL."""

import argparse
import copy
import json
import re
from pathlib import Path

DATASOURCE = {"type": "prometheus", "uid": "victoriametrics-clayton"}
JOBS = {
    "_68battery011_alone": "LB101",
    "_68battery012_alone": "LB102",
    "_68battery013_alone": "LB103",
    "_68battery014_alone": "LB104",
    "_68battery015_alone": "LB105",
}


def translate(query: str) -> dict:
    query = re.sub(r"//[^\n]*", "", query)
    measurement = re.search(r'_measurement"\]\s*==\s*\$\{(\w+)\}', query)
    if measurement is None:
        raise ValueError("Missing battery measurement selector")
    variable = measurement[1]
    if variable != "Battery" and variable not in JOBS:
        raise ValueError(f"Unknown battery variable {variable}")
    job = JOBS[variable] if variable in JOBS else "${Battery}"
    names = re.findall(r'_field"\]\s*==\s*"([^"]+)"', query)
    regexes = re.findall(r'_field"\]\s*=~\s*/([^/]+)/', query)
    excluded = re.findall(r'_field"\]\s*!~\s*/([^/]+)/', query)
    matches = []
    if names:
        matches.append("^(?:" + "|".join(re.escape(n) for n in names) + ")$")
    if regexes:
        matches.append("|".join("(?:" + r + ")" for r in regexes))
    for variable in re.findall(
        r"strings.containsStr\(.*?substr:\s*(\w+)", query
    ):
        value = re.search(rf'{variable}\s*=\s*"([^"]+)"', query)
        if value is None:
            raise ValueError(f"Missing string filter {variable}")
        matches.append(re.escape(value[1]))
    for value in re.findall(
        r'strings.containsStr\(.*?substr:\s*"([^"]+)"', query
    ):
        matches.append(re.escape(value))
    if not matches:
        raise ValueError("Missing field selector")

    def anchored(pattern):
        return ".*(?:" + pattern + ").*"

    hints = names + regexes + matches
    buses = set()
    for hint in hints:
        if any(prefix in hint for prefix in ("clayton_", "PLC_", "PDM")):
            buses.add("CAN2")
        if any(
            prefix in hint
            for prefix in ("debug_", "CHG", "BMS", "errors_", "warning_")
        ):
            buses.add("CAN1")
    # Runtime/distance queries also cover PLC lifetime counters.
    if "runtime" in query or "distance" in query:
        buses.add("CAN2")
    if len(buses) == 1:
        bus = f'bus="{next(iter(buses))}"'
    else:
        bus = 'bus=~"CAN1|CAN2"'
    labels = [f'job="{job}"', bus]
    labels.extend(
        "__name__=~" + json.dumps(anchored(pattern)) for pattern in matches
    )
    labels.extend(
        "__name__!~" + json.dumps(anchored(pattern)) for pattern in excluded
    )
    for name in re.findall(r'_field"\]\s*!=\s*"([^"]+)"', query):
        labels.append("__name__!=" + json.dumps(name))
    selector = "{" + ",".join(labels) + "}"
    window = re.search(r"aggregateWindow\(.*?fn:\s*(\w+)", query)
    instant = window is None
    if "range(start: -90d" in query:
        expr = f"tlast_over_time({selector}[90d]) * 1000"
        instant = True
    elif "|> increase()" in query:
        expr = (
            f"WITH (delta = running_sum(label_copy(increase({selector}[$__interval]) "
            'keep_metric_names, "__name__", "signal"))) '
            "(delta - range_first(delta)) keep_metric_names"
        )
    else:
        operation = window[1] if window else "last"
        active_only = bool(re.search(r'_value"\]\s*==\s*1', query))
        if active_only:
            operation = "max"
        function = {
            "mean": "avg_over_time",
            "max": "max_over_time",
            "last": "last_over_time",
            "median": "quantile_over_time",
        }[operation]
        interval = "$__interval" if window else "${__range_s}s"
        argument = "0.5, " if operation == "median" else ""
        expr = f"{function}({argument}{selector}[{interval}]) keep_metric_names"
        if "PDM133_DO11_current_A" in query:
            expr = f"sum by (job) ({expr})"
        if active_only:
            expr = f"({expr}) == 1"
    return {
        "datasource": DATASOURCE.copy(),
        "editorMode": "code",
        "expr": expr,
        "instant": instant,
        "range": not instant,
        "legendFormat": (
            "{{signal}}" if "|> increase()" in query else "{{__name__}}"
        ),
    }


def migrate(source: dict, libraries: dict[str, dict]) -> dict:
    dashboard = copy.deepcopy(source)

    def convert(panels):
        for index, panel in enumerate(panels):
            if "libraryPanel" in panel:
                uid = panel["libraryPanel"]["uid"]
                if uid == "YjsUZ81nk":
                    model = {
                        "type": "status-history",
                        "title": panel["title"],
                        "description": (
                            "Rebuilt from CAN2 PLC status bits: the source "
                            "cloud library panel was deleted."
                        ),
                        "maxDataPoints": 100,
                        "options": {
                            "showValue": "never",
                            "rowHeight": 0.9,
                            "legend": {"showLegend": False},
                            "tooltip": {"mode": "single"},
                        },
                        "fieldConfig": {
                            "defaults": {
                                "unit": "bool_on_off",
                                "color": {"mode": "thresholds"},
                                "thresholds": {
                                    "mode": "absolute",
                                    "steps": [
                                        {"color": "text", "value": None},
                                        {"color": "blue", "value": 1},
                                    ],
                                },
                            },
                            "overrides": [],
                        },
                        "targets": [
                            {
                                **translate(
                                    'r["_measurement"] == ${Battery}\n'
                                    'r["_field"] =~ /PLC_(10|15)_.*/\n'
                                    'r["_field"] !~ /.*(unused|direction_inbyte|direction_outbyte).*/\n'
                                    "aggregateWindow(every: v.windowPeriod, fn: last)"
                                ),
                                "refId": "A",
                            }
                        ],
                    }
                else:
                    model = copy.deepcopy(libraries[uid])
                for key in ("id", "gridPos", "title"):
                    model[key] = panel[key]
                panel = panels[index] = model
                panel.pop("libraryPanel", None)
            if panel.get("type") == "alertlist":
                panel["type"] = "text"
                panel["options"] = {
                    "mode": "markdown",
                    "content": (
                        "**Cloud alert rules are not migrated.** "
                        "No rules were available from the source account. "
                        "Status panels below show recorded faults, not live "
                        "alert evaluations."
                    ),
                }
                panel.pop("datasource", None)
            elif panel.get("targets"):
                panel["datasource"] = DATASOURCE.copy()
                for target in panel["targets"]:
                    if "query" in target:
                        ref_id = target["refId"]
                        hidden = target.get("hide", False)
                        query = target["query"]
                        if panel["id"] == 16 and ref_id == "BMS_SOC":
                            query = query.replace(
                                "/BMS[1-6].*soc.*/", "/^BMS[1-6]_pack_soc$/"
                            )
                        if panel["id"] == 107:
                            query = query.replace(
                                "/CHG[1-4]_IOUT/", "/^CHG[1-4]_IOUT$/"
                            )
                        converted = translate(query)
                        target.clear()
                        target.update(converted, refId=ref_id, hide=hidden)
                if panel["id"] in (38, 39, 40, 41, 42):
                    panel["options"]["reduceOptions"]["fields"] = ""
                    panel["fieldConfig"]["defaults"]["unit"] = "dateTimeAsIso"
                    panel["description"] = (
                        "Last recorded CAN sample in the previous 90 days; "
                        "independent of the selected dashboard time range."
                    )
                if panel["id"] == 112:
                    panel["description"] = (
                        "Reset-aware runtime/distance increase since the "
                        "first displayed interval in the selected time range."
                    )
                    for override in panel["fieldConfig"]["overrides"]:
                        override["matcher"]["options"] = override["matcher"][
                            "options"
                        ].removeprefix("PLC_00_")
                if panel["id"] == 26:
                    panel["fieldConfig"]["overrides"] = [
                        {
                            "matcher": {"id": "byName", "options": name},
                            "properties": [
                                {"id": "displayName", "value": label},
                                {"id": "unit", "value": unit},
                            ],
                        }
                        for name, label, unit in (
                            ("PLC_00_runtime", "PLC runtime", "h"),
                            ("PLC_00_distance", "PLC distance", "lengthkm"),
                            (
                                "Screen_2_SCR_loco_runtime_hrs",
                                "Screen runtime",
                                "h",
                            ),
                            (
                                "Screen_2_SCR_loco_distance_km",
                                "Screen distance",
                                "lengthkm",
                            ),
                        )
                    ]
                if panel["id"] in (36, 37):
                    # Empty Prometheus frames break status-history's time-field check.
                    panel["type"] = "timeseries"
                    panel["description"] = (
                        "Recorded active fault samples only. No data means "
                        "no matching active samples in this time range, "
                        "not proof that all faults are clear."
                    )
                    panel["options"] = {
                        "legend": {"showLegend": True, "placement": "bottom"},
                        "tooltip": {"mode": "multi"},
                    }
                    panel["fieldConfig"]["defaults"]["custom"].update(
                        drawStyle="line",
                        lineWidth=0,
                        showPoints="always",
                        pointSize=6,
                        fillOpacity=0,
                    )
            else:
                panel.pop("datasource", None)
            convert(panel.get("panels", []))

    convert(dashboard["panels"])
    dashboard["id"] = None
    dashboard["uid"] = "clayton-batteries"
    dashboard["version"] = 1
    dashboard["editable"] = True
    dashboard["description"] = (
        "Migrated from cloud locomotive battery diagnostics. "
        "All recorded signals use VictoriaMetrics. Backfill is ongoing; "
        "offline batteries require their recording dates in the time picker. "
        "PLC status was rebuilt; cloud alert rules were unavailable. "
        "UI edits can be saved; provisioning source updates can overwrite them."
    )
    dashboard.setdefault("tags", []).append("Clayton")
    dashboard["templating"] = {
        "list": [
            {
                "name": "Battery",
                "label": "Battery",
                "description": "Locomotive battery (LB101-LB105)",
                "type": "custom",
                "query": "LB101,LB102,LB103,LB104,LB105",
                "current": {"text": "LB103", "value": "LB103"},
                "options": [
                    {"text": job, "value": job, "selected": job == "LB103"}
                    for job in JOBS.values()
                ],
                "includeAll": False,
                "multi": False,
                "hide": 0,
                "skipUrlSync": False,
            }
        ]
    }
    return dashboard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_dir", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = json.loads((args.export_dir / "source.json").read_text())[
        "dashboard"
    ]
    libraries = {
        p.stem.removeprefix("library-"): json.loads(p.read_text())["result"][
            "model"
        ]
        for p in args.export_dir.glob("library-*.json")
    }
    result = migrate(source, libraries)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=True) + "\n"
    )


if __name__ == "__main__":
    main()
