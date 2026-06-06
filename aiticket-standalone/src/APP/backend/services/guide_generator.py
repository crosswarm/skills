"""
引导生成引擎 - 整合场景识别、截图分析和标注生成
为应用操作类工单提供实时界面引导
"""

import json
import logging
import os
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime

from services.environment_detector import (
    EnvironmentDetector, EnvironmentInfo, detect_environment
)
from services.version_adapter import (
    VersionAdapterManager, GuideStep, get_version_adapter
)
try:
    from services.screenshot_analyzer import (
        ScreenshotAnalyzer, ScreenshotAnalysisResult, analyze_screenshot
    )
    _SCREENSHOT_ANALYZER_AVAILABLE = True
except ImportError:
    ScreenshotAnalyzer = None
    ScreenshotAnalysisResult = None
    analyze_screenshot = None
    _SCREENSHOT_ANALYZER_AVAILABLE = False
from services.guide_annotator import (
    GuideAnnotator, Annotation, AnnotationType, get_annotator
)

logger = logging.getLogger(__name__)

# 获取项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(BASE_DIR, "../../.."))


@dataclass
class GuideRequest:
    """引导请求"""
    issue_key: str
    issue_summary: str
    issue_description: str
    tenant_info: Dict[str, Any] = field(default_factory=dict)
    screenshots: List[bytes] = field(default_factory=list)
    user_question: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            'issue_key': self.issue_key,
            'issue_summary': self.issue_summary,
            'issue_description': self.issue_description,
            'tenant_info': self.tenant_info,
            'user_question': self.user_question
        }


@dataclass
class GuideResult:
    """引导结果"""
    request_id: str
    status: str  # success, partial, failed
    env_info: EnvironmentInfo
    matched_scenario: str = ""
    scenario_confidence: float = 0.0
    steps: List[Dict[str, Any]] = field(default_factory=list)
    annotated_images: List[Dict[str, Any]] = field(default_factory=list)
    text_guide: str = ""
    created_at: str = ""
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            'request_id': self.request_id,
            'status': self.status,
            'env_info': self.env_info.to_dict() if self.env_info else None,
            'matched_scenario': self.matched_scenario,
            'scenario_confidence': self.scenario_confidence,
            'steps': self.steps,
            'annotated_images': self.annotated_images,
            'text_guide': self.text_guide,
            'created_at': self.created_at,
            'error_message': self.error_message
        }


