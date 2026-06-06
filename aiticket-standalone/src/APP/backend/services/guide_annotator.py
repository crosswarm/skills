"""
引导标注服务 - 在截图上生成标注（红框、箭头、步骤说明）
"""

import io
import json
import logging
import os
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
import base64

logger = logging.getLogger(__name__)

# 获取项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(BASE_DIR, "../.."))


class AnnotationType(Enum):
    """标注类型"""
    RECTANGLE = "rectangle"      # 矩形框
    ARROW = "arrow"              # 箭头
    CIRCLE = "circle"            # 圆形
    NUMBER = "number"            # 数字标注
    TEXT = "text"                # 文字说明
    HIGHLIGHT = "highlight"      # 高亮


@dataclass
class Annotation:
    """标注定义"""
    type: AnnotationType
    x: int
    y: int
    width: int = 0
    height: int = 0
    text: str = ""
    color: str = "#FF0000"
    line_width: int = 2
    order: int = 0  # 步骤序号

    def to_dict(self) -> Dict[str, Any]:
        return {
            'type': self.type.value,
            'x': self.x,
            'y': self.y,
            'width': self.width,
            'height': self.height,
            'text': self.text,
            'color': self.color,
            'line_width': self.line_width,
            'order': self.order
        }


@dataclass
class AnnotatedImage:
    """标注后的图片结果"""
    image_id: str
    original_size: Tuple[int, int]
    annotations: List[Annotation]
    annotated_image_base64: Optional[str] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'image_id': self.image_id,
            'original_size': list(self.original_size),
            'annotations': [a.to_dict() for a in self.annotations],
            'annotated_image_base64': self.annotated_image_base64,
            'steps': self.steps
        }


