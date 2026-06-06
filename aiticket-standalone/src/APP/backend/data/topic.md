<!--
此文档用于设置项目的主题模块构成，在进行智能问题分析、报告分析、知识库检索、
需求规划和 PRD 写作时，应优先使用这里定义的主题树进行分类、聚合和关联。

规则：
1. 每个主题必须带 topic 标识，格式统一为 [TOP-...]
2. 分析时尽量命中末级主题；数量不够再向上聚合
3. KB、工单、应用与开发平台文档仓都应尽量映射到这些主题
4. topic 标识可直接作为快速索引键和知识关联键

多项目分区格式：
- 每个 Jira 项目的主题树用 ## [PROJECT:<KEY>] 作为 section 头
- TopicParser 按 project_key 参数选取对应分区；无分区头则整体视为 MYPROJECT（向后兼容）
- 新增项目在此文件末尾追加新分区即可
-->

# 主题

## 主题结构

- 使用多层列表表达主题树
- 每个节点统一格式：`- [TOP-标识] 主题名称`
- 缩进表示父子层级
- 主题下的说明文字用于提示该主题覆盖的内容边界

## [PROJECT:MYPROJECT]

- [TOP-WF] 工作流
    工作流产品域主题，覆盖流程中心的设计、运行、监控、扩展和周边集成能力。
    - [TOP-WF.ENGINE] 流程引擎
        流程底层模型和核心执行能力，基于 BPMN / Flowable 一类模型。
        - [TOP-WF.ENGINE.RUNTIME] 流转
        - [TOP-WF.ENGINE.CALLBACK] 回调
        - [TOP-WF.ENGINE.STATE] 流程状态
    - [TOP-WF.FIELD] 字段权限
    - [TOP-WF.DEF] 关键功能定义
    - [TOP-WF.MSG] 消息模板
    - [TOP-WF.DESIGNER] 工作流设计器
        - [TOP-WF.DESIGNER.PEOPLE] 选人组件
        - [TOP-WF.DESIGNER.TIMEOUT] 时限
        - [TOP-WF.DESIGNER.CC] 抄送
        - [TOP-WF.DESIGNER.BO] 业务对象组件
        - [TOP-WF.DESIGNER.SCRIPT] 脚本环节
        - [TOP-WF.DESIGNER.COUNTERSIGN] 汇签环节
        - [TOP-WF.DESIGNER.SHARED] 共享环节
        - [TOP-WF.DESIGNER.BATCH] 批量设置
        - [TOP-WF.DESIGNER.GLOBAL] 全局属性
        - [TOP-WF.DESIGNER.BRANCH] 分支条件
        - [TOP-WF.DESIGNER.ADDSIGN] 前后加签
    - [TOP-WF.PREDICT] 流程预测
        - [TOP-WF.PREDICT.FUTURE] 未来审批流
        - [TOP-WF.PREDICT.SIM] 流程仿真
    - [TOP-WF.ANALYTICS] 效能分析
    - [TOP-WF.CONTROL] 分级管控
    - [TOP-WF.MONITOR] 流程监控
        - [TOP-WF.MONITOR.INTERVENE] 流程干预
        - [TOP-WF.MONITOR.EXCEPTION] 异常监控
        - [TOP-WF.MONITOR.LOG] 流程日志
            - [TOP-WF.MONITOR.LOG.ALL] 全部日志
            - [TOP-WF.MONITOR.LOG.PARTICIPANT] 参与人日志
            - [TOP-WF.MONITOR.LOG.INTERVENE] 干预日志
            - [TOP-WF.MONITOR.LOG.MESSAGE] 消息日志
            - [TOP-WF.MONITOR.LOG.NODE] 环节日志
            - [TOP-WF.MONITOR.LOG.HANDLE] 处理日志
            - [TOP-WF.MONITOR.LOG.START] 发起日志
    - [TOP-WF.PATH] 流程路径表与环节模板
    - [TOP-WF.MATRIX] 审批矩阵
    - [TOP-WF.AGENT] 代理人设置

