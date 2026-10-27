import logging

LIVE_STREAMING = False
LIVE_STREAMING_SERVER = "http://localhost:8428"

TEST_MF4_FILE = "/home/default/work/test_dbc/00000007.MF4"
DBC_FILE_PATHS = ["/home/default/work/test_dbc/d65_brightloops.dbc"]

CAN_INTERFACE = "socketcan"
CAN_CHANNEL = "vcan0"

# Logging Configuration
LOG_LEVEL = logging.INFO
LOG_FORMAT = "[%(asctime)s] %(filename)s:%(lineno)d %(message)s"

server_vm_test_dump = "http://victoriametrics.tail696b12.ts.net:8427"
server_vm_d65 = "http://victoriametrics.tail696b12.ts.net:8428"
server_vm_sltms = "http://victoriametrics.tail696b12.ts.net:8429"
server_vm_gh = "http://victoriametrics.tail696b12.ts.net:8430"
server_vm_b3sr = "http://victoriametrics.tail696b12.ts.net:8431"
server_vm_clayton = "http://victoriametrics.tail696b12.ts.net:8433"
server_vm_localhost = "http://localhost:8428"

vmapi_delete_series = "/api/v1/admin/tsdb/delete_series"
vmapi_export = "/api/v1/export"
vmapi_export_csv = "/api/v1/export/csv"
vmapi_export_native = "/api/v1/export/native"
vmapi_import = "/api/v1/import"
vmapi_import_csv = "/api/v1/import/csv"
vmapi_import_native = "/api/v1/import/native"
vmapi_import_prometheus = "/api/v1/import/prometheus"
vmapi_labels = "/api/v1/labels"
vmapi_label = "/api/v1/label/"
vmapi_query = "/api/v1/query"
vmapi_query_range = "/api/v1/query_range"
vmapi_series = "/api/v1/series"
vmapi_status_tsdb = "/api/v1/status/tsdb"
vmapi_datadog = "/datadog"
vmapi_datadog_v1_series = "/datadog/api/v1/series"
vmapi_datadog_v2_series = "/datadog/api/v2/series"
vmapi_federate = "/federate"
vmapi_graphite_find = "/graphite/metrics/find"
vmapi_influx_write = "/influx/write"
vmapi_resetRollupResultCache = "/internal/resetRollupResultCache"
