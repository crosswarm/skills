"""
PM 协作任务数据模型
用于 PM 系统集成和智能协作任务管理
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
import uuid


class DemandStatus(str, Enum):
    """PM需求状态枚举"""
    WAIT_ANALYSIS = "WAIT_ANALYSIS"  # 待分析
    COO_ACCEPT = "COO_ACCEPT"  # 协作方已采纳
    COO_HANG = "COO_HANG"  # 协作方挂起
    ANALYZING = "ANALYZING"  # 分析中
    COMPLETED = "COMPLETED"  # 已完成
    CLOSED = "CLOSED"  # 已关闭


class DemandStatusLabel(str, Enum):
    """PM需求状态中文标签"""
    WAIT_ANALYSIS = "待分析"
    COO_ACCEPT = "协作方已采纳"
    COO_HANG = "协作方挂起"
    ANALYZING = "分析中"
    COMPLETED = "已完成"
    CLOSED = "已关闭"


# 状态映射
STATUS_LABEL_MAP = {
    DemandStatus.WAIT_ANALYSIS: DemandStatusLabel.WAIT_ANALYSIS,
    DemandStatus.COO_ACCEPT: DemandStatusLabel.COO_ACCEPT,
    DemandStatus.COO_HANG: DemandStatusLabel.COO_HANG,
    DemandStatus.ANALYZING: DemandStatusLabel.ANALYZING,
    DemandStatus.COMPLETED: DemandStatusLabel.COMPLETED,
    DemandStatus.CLOSED: DemandStatusLabel.CLOSED,
}


class UserInfo(BaseModel):
    """用户信息模型"""
    aid: str = Field(..., description="用户ID")
    name: str = Field(..., description="用户姓名")
    code: str = Field(..., description="用户编码")
    email: Optional[str] = Field(None, description="邮箱")
    org: Optional[str] = Field(None, description="组织")
    dept: Optional[str] = Field(None, description="部门")
    dept_id: Optional[str] = Field(None, description="部门ID")


class PMDemand(BaseModel):
    """PM需求数据模型"""
    aid: str = Field(..., description="需求唯一ID")
    code: str = Field(..., description="需求编码")
    title: str = Field(..., description="需求标题")
    status: DemandStatus = Field(..., description="当前状态")
    analyst: Optional[UserInfo] = Field(None, description="分析人员")
    cor_proposer: UserInfo = Field(..., description="协作提出人")
    product_id: Optional[str] = Field(None, description="产品ID")
    category_id: Optional[str] = Field(None, description="分类ID")
    create_time: datetime = Field(..., description="创建时间")
    update_time: Optional[datetime] = Field(None, description="更新时间")
    expected_resolve_time: Optional[datetime] = Field(None, description="预计解决时间")
    commit_delivery_time: Optional[datetime] = Field(None, description="承诺交付时间")
    close_time: Optional[datetime] = Field(None, description="关闭时间")

    # 扩展字段
    description: Optional[str] = Field(None, description="需求描述")
    priority: Optional[str] = Field(None, description="优先级")
    tags: List[str] = Field(default_factory=list, description="标签")

    # 本地处理字段
    processed_at: Optional[datetime] = Field(None, description="本地处理时间")
    processed_action: Optional[str] = Field(None, description="处理动作: accept/reject/manual")
    processed_by: Optional[str] = Field(None, description="处理人（自动处理时为system）")
    predefine_id: Optional[str] = Field(None, description="匹配的预定义ID")
    reject_reason: Optional[str] = Field(None, description="拒绝原因")

    @property
    def status_label(self) -> str:
        """获取状态中文标签"""
        return STATUS_LABEL_MAP.get(self.status, "未知")

    @property
    def waiting_days(self) -> int:
        """计算等待天数（排除周末）"""
        start_date = self.create_time.date()
        today = datetime.now().date()
        days = 0

        current = start_date
        while current < today:
            if current.weekday() < 5:  # 周一到周五
                days += 1
            current += timedelta(days=1)

        return days

    @property
    def is_overdue(self) -> bool:
        """是否超过2个工作日"""
        return self.waiting_days > 2

    @property
    def is_urgent(self) -> bool:
        """是否紧急（24小时内到期）"""
        if not self.commit_delivery_time:
            return False
        time_left = self.commit_delivery_time - datetime.now()
        return time_left.total_seconds() < 24 * 3600

    @property
    def proposer_display(self) -> str:
        """提出人显示文本"""
        name = self.cor_proposer.name if self.cor_proposer else "未知"
        dept = self.cor_proposer.dept if self.cor_proposer else ""
        return f"{name}/{dept}" if dept else name

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于缓存）"""
        return self.model_dump()

    @classmethod
    def from_api_response(cls, data: Dict[str, Any]) -> "PMDemand":
        """从API响应创建实例"""
        # 解析用户信息
        def parse_user(user_data: Any) -> Optional[UserInfo]:
            if not user_data:
                return None
            if isinstance(user_data, dict):
                return UserInfo(**user_data)
            # 如果是字符串ID，需要后续查询
            return None

        # 解析时间
        def parse_time(time_val: Any) -> Optional[datetime]:
            if not time_val:
                return None
            if isinstance(time_val, datetime):
                return time_val
            if isinstance(time_val, str):
                # 尝试多种格式
                formats = [
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%fZ",
                    "%Y-%m-%d",
                ]
                for fmt in formats:
                    try:
                        return datetime.strptime(time_val, fmt)
                    except ValueError:
                        continue
            return None

        return cls(
            aid=data.get("aid", ""),
            code=data.get("code", ""),
            title=data.get("title", ""),
            status=DemandStatus(data.get("status", "WAIT_ANALYSIS")),
            analyst=parse_user(data.get("analyst")),
            cor_proposer=parse_user(data.get("corProposer")) or UserInfo(
                aid="", name="未知", code=""
            ),
            product_id=data.get("productId"),
            category_id=data.get("categoryId"),
            create_time=parse_time(data.get("ctime")) or datetime.now(),
            update_time=parse_time(data.get("mtime")),
            expected_resolve_time=parse_time(data.get("expectedResolveTime")),
            commit_delivery_time=parse_time(data.get("commitDeliveryTime")),
            close_time=parse_time(data.get("closeTime")),
            description=data.get("description"),
            priority=data.get("priority"),
            tags=data.get("tags", []),
        )


