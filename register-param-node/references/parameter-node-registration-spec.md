# 参数节点注册规范

## 示例输入

```text
财务云	财务会计	冯奎	收入参数	冯奎	ercl_parameters	收入管理		RVN	组织级参数	yonbip-fi-ercl																	保留不下线
```

## 表结构

```csv
领域云,子领域,子领域对接人,参数节点,负责人,服务编码,节点所属应用,节点所属应用多语词条ID,应用编码,参数类型,关联微服务,"option_id
研发提供","二方包参数
研发提供",组织级参照类型,组织参照编码,是否框架,配置迁移,框架改造,地址改造,"确认产品形态
包括所属领域云、子领域、服务编码、微服务编码、参数类型等","录入验证故事/bug
（填写单号）","标准化改造
去自定义树","标准化改造
使用标准组织参照",UE规范改造,"接入平台时间
（不接入填写不确认并填写最后一列备注）",所有适配任务完成时间,"原参数节点下线时间计划
（不确定留空，不下线写保留不下线）",确认人,"备注
（特殊问题、不接入理由等）"
```

## 服务补全规则

如果 `服务编码` 不是 URL 地址，按服务编码查询：

```sql
select * from iuap_apcom_benchservice.sys_service where service_code='ercl_parameters';
```

如果 `服务编码` 是 URL 地址，按参数节点名称查询：

```sql
select * from iuap_apcom_benchservice.sys_service where service_name='销项发票管理参数';
```

如果按参数节点名称查询到了服务库记录，按查询到的 `application_code`、`resid`、`url`、`domain_key`、`micro_service_code` 等字段注册。

如果没查到，直接按 `服务编码` 中的 URL 进行智能补全修正后注册：

- `request_content` 使用该 URL；如果 URL 以 `/` 开头，补成 `${domain.iuap-mdf-node}<URL>`。
- `domain` 和 `micro_service_code` 优先从 URL 查询参数 `domainKey` 推导，其次从 `${domain.xxx}` 或用户提供的 `关联微服务` 推导。
- `code/group_code` 优先使用用户提供的 `注册编码`，其次从 URL 查询参数 `busiObj` 推导，再其次用 URL 路径末段清洗后推导。
- `application_code`、`name_resid` 如果服务库查不到且用户未提供，可写入 `null`，但需要在结果里提示用户未补全。

从服务库读取 `application_code`、`resid`、`url`、`domain_key`、`micro_service_code` 等字段。

## 连接信息

发布版 skill 不保存密码。按原文档配置连接参数时，使用环境变量或脚本参数提供。

服务编码查询库：

```text
host: dbproxy.diwork.com
port: 12999
database: iuap_apcom_benchservice
user: iuap_benchservice
```

参数库：

```text
host: dbproxy.diwork.com
port: 12368
database: iuap_apcom_auth
user: iuap_apauth
```

## 示例表1

```sql
INSERT INTO iuap_apcom_auth.pub_param_group (id, code, name, name_resid, application_code, micro_service_code, request_content, request_type, external_data, param_type, org_reference, group_code, is_domain_param, domain, option_id, creator, creator_time, modifier, modify_time, pubts, ytenant_id, dr) VALUES ('2360166451453624388', 'ercl_parameters', '收入参数', 'UID:P_YONBIP-FI-ERCL-WB_217862DA05A00055', 'RVN', 'yonbip-fi-ercl', '${domain.iuap-mdf-node}/meta/VoucherList/rclParametersNewList?domainKey=yonbip-fi-ercl&busiObj=rclParameters', 1, null, 1, null, 'ercl_parameters', 1, 'yonbip-fi-ercl', null, 'testqx', '2026-03-20 15:51:28', null, null, '2026-03-20 15:51:30', '0', 0)
```

## 示例表2

```sql
INSERT INTO iuap_apcom_auth.pub_option_group (id, code, name, ordernum, parentcode, pubts, ideleted, datasourcename, image, controltype, align, ismain, optionid, iCols, industrytype, cStyle, systemcode, name_resid, ytenant_id, micro_service_code) VALUES (2417028631585357894, 'ercl_parameters', '收入参数', 120, 'common_option_01', '2026-03-20 15:52:48', 0, null, null, null, null, 1, 'common_option', 0, null, null, 'U8C3', 'UID:P_YONBIP-FI-ERCL-WB_217862DA05A00055', '0', 'yonbip-fi-ercl')
```
