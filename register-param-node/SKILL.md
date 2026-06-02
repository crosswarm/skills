---
name: register-param-node
description: 生成并执行参数节点注册 SQL。用于用户粘贴参数节点注册表行、示例字段内容，或用自然语言描述要注册的参数节点时，自动补全服务信息，生成 iuap_apcom_auth.pub_param_group 与 pub_option_group 两条 INSERT，并在参数库执行后返回结果；发布分享时通过环境变量或命令参数提供数据库连接信息。
---

# 参数节点注册

用于把用户提供的参数节点注册信息转换为两条注册 SQL，并按需执行到参数库。此 skill 是可分发版本，不包含数据库密码；运行环境必须通过环境变量或脚本参数提供连接信息。

## 工作流

1. 读取用户输入，优先识别制表符表格行；如果用户按字段或自然语言描述，先整理成字段 JSON。
2. 至少确认有 `参数节点` 和 `服务编码`。
3. 运行 `scripts/register_param_node.py`：
   - 用户粘贴表格行时，用 `--input-file`。
   - 用户按字段描述时，用 `--fields-json`，字段名使用中文表头。
   - 用户要求正式注册时，加 `--execute`；只想看 SQL 时不加。
4. 将脚本输出的两条 SQL、服务库补全结果、参数库执行结果返回给用户。
5. 如果脚本返回 `missing_fields`、`missing_db_config`、`service_lookup_error`、`duplicate_rows` 或数据库错误，提示用户补充或修正对应信息，不要编造缺失字段。

## 数据库配置

服务库用于补全应用编码、多语资源、URL、domain、微服务等信息。参数库用于执行两条 INSERT。

优先使用环境变量：

```bash
export PARAM_NODE_SERVICE_DB_HOST="dbproxy.diwork.com"
export PARAM_NODE_SERVICE_DB_PORT="12999"
export PARAM_NODE_SERVICE_DB_USER="iuap_benchservice"
export PARAM_NODE_SERVICE_DB_PASSWORD="..."
export PARAM_NODE_SERVICE_DB_NAME="iuap_apcom_benchservice"

export PARAM_NODE_AUTH_DB_HOST="dbproxy.diwork.com"
export PARAM_NODE_AUTH_DB_PORT="12368"
export PARAM_NODE_AUTH_DB_USER="iuap_apauth"
export PARAM_NODE_AUTH_DB_PASSWORD="..."
export PARAM_NODE_AUTH_DB_NAME="iuap_apcom_auth"
```

也可以在运行脚本时传入 `--service-db-password`、`--auth-db-password` 等参数。不要把密码写进 skill 文件。

## 命令模板

表格行输入：

```bash
python3 scripts/register_param_node.py --input-file /path/to/input.txt --execute
```

字段 JSON 输入：

```bash
python3 scripts/register_param_node.py \
  --fields-json '{"参数节点":"收入参数","服务编码":"ercl_parameters","应用编码":"RVN","参数类型":"组织级参数","关联微服务":"yonbip-fi-ercl"}' \
  --execute
```

只生成 SQL 不执行：

```bash
python3 scripts/register_param_node.py \
  --fields-json '{"参数节点":"收入参数","服务编码":"ercl_parameters","应用编码":"RVN","节点所属应用多语词条ID":"UID:...","关联微服务":"yonbip-fi-ercl","服务地址":"/meta/example"}'
```

## 输入字段

表格表头见 `references/parameter-node-registration-spec.md`。脚本会按文档中的表头顺序解析制表符行。

核心字段：

- `参数节点`：写入 `name`，也是服务编码为 URL 时查询服务库的 `service_name`。
- `服务编码`：非 URL 时按 `sys_service.service_code` 查询；URL 或路径时先按 `sys_service.service_name=参数节点` 查询，查不到时按 URL 本身补全注册。
- `应用编码`：可由服务库 `application_code` 补全，也可由用户直接提供。
- `节点所属应用多语词条ID`：可由服务库 `resid` 补全，也可由用户直接提供。
- `关联微服务`：可由服务库 `micro_service_code`、`domain_key` 等补全，也可由用户直接提供。
- `服务地址`：服务库查不到时可由用户补充；如果 `服务编码` 本身是 URL，则直接作为服务地址使用；如果以 `/` 开头，脚本自动拼接 `${domain.iuap-mdf-node}`。
- `注册编码`：可选。URL fallback 时优先使用；未提供时脚本从 URL 的 `busiObj` 或路径末段推导。
- `参数类型`：当前按文档示例支持 `组织级参数`，写入 `param_type=1`。
- `option_id 研发提供`：如果用户提供，写入 `pub_param_group.option_id`；否则为 `null`。

## 输出规则

- `pub_param_group.request_content` 优先使用服务库 `url`；服务编码是 URL 且服务库查不到时，使用该 URL 智能修正后的地址。
- `pub_param_group.group_code` 与两张表的 `code` 使用最终服务编码。
- `pub_param_group.domain` 优先使用服务库 `domain_key`，其次使用 URL 中的 `domainKey`，再其次使用微服务编码。
- `pub_option_group.parentcode` 固定为 `common_option_01`。
- `pub_option_group.optionid` 固定为 `common_option`。
- `pub_option_group.systemcode` 固定为 `U8C3`。
- `creator` 默认 `testqx`，可通过脚本参数 `--creator` 覆盖。

## 错误处理

- 服务编码是 URL 时，先按参数节点名称查询服务库；查到则使用服务库信息，查不到则按 URL 本身注册，并从 `domainKey`、`${domain.xxx}`、`busiObj`、路径末段推导缺省值。
- URL fallback 时，如果 URL 和用户字段都无法确定 `关联微服务/domain`，要求用户补充 `关联微服务` 或带 `domainKey` 的 URL。
- 非 URL 服务编码查不到且用户未显式提供 `应用编码`、`节点所属应用多语词条ID`、`关联微服务`、服务地址时，要求用户补充或修正服务编码/参数节点。
- 参数库中同一 `code` 与租户 `0` 已存在时，不重复插入；把已存在表名和主键返回给用户。
- 数据库连接、账号、密码或权限失败时，提示用户确认环境变量或命令参数是否正确。

## 资源

- `scripts/register_param_node.py`：解析、补全、生成 SQL、执行参数库写入。
- `references/parameter-node-registration-spec.md`：字段、库信息和示例 SQL 规则。