- [TOP-WF.UPSTREAM] 工作流上游业务
    工作流依赖的上游平台能力和基础技术。
    - [TOP-WF.UPSTREAM.UITPL] UI模板
    - [TOP-WF.UPSTREAM.FRONTEND] 前端框架
    - [TOP-WF.UPSTREAM.META] 业务对象与元数据
    - [TOP-WF.UPSTREAM.BUILDER] 应用构建
    - [TOP-WF.UPSTREAM.ORG] 组织管理
    - [TOP-WF.UPSTREAM.AUTH] 权限管理
	- 数据权限
	- 特殊数据权限
	- 字段权限
	- 敏感数据权限
	- 自动授权
	- 管理员授权
	- 角色管理
	- 用户管理
	- 授权
	- 全员应用授权
	- 权限申请单
	- 按钮权限

- [TOP-WF.PARALLEL] 工作流平行业务
    与工作流并行配合的通用业务能力。
    - [TOP-WF.PARALLEL.MSG] 消息模板
    - [TOP-WF.PARALLEL.ACTIVITY] 业务活动
    - [TOP-WF.PARALLEL.I18N] 全球化多语

- [TOP-WF.DOWNSTREAM] 工作流下游业务
    工作流流转完成后承接或消费流程结果的业务能力。
    - [TOP-WF.DOWNSTREAM.BILL] 单据
    - [TOP-WF.DOWNSTREAM.MESSAGE] 消息中心