class PredefineStatus(str, Enum):
    """预定义状态"""
    ACTIVE = "active"  # 有效
    EXPIRED = "expired"  # 已过期
    DISABLED = "disabled"  # 已禁用


class PredefineData(BaseModel):
    """预定义协作数据模型"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="预定义ID")
    proposer_name: str = Field(..., description="协作提出人名称")
    proposer_domain: str = Field(..., description="所属领域/部门")
    expected_resolve_time: Optional[datetime] = Field(
        None, description="期望解决时间"
    )
    keywords: List[str] = Field(default_factory=list, description="匹配关键词")
    auto_accept: bool = Field(True, description="是否自动采纳")
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now() + timedelta(days=30),
        description="有效期至",
    )
    status: PredefineStatus = Field(PredefineStatus.ACTIVE, description="状态")
    description: Optional[str] = Field(None, description="备注说明")
    created_by: Optional[str] = Field(None, description="创建人")

    @property
    def is_active(self) -> bool:
        """是否有效"""
        if self.status != PredefineStatus.ACTIVE:
            return False
        return datetime.now() < self.expires_at

    @property
    def keywords_display(self) -> str:
        """关键词显示文本"""
        return ", ".join([f"[{k}]" for k in self.keywords])

    @property
    def display_text(self) -> str:
        """显示文本"""
        resolve_days = ""
        if self.expected_resolve_time:
            days = (self.expected_resolve_time - datetime.now()).days
            if days > 0:
                resolve_days = f" - 预计{days}天解决"

        keywords = self.keywords_display
        return f"{self.proposer_name}/{self.proposer_domain}{resolve_days} - 关键词: {keywords}"


class ProcessAction(str, Enum):
    """处理动作"""
    ACCEPT = "accept"  # 采纳
    REJECT = "reject"  # 拒绝
    MANUAL = "manual"  # 需人工处理
    SKIP = "skip"  # 跳过


class ProcessResult(BaseModel):
    """处理结果"""
    demand_id: str = Field(..., description="需求ID")
    action: ProcessAction = Field(..., description="处理动作")
    predefine_id: Optional[str] = Field(None, description="匹配的预定义ID")
    reason: str = Field("", description="处理原因/说明")
    processed_at: datetime = Field(default_factory=datetime.now, description="处理时间")
    success: bool = Field(True, description="是否成功")
    error_message: Optional[str] = Field(None, description="错误信息")


class PredefineMatch(BaseModel):
    """预定义匹配结果"""
    predefine: PredefineData = Field(..., description="匹配的预定义")
    score: float = Field(..., description="匹配度 (0-1)")
    matched_fields: List[str] = Field(default_factory=list, description="匹配的字段")


class PMBoardStats(BaseModel):
    """PM看板统计数据"""
    total: int = Field(0, description="总数")
    wait_analysis: int = Field(0, description="待分析")
    coo_accept: int = Field(0, description="已采纳")
    coo_hang: int = Field(0, description="已挂起")
    overdue: int = Field(0, description="已超时")
    processed_today: int = Field(0, description="今日处理")
    auto_processed_today: int = Field(0, description="今日自动处理")


class PMBoardResponse(BaseModel):
    """PM看板响应"""
    status: str = Field("success", description="状态")
    data: Dict[str, Any] = Field(default_factory=dict, description="数据")
    stats: PMBoardStats = Field(default_factory=PMBoardStats, description="统计")


class PredefineCreateRequest(BaseModel):
    """创建预定义请求"""
    proposer_name: str = Field(..., description="协作提出人名称")
    proposer_domain: str = Field(..., description="所属领域/部门")
    expected_resolve_days: Optional[int] = Field(
        None, description="期望解决天数（从当前时间计算）"
    )
    keywords: List[str] = Field(default_factory=list, description="匹配关键词")
    auto_accept: bool = Field(True, description="是否自动采纳")
    description: Optional[str] = Field(None, description="备注说明")


class AutoProcessStatus(BaseModel):
    """自动处理状态"""
    enabled: bool = Field(False, description="是否启用")
    running: bool = Field(False, description="是否运行中")
    last_run_at: Optional[datetime] = Field(None, description="上次运行时间")
    processed_count: int = Field(0, description="累计处理数量")
    today_processed: int = Field(0, description="今日处理数量")


class SyncResult(BaseModel):
    """同步结果"""
    status: str = Field("success", description="状态")
    synced_count: int = Field(0, description="同步数量")
    new_count: int = Field(0, description="新增数量")
    updated_count: int = Field(0, description="更新数量")
    message: str = Field("", description="消息")


# ===========================================================================
# 原始需求相关模型（feat-原始需求，2026-04-14）
# 与 PMModuleService 配合使用，不绑定 entityType
# ===========================================================================

class OriginalDemandRecord(BaseModel):
    """
    原始需求展平记录。
    字段来自 PMModuleService._flatten_record()，按需添加 Optional 字段。
    AI 分析字段本地存储，不回写到 PM 系统。
    """
    # --- PM 原生字段 ---
    aid: str = Field(..., description="需求唯一 ID")
    code: str = Field("", description="需求编码，如 YYY-PF-OR-803")
    title: str = Field("", description="需求标题")
    status: str = Field("", description="当前状态标签（展平后，如'待分析'）")
    status_raw: Optional[str] = Field(None, description="状态原始枚举值，如 WAIT_ANALYSIS")
    source: Optional[str] = Field(None, description="来源标签，如'支持问题'")
    source_raw: Optional[str] = Field(None, description="来源原始值")
    assignee: Optional[Dict[str, Any]] = Field(None, description="经办人完整对象")
    assignee_name: Optional[str] = Field(None, description="经办人姓名")
    description: Optional[str] = Field(None, description="需求描述")
    create_time: Optional[str] = Field(None, description="创建时间字符串")

    # --- AI 分析字段（本地产出）---
    ai_core_problem: Optional[str] = Field(None, description="核心问题一句话")
    ai_module: Optional[str] = Field(None, description="涉及模块")
    ai_product_layer: Optional[str] = Field(None, description="产品层级")
    ai_gap_analysis: Optional[str] = Field(None, description="缺口分析")
    ai_mvp_suggestion: Optional[str] = Field(None, description="MVP 建议")
    ai_scenario_keywords: Optional[List[str]] = Field(None, description="场景关键词")
    ai_value_score: Optional[int] = Field(None, ge=0, le=100, description="价值评分 0-100")
    ai_alternative_solution: Optional[str] = Field(None, description="可变相实现方案（空=无）")
    ai_triage_recommendation: Optional[str] = Field(
        None, description="Agent 分诊建议: auto_reject|auto_alternative|manual"
    )
    ai_triage_reason: Optional[str] = Field(None, description="分诊理由")
    ai_confidence: Optional[float] = Field(None, ge=0.0, le=1.0, description="模型置信度")
    ai_analyzed_at: Optional[datetime] = Field(None, description="AI 分析完成时间")

    # --- 分诊决策（人工确认/覆盖后）---
    triage_decision: Optional[str] = Field(
        None, description="最终分诊决策: auto_reject|auto_alternative|manual|pending"
    )
    triage_notified: bool = Field(False, description="是否已发出自动回复")
    human_override: bool = Field(False, description="人工是否覆盖了 AI 决策")
    human_override_by: Optional[str] = Field(None, description="覆盖操作人")
    human_override_at: Optional[datetime] = Field(None, description="覆盖时间")

    # --- 本地元数据 ---
    module_key: str = Field("original_demand", description="所属 PM 模块 key")
    local_updated_at: Optional[datetime] = Field(None, description="本地最后更新时间")

    class Config:
        extra = "allow"  # 允许接收未定义字段（PM 字段可能扩展）


class OriginalDemandTriageSummary(BaseModel):
    """分诊看板总结（进入 Tab 时展示）"""
    module_key: str = Field("original_demand", description="模块 key")
    module_label: str = Field("原始需求", description="模块标签")
    total: int = Field(0, description="总数")
    pending_triage: int = Field(0, description="待分诊（未分析）")
    auto_reject_count: int = Field(0, description="AI 建议自动拒绝")
    auto_alternative_count: int = Field(0, description="AI 建议变相实现")
    manual_count: int = Field(0, description="需人工处理")
    notified_count: int = Field(0, description="已发出自动回复")
    module_distribution: Dict[str, int] = Field(default_factory=dict, description="模块分布")
    theme_distribution: Dict[str, int] = Field(default_factory=dict, description="主题分布")
    value_buckets: Dict[str, int] = Field(default_factory=dict, description="价值分桶: high/mid/low")
    top_core_demands: List[Dict[str, Any]] = Field(default_factory=list, description="核心诉求 Top 5")
    generated_at: Optional[datetime] = Field(None, description="总结生成时间")


class TriageActionRequest(BaseModel):
    """前端触发分诊动作请求"""
    aid: str = Field(..., description="需求 ID")
    action: str = Field(..., description="操作: pm_action|ai_decide|human_override|execute_auto")
    pm_action: Optional[str] = Field(None, description="PM 原生操作: accept/reject/hang/...")
    pm_payload: Optional[Dict[str, Any]] = Field(None, description="PM 操作附加参数（如拒绝原因）")
    override_decision: Optional[str] = Field(None, description="人工覆盖的分诊决策")
    override_reason: Optional[str] = Field(None, description="覆盖原因")