class GuideGenerator:
    """
    引导生成引擎

    整合多个服务：
    1. 环境识别：判断公有云/专属云/私有云
    2. 场景识别：匹配操作场景模板
    3. 截图分析：识别界面元素
    4. 标注生成：在截图上绘制引导
    """

    def __init__(self, llm_service=None):
        """初始化引导生成器"""
        self.llm_service = llm_service
        self.env_detector = EnvironmentDetector()
        self.version_adapter = VersionAdapterManager()
        self.screenshot_analyzer = ScreenshotAnalyzer(llm_service) if _SCREENSHOT_ANALYZER_AVAILABLE and ScreenshotAnalyzer else None
        self.guide_annotator = GuideAnnotator()

        # 加载场景模板
        self.scenario_templates = self._load_scenario_templates()

    def _load_scenario_templates(self) -> Dict[str, Any]:
        """加载场景模板"""
        templates_path = os.path.join(
            PROJECT_ROOT, "data", "guide_templates", "scenarios.json"
        )

        try:
            if os.path.exists(templates_path):
                with open(templates_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            else:
                logger.warning(f"⚠️ 场景模板文件不存在: {templates_path}")
                return {'templates': []}
        except Exception as e:
            logger.error(f"❌ 加载场景模板失败: {e}")
            return {'templates': []}

    def generate(self, request: GuideRequest) -> GuideResult:
        """
        生成操作引导

        Args:
            request: 引导请求

        Returns:
            GuideResult: 引导结果
        """
        request_id = f"guide_{datetime.now().strftime('%Y%m%d%H%M%S')}_{request.issue_key}"

        try:
            # 1. 环境识别
            env_info = self.env_detector.detect(request.tenant_info)
            logger.info(f"🔍 环境识别结果: {env_info.to_dict()}")

            # 2. 场景识别
            scenario_result = self._identify_scenario(request)
            matched_scenario = scenario_result.get('scenario_id', '')
            scenario_confidence = scenario_result.get('confidence', 0.0)

            # 3. 获取引导步骤
            if matched_scenario:
                steps = self._get_guide_steps(matched_scenario, env_info)
            else:
                steps = self._generate_adhoc_steps(request, env_info)

            # 4. 处理截图（如果有）
            annotated_images = []
            if request.screenshots:
                annotated_images = self._process_screenshots(
                    request.screenshots,
                    steps,
                    request.to_dict()
                )

            # 5. 生成文字引导
            text_guide = self._generate_text_guide(steps, matched_scenario)

            # 6. 构建结果
            status = 'success' if steps else 'partial'
            if not steps and not annotated_images:
                status = 'failed'

            return GuideResult(
                request_id=request_id,
                status=status,
                env_info=env_info,
                matched_scenario=matched_scenario,
                scenario_confidence=scenario_confidence,
                steps=[s.to_dict() if isinstance(s, GuideStep) else s for s in steps],
                annotated_images=annotated_images,
                text_guide=text_guide,
                created_at=datetime.now().isoformat()
            )

        except Exception as e:
            logger.error(f"❌ 引导生成失败: {e}")
            return GuideResult(
                request_id=request_id,
                status='failed',
                env_info=EnvironmentInfo(
                    env_type=self.env_detector.detect(request.tenant_info).env_type,
                    version='unknown',
                    access_method=self.env_detector.detect(request.tenant_info).access_method,
                    ui_rules_version='fallback'
                ),
                error_message=str(e),
                created_at=datetime.now().isoformat()
            )

    def _identify_scenario(self, request: GuideRequest) -> Dict[str, Any]:
        """
        识别操作场景

        通过关键词匹配和语义分析确定用户想要完成的操作
        """
        # 合并查询文本
        query_text = f"{request.issue_summary} {request.issue_description} {request.user_question}"

        best_match = None
        best_confidence = 0.0

        templates = self.scenario_templates.get('templates', [])

        for template in templates:
            # 关键词匹配
            keywords = template.get('keywords', [])
            keyword_score = self._calculate_keyword_match(query_text, keywords)

            if keyword_score > best_confidence:
                best_confidence = keyword_score
                best_match = template

        if best_match and best_confidence >= 0.3:
            return {
                'scenario_id': best_match.get('id', ''),
                'scenario_name': best_match.get('name', ''),
                'confidence': best_confidence,
                'module': best_match.get('module', ''),
                'template': best_match
            }

        return {'scenario_id': '', 'confidence': 0.0}

    def _calculate_keyword_match(self, text: str, keywords: List[str]) -> float:
        """计算关键词匹配分数"""
        text_lower = text.lower()
        match_count = 0

        for keyword in keywords:
            if keyword.lower() in text_lower:
                match_count += 1

        return match_count / len(keywords) if keywords else 0.0

    def _get_guide_steps(
        self,
        scenario_id: str,
        env_info: EnvironmentInfo
    ) -> List[GuideStep]:
        """
        获取引导步骤

        根据场景模板和版本适配生成步骤
        """
        # 查找场景模板
        template = None
        for t in self.scenario_templates.get('templates', []):
            if t.get('id') == scenario_id:
                template = t
                break

        if not template:
            return []

        # 获取原始步骤
        raw_steps = template.get('steps', [])

        # 转换为GuideStep对象
        steps = []
        for s in raw_steps:
            step = GuideStep(
                step=s.get('step', len(steps) + 1),
                action=s.get('action', 'click'),
                target=s.get('target', ''),
                tip=s.get('tip', ''),
                selector=s.get('selector')
            )
            steps.append(step)

        # 版本适配
        if env_info.ui_rules_version != 'latest':
            module = template.get('module', '')
            steps = self.version_adapter.adapt_steps(
                steps, env_info.ui_rules_version, module
            )

        return steps

    def _generate_adhoc_steps(
        self,
        request: GuideRequest,
        env_info: EnvironmentInfo
    ) -> List[GuideStep]:
        """
        动态生成引导步骤

        当没有匹配的场景模板时，尝试根据工单内容生成步骤
        """
        # 这里可以调用LLM动态生成步骤
        # 暂时返回基础步骤

        steps = [
            GuideStep(
                step=1,
                action='navigate',
                target='相关模块',
                tip=f'请进入相关功能模块'
            ),
            GuideStep(
                step=2,
                action='identify',
                target='操作目标',
                tip='请在界面上找到您要操作的目标'
            ),
            GuideStep(
                step=3,
                action='confirm',
                target='完成操作',
                tip='确认操作已完成'
            )
        ]

        return steps

    def _process_screenshots(
        self,
        screenshots: List[bytes],
        steps: List[GuideStep],
        issue_context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        处理截图，生成标注图片
        """
        results = []

        for i, screenshot in enumerate(screenshots):
            # 分析截图
            analysis = self.screenshot_analyzer.analyze(screenshot, issue_context)

            # 创建标注
            annotations = self._create_annotations_from_analysis(analysis, steps)

            # 生成标注图片
            annotated = self.guide_annotator.annotate(screenshot, annotations)

            results.append(annotated.to_dict())

        return results

    def _create_annotations_from_analysis(
        self,
        analysis: ScreenshotAnalysisResult,
        steps: List[GuideStep]
    ) -> List[Annotation]:
        """根据分析结果创建标注"""
        annotations = []

        # 为每个步骤创建标注
        for step in steps:
            if not step.target:
                continue

            # 在分析结果中查找对应元素
            element = self.screenshot_analyzer.find_target_element(
                analysis, step.target
            )

            if element:
                bounds = element.bounds

                # 创建矩形标注
                annotations.append(Annotation(
                    type=AnnotationType.RECTANGLE,
                    x=bounds.get('x', 0),
                    y=bounds.get('y', 0),
                    width=bounds.get('width', 100),
                    height=bounds.get('height', 30),
                    text=step.tip,
                    color='#FF0000',
                    order=step.step
                ))

        return annotations

    def _generate_text_guide(
        self,
        steps: List[GuideStep],
        scenario_name: str
    ) -> str:
        """生成文字引导"""
        if not steps:
            return "抱歉，无法识别您的操作场景。请提供更多详细信息或上传截图。"

        lines = [f"## 操作指南"]

        if scenario_name:
            lines.append(f"**场景**: {scenario_name}\n")

        lines.append("**操作步骤**:\n")

        for step in steps:
            tip = step.tip if isinstance(step, GuideStep) else step.get('tip', '')
            action = step.action if isinstance(step, GuideStep) else step.get('action', '')

            action_icon = {
                'click': '👆',
                'input': '⌨️',
                'select': '📋',
                'navigate': '🔍',
                'drag': '✋',
                'config': '⚙️'
            }.get(action, '➡️')

            lines.append(f"{step.step if isinstance(step, GuideStep) else step.get('step', 1)}. {action_icon} {tip}")

        return '\n'.join(lines)


# 全局实例
_generator: Optional[GuideGenerator] = None


def get_guide_generator(llm_service=None) -> GuideGenerator:
    """获取引导生成器单例"""
    global _generator
    if _generator is None:
        _generator = GuideGenerator(llm_service)
    return _generator


def generate_guide(request: GuideRequest) -> GuideResult:
    """便捷函数：生成引导"""
    return get_guide_generator().generate(request)