- [TOP-APCOM] 应用与开发平台
    面向 `crosswarm-apcom-docs` 文档仓的总主题，覆盖平台公共、开发框架、应用支撑、应用构建、档案和应用。

    - [TOP-APCOM.PUBLIC] 平台公共
        跨团队共享的通用文档、规范、手册和演进记录。
        - [TOP-APCOM.PUBLIC.RELEASE] 发布日志
        - [TOP-APCOM.PUBLIC.SPEC] 开发规范
        - [TOP-APCOM.PUBLIC.SHARING] 技术分享
        - [TOP-APCOM.PUBLIC.MANUAL] 应用与需求使用手册
        - [TOP-APCOM.PUBLIC.ARCH] 架构优化

    - [TOP-APCOM.FRAMEWORK] 开发框架
        平台基础开发框架与技术栈。
        - [TOP-APCOM.FRAMEWORK.META] 元数据
            - [TOP-APCOM.FRAMEWORK.META.PRODUCT] 产品概述
            - [TOP-APCOM.FRAMEWORK.META.GUIDE] 操作指南
            - [TOP-APCOM.FRAMEWORK.META.FAQ] 常见问题
            - [TOP-APCOM.FRAMEWORK.META.API] 接口设计
            - [TOP-APCOM.FRAMEWORK.META.TRAINING] 赋能和培训
            - [TOP-APCOM.FRAMEWORK.META.REQ] 应用与需求
            - [TOP-APCOM.FRAMEWORK.META.DESIGN] 设计方案
            - [TOP-APCOM.FRAMEWORK.META.ISSUE] 问题分析
            - [TOP-APCOM.FRAMEWORK.META.JIRA] JIRA工单
        - [TOP-APCOM.FRAMEWORK.FRONTEND] 前端框架
            - [TOP-APCOM.FRAMEWORK.FRONTEND.MDF] MDF
            - [TOP-APCOM.FRAMEWORK.FRONTEND.YNF] YNF
            - [TOP-APCOM.FRAMEWORK.FRONTEND.UITPL] UI模板
        - [TOP-APCOM.FRAMEWORK.BACKEND] 后端框架
            - [TOP-APCOM.FRAMEWORK.BACKEND.MDD] MDD
            - [TOP-APCOM.FRAMEWORK.BACKEND.YPD] YPD
            - [TOP-APCOM.FRAMEWORK.BACKEND.PUBLIC] 公共能力

    - [TOP-APCOM.SUPPORT] 应用支撑
        平台提供的通用中台与底座能力。
        - [TOP-APCOM.SUPPORT.BIZFLOW] 业务流
        - [TOP-APCOM.SUPPORT.FORMULA] 公式
        - [TOP-APCOM.SUPPORT.FUNC] 函数引擎
        - [TOP-APCOM.SUPPORT.DELETECHECK] 删除引用校验
        - [TOP-APCOM.SUPPORT.I18N] 国际化
        - [TOP-APCOM.SUPPORT.IMPEXP] 导入导出
        - [TOP-APCOM.SUPPORT.MIGRATION] 开发迁移
        - [TOP-APCOM.SUPPORT.PRINT] 打印
        - [TOP-APCOM.SUPPORT.LOG] 日志
        - [TOP-APCOM.SUPPORT.MESSAGE] 消息
        - [TOP-APCOM.SUPPORT.CODING] 编码规则
        - [TOP-APCOM.SUPPORT.RULE] 规则引擎
        - [TOP-APCOM.SUPPORT.SCHED] 调度
        - [TOP-APCOM.SUPPORT.CONFIG] 配置迁移
        - [TOP-APCOM.SUPPORT.REQ] 应用与需求
        - [TOP-APCOM.SUPPORT.API] 接入与接口
        - [TOP-APCOM.SUPPORT.ARCH] 技术架构与扩展开发
        - [TOP-APCOM.SUPPORT.FAQ] 常见问题
        - [TOP-APCOM.SUPPORT.TRAINING] 赋能和培训
        - [TOP-APCOM.SUPPORT.PRACTICE] 方案和最佳实践
        - [TOP-APCOM.SUPPORT.JIRA] JIRA工单

    - [TOP-APCOM.BUILD] 应用构建
        面向应用创建和交付的构建能力。
        - [TOP-APCOM.BUILD.PUBLIC] 公共能力
        - [TOP-APCOM.BUILD.ZERO] 零代码
        - [TOP-APCOM.BUILD.LOW] 低代码
        - [TOP-APCOM.BUILD.PRO] 专业开发
        - [TOP-APCOM.BUILD.PRODUCT] 产品文档
        - [TOP-APCOM.BUILD.API] 接入与接口
        - [TOP-APCOM.BUILD.ARCH] 技术架构与扩展开发
        - [TOP-APCOM.BUILD.PRACTICE] 方案和最佳实践
        - [TOP-APCOM.BUILD.TEAM] 团队日常工作
        - [TOP-APCOM.BUILD.REQ] 应用与需求
        - [TOP-APCOM.BUILD.TRAINING] 帮助与培训

    - [TOP-APCOM.APPS] 档案和应用
        平台面向业务域的产品模块集合。
        - [TOP-APCOM.APPS.MDM] 主数据
        - [TOP-APCOM.APPS.BASIC] 基础档案
        - [TOP-APCOM.APPS.WORKBENCH] 工作台
        - [TOP-APCOM.APPS.AUTH] 权限
        - [TOP-APCOM.APPS.CORE] 核心档案
        - [TOP-APCOM.APPS.FLOW] 流程
        - [TOP-APCOM.APPS.ORG] 组织
        - [TOP-APCOM.APPS.INTERNAL] 内部文档
        - [TOP-APCOM.APPS.PRODUCT] 产品概述
        - [TOP-APCOM.APPS.ARCH] 技术架构与扩展开发
        - [TOP-APCOM.APPS.PRACTICE] 方案和最佳实践
        - [TOP-APCOM.APPS.FAQ] 常见问题
        - [TOP-APCOM.APPS.TRAINING] 赋能和培训
        - [TOP-APCOM.APPS.JIRA] JIRA工单
        - [TOP-APCOM.APPS.REQ] 需求设计与应用需求
        - [TOP-APCOM.APPS.TEST] 测试相关

- [TOP-CROSS] 跨主题关联
    用于连接工作流与应用与开发平台之间的交叉能力。
    - [TOP-CROSS.WF_PLATFORM] 工作流与平台底座
    - [TOP-CROSS.WF_FRAMEWORK] 工作流与开发框架
    - [TOP-CROSS.WF_SUPPORT] 工作流与应用支撑
    - [TOP-CROSS.WF_APPS] 工作流与档案和应用
    - [TOP-CROSS.REQ] 需求设计与 PRD 写作