class GuideAnnotator:
    """
    引导标注服务

    功能：
    1. 在截图上绘制矩形框、箭头等标注
    2. 添加步骤序号和说明文字
    3. 生成带标注的图片
    """

    # 颜色配置
    COLORS = {
        'primary': '#FF0000',      # 主要元素 - 红色
        'secondary': '#0078D7',    # 次要元素 - 蓝色
        'highlight': '#FFD700',    # 高亮 - 金色
        'success': '#28A745',      # 成功 - 绿色
        'warning': '#FFC107',      # 警告 - 黄色
        'text_bg': '#FFFFFF',      # 文字背景 - 白色
    }

    def __init__(self):
        """初始化标注器"""
        self._try_import_pil()

    def _try_import_pil(self):
        """尝试导入PIL库"""
        try:
            from PIL import Image, ImageDraw, ImageFont
            self.Image = Image
            self.ImageDraw = ImageDraw
            self.ImageFont = ImageFont
            self._pil_available = True
            logger.info("✅ PIL库加载成功")
        except ImportError:
            self._pil_available = False
            logger.warning("⚠️ PIL库未安装，将返回标注数据而不生成图片")

    def annotate(
        self,
        image_data: bytes,
        annotations: List[Annotation],
        add_step_numbers: bool = True
    ) -> AnnotatedImage:
        """
        在图片上添加标注

        Args:
            image_data: 原始图片数据
            annotations: 标注列表
            add_step_numbers: 是否添加步骤序号

        Returns:
            AnnotatedImage: 标注结果
        """
        import hashlib

        # 生成图片ID
        image_id = hashlib.md5(image_data).hexdigest()[:16]

        # 如果PIL不可用，只返回标注数据
        if not self._pil_available:
            return self._create_annotation_only_result(image_id, annotations)

        try:
            # 打开图片
            image = self.Image.open(io.BytesIO(image_data))
            original_size = image.size

            # 转换为RGB模式（如果需要）
            if image.mode != 'RGB':
                image = image.convert('RGB')

            # 创建绘制对象
            draw = self.ImageDraw.Draw(image)

            # 按顺序绘制标注
            sorted_annotations = sorted(annotations, key=lambda a: a.order)

            for ann in sorted_annotations:
                self._draw_annotation(draw, ann)

            # 添加步骤序号
            if add_step_numbers:
                self._add_step_numbers(draw, sorted_annotations)

            # 转换为base64
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            annotated_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

            return AnnotatedImage(
                image_id=image_id,
                original_size=original_size,
                annotations=sorted_annotations,
                annotated_image_base64=annotated_base64,
                steps=self._generate_steps_text(sorted_annotations)
            )

        except Exception as e:
            logger.error(f"❌ 标注生成失败: {e}")
            return AnnotatedImage(
                image_id=image_id,
                original_size=(0, 0),
                annotations=annotations
            )

    def _draw_annotation(self, draw, ann: Annotation) -> None:
        """绘制单个标注"""
        color = ann.color

        if ann.type == AnnotationType.RECTANGLE:
            # 绘制矩形
            draw.rectangle(
                [ann.x, ann.y, ann.x + ann.width, ann.y + ann.height],
                outline=color,
                width=ann.line_width
            )

        elif ann.type == AnnotationType.CIRCLE:
            # 绘制圆形（椭圆）
            draw.ellipse(
                [ann.x, ann.y, ann.x + ann.width, ann.y + ann.height],
                outline=color,
                width=ann.line_width
            )

        elif ann.type == AnnotationType.ARROW:
            # 绘制箭头（简化为线条+三角形）
            self._draw_arrow(draw, ann.x, ann.y,
                           ann.x + ann.width, ann.y + ann.height,
                           color, ann.line_width)

        elif ann.type == AnnotationType.NUMBER:
            # 绘制数字圆圈
            self._draw_number_circle(draw, ann.x, ann.y, ann.order, color)

        elif ann.type == AnnotationType.TEXT:
            # 绘制文字
            self._draw_text(draw, ann.x, ann.y, ann.text, color)

        elif ann.type == AnnotationType.HIGHLIGHT:
            # 绘制高亮（半透明矩形）
            self._draw_highlight(draw, ann.x, ann.y, ann.width, ann.height, color)

    def _draw_arrow(self, draw, x1: int, y1: int, x2: int, y2: int, color: str, width: int):
        """绘制箭头"""
        # 绘制线条
        draw.line([(x1, y1), (x2, y2)], fill=color, width=width)

        # 计算箭头头部
        import math
        angle = math.atan2(y2 - y1, x2 - x1)
        arrow_length = 15
        arrow_angle = math.pi / 6

        # 箭头两个点
        ax1 = x2 - arrow_length * math.cos(angle - arrow_angle)
        ay1 = y2 - arrow_length * math.sin(angle - arrow_angle)
        ax2 = x2 - arrow_length * math.cos(angle + arrow_angle)
        ay2 = y2 - arrow_length * math.sin(angle + arrow_angle)

        # 绘制箭头头部
        draw.polygon([(x2, y2), (ax1, ay1), (ax2, ay2)], fill=color)

    def _draw_number_circle(self, draw, x: int, y: int, number: int, color: str):
        """绘制带数字的圆圈"""
        radius = 15

        # 绘制圆圈
        draw.ellipse(
            [x - radius, y - radius, x + radius, y + radius],
            fill=color,
            outline='white',
            width=2
        )

        # 绘制数字
        try:
            font = self.ImageFont.truetype("Arial", 14)
        except:
            font = self.ImageFont.load_default()

        text = str(number)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        draw.text(
            (x - text_width // 2, y - text_height // 2),
            text,
            fill='white',
            font=font
        )

    def _draw_text(self, draw, x: int, y: int, text: str, color: str):
        """绘制文字"""
        try:
            font = self.ImageFont.truetype("Arial", 12)
        except:
            font = self.ImageFont.load_default()

        # 绘制背景
        bbox = draw.textbbox((x, y), text, font=font)
        padding = 4
        draw.rectangle(
            [bbox[0] - padding, bbox[1] - padding,
             bbox[2] + padding, bbox[3] + padding],
            fill=self.COLORS['text_bg']
        )

        # 绘制文字
        draw.text((x, y), text, fill=color, font=font)

    def _draw_highlight(self, draw, x: int, y: int, width: int, height: int, color: str):
        """绘制高亮效果"""
        # 创建半透明效果（PIL不支持真正的透明度，用浅色代替）
        highlight_color = self._lighten_color(color, 0.5)
        draw.rectangle(
            [x, y, x + width, y + height],
            fill=highlight_color,
            outline=color,
            width=2
        )

    def _lighten_color(self, hex_color: str, factor: float) -> str:
        """将颜色变浅"""
        hex_color = hex_color.lstrip('#')
        r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        r = int(r + (255 - r) * factor)
        g = int(g + (255 - g) * factor)
        b = int(b + (255 - b) * factor)
        return f'#{r:02x}{g:02x}{b:02x}'

    def _add_step_numbers(self, draw, annotations: List[Annotation]):
        """添加步骤序号"""
        step_num = 1
        for ann in annotations:
            if ann.is_interactive if hasattr(ann, 'is_interactive') else True:
                self._draw_number_circle(draw, ann.x, ann.y, step_num, self.COLORS['primary'])
                step_num += 1

    def _generate_steps_text(self, annotations: List[Annotation]) -> List[Dict[str, Any]]:
        """生成步骤文字说明"""
        steps = []
        for i, ann in enumerate(annotations, 1):
            if ann.text:
                steps.append({
                    'step': i,
                    'text': ann.text,
                    'type': ann.type.value
                })
        return steps

    def _create_annotation_only_result(
        self,
        image_id: str,
        annotations: List[Annotation]
    ) -> AnnotatedImage:
        """创建仅包含标注数据的结果"""
        return AnnotatedImage(
            image_id=image_id,
            original_size=(0, 0),
            annotations=annotations,
            steps=self._generate_steps_text(annotations)
        )

    def create_guide_annotations(
        self,
        detected_elements: List[Dict[str, Any]],
        guide_steps: List[Dict[str, Any]]
    ) -> List[Annotation]:
        """
        根据检测到的元素和引导步骤创建标注

        Args:
            detected_elements: 检测到的元素列表
            guide_steps: 引导步骤列表

        Returns:
            标注列表
        """
        annotations = []

        for i, step in enumerate(guide_steps, 1):
            target = step.get('target', '')
            tip = step.get('tip', '')

            # 查找对应的元素
            element = self._find_element_by_id(detected_elements, target)

            if element:
                bounds = element.get('bounds', {})

                # 创建矩形标注
                annotations.append(Annotation(
                    type=AnnotationType.RECTANGLE,
                    x=bounds.get('x', 0),
                    y=bounds.get('y', 0),
                    width=bounds.get('width', 100),
                    height=bounds.get('height', 30),
                    text=tip,
                    color=self.COLORS['primary'],
                    order=i
                ))

                # 创建数字标注
                annotations.append(Annotation(
                    type=AnnotationType.NUMBER,
                    x=bounds.get('x', 0) + 10,
                    y=bounds.get('y', 0) + 10,
                    width=0,
                    height=0,
                    text=str(i),
                    color=self.COLORS['primary'],
                    order=i
                ))

        return annotations

    def _find_element_by_id(
        self,
        elements: List[Dict[str, Any]],
        element_id: str
    ) -> Optional[Dict[str, Any]]:
        """根据ID查找元素"""
        for elem in elements:
            if elem.get('id') == element_id:
                return elem
        return None


# 全局实例
_annotator: Optional[GuideAnnotator] = None


def get_annotator() -> GuideAnnotator:
    """获取标注器单例"""
    global _annotator
    if _annotator is None:
        _annotator = GuideAnnotator()
    return _annotator


def annotate_image(
    image_data: bytes,
    annotations: List[Annotation]
) -> AnnotatedImage:
    """便捷函数：标注图片"""
    return get_annotator().annotate(image_data, annotations)