---
name: feishu-table-monitor
description: "Monitor a Feishu Bitable (飞书多维表格) for recently added or updated records, support conversational parameter selection by name or code, and export selected parameters to Excel matching the parameter database template format. Zero-config for the default shared table. Trigger keywords: 飞书表格, 多维表格, 监控, 新增, 更新, 导出Excel, bitable, feishu monitor."
agent_created: true
---

# Feishu Table Monitor (飞书表格监控)

## Overview

Monitor a Feishu Bitable (飞书多维表格) for data changes within a configurable time window (default: 7 days). Present newly added and recently updated records conversationally for the user to select, then export the chosen records to an Excel file matching the parameter database template format (transposed: one column per parameter, one row per metadata field).

**Zero configuration** — the default shared bitable credentials are built into the script. Users can start monitoring immediately without any setup.

## Quick Start (No Setup Required)

The default shared bitable is pre-configured. Simply trigger the skill and it works out of the box:

```
# Fetch recent changes (no args needed)
python3 scripts/feishu_monitor.py fetch

# Export specific parameters
python3 scripts/feishu_monitor.py export --params "CODE1,CODE2"
```

No Feishu connector, no App ID, no App Secret required for the default table.

## Using a Different Table (Override)

To monitor a different bitable, provide your own credentials:

1. **Via CLI args**: `--app-id`, `--app-secret`, `--app-token`, `--table-id`, or `--table-url`
2. **Via env vars**: `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_APP_TOKEN`, `FEISHU_TABLE_ID`
3. **Via Feishu connector**: Connect the Feishu connector in WorkBuddy settings

Priority: CLI args > env vars > built-in defaults.

## Default Table Configuration

The shared default table (used unless user specifies otherwise):

- **URL**: `https://yyhubble.feishu.cn/wiki/I92awJKKJiV5efkYLmQctyHsn4e?table=tbl7w2cvN5ZkuA7u&view=vew54wfNwm`
- **App Token**: `I92awJKKJiV5efkYLmQctyHsn4e`
- **Table ID**: `tbl7w2cvN5ZkuA7u`
- **Access**: 全员可见 (publicly visible to all organization members)
- **Parameter code field**: `参数申请编码` (primary identifier for each parameter)
- **Parameter name field**: `参数名称`
- **Date fields**: Look for `申请时间` or `创建时间` for time-based filtering

## Auth Method Priority

1. **Built-in defaults** — The script ships with pre-configured credentials for the default shared table. Zero setup needed.
2. **CLI args / env vars** — Override defaults for a different table or app credentials.
3. **Feishu connector** — If `feishu` connector is connected, use its MCP tools for bitable operations.

## Workflow

### Step 1: Check Auth & Table

- **Default table**: No auth needed — built-in credentials work out of the box.
- **Custom table**: If the user provides a different URL, check for CLI args / env vars / Feishu connector.
- **Time range**: Default to 7 days. Ask only if the user specifies otherwise.

### Step 2: Fetch Table Data

**If Feishu connector is connected**: Use its MCP tools to list fields and records. Filter by `created_time` / `last_modified_time` client-side within Python.

**Using the script** (default table — no credentials needed):

```bash
python3 scripts/feishu_monitor.py fetch --days 7
```

The fetch process:
- Authenticates via Feishu Open API
- Lists all table fields with names and types
- Fetches all records with pagination
- Filters by `created_time` and `last_modified_time` within the specified range
- Prints a summary: total fields, new records count, updated records count
- Shows each changed record with identifying fields (parameter name + code)

### Step 3: Present Results to User

After fetching, present a clear conversational summary:

- Total records in scope (new + updated)
- Each record with parameter name, code, last modified time
- Mark which are **新增** and which are **更新**

Example:
```
📊 飞书表格监控报告 (近7天)
- 新增 3 条，更新 2 条

📌 新增:
  1. 是否调用利润中心规则服务 | Y_FCC_DPMACCT_OPROFITCENTER10 | 2025-12-29 11:30
  ...

✏️ 更新:
  1. 单据保存时利润中心更新规则 | Y_FCC_DPMACCT_OPROFITCENTER20 | 2025-12-29 14:05
  ...
```

### Step 4: Conversational Selection

Let the user select parameters through natural conversation. Accept:

- **Parameter codes**: "导出 Y_FCC_DPMACCT_OCOSTCENTER10"
- **Parameter names**: "导出成本中心与部门关系"
- **Index numbers**: "导出第 1、3、5 条"
- **Ranges**: "导出所有新增的"
- **Combinations**: "导出所有更新记录中属于事项会计中台的"

If the user says "全部" or "所有", export all records in the current time range.

Collect selections into a comma-separated list of parameter codes. Map names to codes using the fetched data.

### Step 5: Export to Excel

**If using Feishu connector data**: Import the script's export function directly:

```python
# Use the managed Python env with openpyxl installed
import sys; sys.path.insert(0, '.workbuddy/skills/feishu-table-monitor/scripts')
from feishu_monitor import export_to_excel
export_to_excel(records, fields, ['CODE1', 'CODE2'], 'output.xlsx')
```

**Using the script** (default table — no credentials needed):

```bash
python3 scripts/feishu_monitor.py export \
  --params "CODE1,CODE2" \
  --output "参数导出结果_YYYYMMDD.xlsx"
```

The Excel output matches the parameter database template format:
- **Transposed layout**: each parameter = one column, metadata fields = rows
- **Column A**: field labels (44 metadata fields)
- **Row 1**: parameter names as headers
- **Styles**: 微软雅黑 font, #D9E2F3 header fill, #F2F2F2 label fill, thin borders
- **Freeze panes**: B2 (fixed label column + header row)

See `references/excel_template.md` for the full field list and style specs.

### Step 6: Deliver Result

After generation:
- Confirm output path and file size
- Use `deliver_attachments` to send the Excel file
- Present a summary of exported parameters

## Error Handling

| Scenario | Action |
|----------|--------|
| Default table, no user auth | Works out of the box — built-in credentials handle everything. |
| Custom table, no override credentials | Ask user to provide `--app-id`/`--app-secret` or set env vars. |
| Feishu auth fails | Verify app_id/app_secret and `bitable:app` permission. |
| No records in time range | "近 N 天内没有新增或更新的记录。是否需要扩大时间范围？" |
| Table URL parsing fails | Fall back to default table. |
| Selected codes not found | List available codes/names from fetched data, ask user to pick. |
| openpyxl not installed | Install in managed venv: `pip install openpyxl` |

## Bundled Resources

### `scripts/feishu_monitor.py`
Main automation script with two commands: `fetch` (discover + query) and `export` (generate Excel). See `--help` for full CLI reference. The `export_to_excel()` function can also be imported directly for use with connector-fetched data.

### `references/excel_template.md`
Detailed spec of the Excel output format: all 44 metadata field labels, column structure, cell styles, and layout rules.

### `assets/template.xlsx`
Original parameter database Excel template for visual reference. Read-only — the export script generates new files based on this format.

## Usage Examples

**Default table (zero config):**
- "帮我看看这个飞书表格最近有什么变化"
- Fetch 7 days from default table → present → select → export

**Custom table:**
- "监控 https://xxx.feishu.cn/wiki/ABC?table=xyz 近30天的变化"
- Use provided URL → fetch 30 days → present → select → export

**Direct export by code:**
- "导出 Y_FCC_DPMACCT_OCOSTCENTER10"
- Skip fetch summary → export directly

**Bulk export:**
- "把所有新增的都导出来"
- Fetch first → collect all "new" codes → export all
