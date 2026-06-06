"""
版本适配管理器 - 管理不同版本的UI元素选择器规则
用于处理专属云/私有云等非最新版本系统的UI适配
"""

import json
import logging
import os
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
import threading

logger = logging.getLogger(__name__)

# 获取项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 从 APP/backend/services/ 回到项目根目录
PROJECT_ROOT = os.path.normpath(os.path.join(BASE_DIR, "../../.."))


@dataclass
class UIElement:
    """UI元素定义"""
    id: str
    name: str
    selector: str
    type: str
    description: str = ""
    alternatives: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'selector': self.selector,
            'type': self.type,
            'description': self.description,
            'alternatives': self.alternatives
        }


@dataclass
class GuideStep:
    """引导步骤"""
    step: int
    action: str  # click, select, input, drag, drop, config, navigate
    target: str  # 目标元素ID
    tip: str     # 操作提示
    selector: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'step': self.step,
            'action': self.action,
            'target': self.target,
            'tip': self.tip,
            'selector': self.selector
        }


class VersionAdapterManager:
    """
    版本适配管理器

    功能：
    1. 加载不同版本的UI元素规则
    2. 根据版本获取正确的元素选择器
    3. 将引导步骤适配到特定版本
    """

    def __init__(self, rules_base_path: Optional[str] = None):
        """
        初始化版本适配管理器

        Args:
            rules_base_path: UI规则库根路径，默认为 data/ui_rules/
        """
        if rules_base_path:
            self.rules_base_path = rules_base_path
        else:
            self.rules_base_path = os.path.join(PROJECT_ROOT, "data", "ui_rules")

        self.rules_store: Dict[str, Dict[str, UIElement]] = {}
        self.module_rules: Dict[str, Dict[str, Any]] = {}  # 按模块存储完整规则
        self._lock = threading.Lock()
        self._load_all_versions()

    def _load_all_versions(self) -> None:
        """加载所有版本的规则"""
        manifest_path = os.path.join(self.rules_base_path, "manifest.json")

        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)

            versions = manifest.get('versions', {})

            for version_key, version_info in versions.items():
                rules_path = version_info.get('rules_path', '')
                if rules_path:
                    self._load_version_rules(version_key, rules_path)

            logger.info(f"✅ 加载版本规则完成: {list(self.rules_store.keys())}")

        except Exception as e:
            logger.error(f"❌ 加载版本规则失败: {e}")
            # 加载兜底规则
            self._load_fallback_rules()

    def _load_version_rules(self, version_key: str, rules_path: str) -> None:
        """加载特定版本的规则"""
        version_dir = os.path.join(self.rules_base_path, rules_path)

        if not os.path.exists(version_dir):
            logger.warning(f"⚠️ 版本规则目录不存在: {version_dir}")
            return

        version_elements = {}
        version_modules = {}

        for filename in os.listdir(version_dir):
            if filename.endswith('.json'):
                filepath = os.path.join(version_dir, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        module_rules = json.load(f)

                    module_name = module_rules.get('module', filename[:-5])
                    version_modules[module_name] = module_rules

                    # 提取元素定义
                    elements = module_rules.get('elements', [])
                    for elem in elements:
                        element = UIElement(
                            id=elem.get('id', ''),
                            name=elem.get('name', ''),
                            selector=elem.get('selector', ''),
                            type=elem.get('type', ''),
                            description=elem.get('description', ''),
                            alternatives=elem.get('alternatives', [])
                        )
                        version_elements[f"{module_name}.{element.id}"] = element
                        # 也存储不带模块前缀的
                        version_elements[element.id] = element

                except Exception as e:
                    logger.error(f"❌ 加载规则文件失败 {filepath}: {e}")

        with self._lock:
            self.rules_store[version_key] = version_elements
            self.module_rules[version_key] = version_modules

    def _load_fallback_rules(self) -> None:
        """加载兜底规则"""
        fallback_path = os.path.join(self.rules_base_path, "fallback", "generic_selectors.json")

        if os.path.exists(fallback_path):
            try:
                with open(fallback_path, 'r', encoding='utf-8') as f:
                    rules = json.load(f)
                with self._lock:
                    self.rules_store['fallback'] = {
                        'generic': UIElement(
                            id='generic',
                            name='通用元素',
                            selector='*',
                            type='generic'
                        )
                    }
                    self.module_rules['fallback'] = rules
            except Exception as e:
                logger.error(f"❌ 加载兜底规则失败: {e}")

    def get_selector(
        self,
        element_id: str,
        version: str,
        module: Optional[str] = None
    ) -> Optional[str]:
        """
        根据版本获取元素选择器

        Args:
            element_id: 元素ID
            version: 版本key (latest, v3.2, fallback)
            module: 模块名 (可选)

        Returns:
            元素选择器字符串
        """
        with self._lock:
            # 尝试获取特定版本的规则
            version_rules = self.rules_store.get(version)

            if version_rules:
                # 优先尝试带模块前缀的
                if module:
                    element = version_rules.get(f"{module}.{element_id}")
                    if element:
                        return element.selector

                # 尝试不带前缀的
                element = version_rules.get(element_id)
                if element:
                    return element.selector

            # 回退到最新版规则
            if version != 'latest':
                latest_rules = self.rules_store.get('latest', {})
                if module:
                    element = latest_rules.get(f"{module}.{element_id}")
                    if element:
                        return element.selector
                element = latest_rules.get(element_id)
                if element:
                    return element.selector

            # 最后使用兜底规则
            fallback_rules = self.rules_store.get('fallback', {})
            element = fallback_rules.get(element_id)
            if element:
                return element.selector

            return None

    def get_element(
        self,
        element_id: str,
        version: str,
        module: Optional[str] = None
    ) -> Optional[UIElement]:
        """
        获取元素完整定义
        """
        with self._lock:
            version_rules = self.rules_store.get(version, {})

            if module:
                element = version_rules.get(f"{module}.{element_id}")
                if element:
                    return element

            element = version_rules.get(element_id)
            if element:
                return element

            # 回退到最新版
            if version != 'latest':
                latest_rules = self.rules_store.get('latest', {})
                element = latest_rules.get(element_id)
                if element:
                    return element

            return None

    def adapt_steps(
        self,
        steps: List[GuideStep],
        version: str,
        module: Optional[str] = None
    ) -> List[GuideStep]:
        """
        将引导步骤适配到特定版本

        Args:
            steps: 原始步骤列表
            version: 目标版本
            module: 模块名

        Returns:
            适配后的步骤列表
        """
        adapted = []

        for step in steps:
            adapted_step = GuideStep(
                step=step.step,
                action=step.action,
                target=step.target,
                tip=step.tip
            )

            # 查找适配的选择器
            if step.target:
                selector = self.get_selector(step.target, version, module)
                adapted_step.selector = selector

            adapted.append(adapted_step)

        return adapted

    def get_module_rules(self, module: str, version: str) -> Optional[Dict[str, Any]]:
        """
        获取模块的完整规则

        Args:
            module: 模块名
            version: 版本key

        Returns:
            模块规则字典
        """
        with self._lock:
            version_modules = self.module_rules.get(version, {})
            return version_modules.get(module)

    def find_matching_module(self, url: str, version: str) -> Optional[str]:
        """
        根据URL匹配合适的模块

        Args:
            url: 页面URL
            version: 版本key

        Returns:
            匹配的模块名
        """
        with self._lock:
            version_modules = self.module_rules.get(version, {})

            for module_name, rules in version_modules.items():
                url_pattern = rules.get('url_pattern', '')
                if url_pattern and url_pattern in url:
                    return module_name

            return None

    def list_available_versions(self) -> List[str]:
        """列出所有可用版本"""
        with self._lock:
            return list(self.rules_store.keys())

    def list_modules(self, version: str) -> List[str]:
        """列出指定版本的所有模块"""
        with self._lock:
            version_modules = self.module_rules.get(version, {})
            return list(version_modules.keys())


# 全局实例
_manager: Optional[VersionAdapterManager] = None


def get_version_adapter() -> VersionAdapterManager:
    """获取版本适配管理器单例"""
    global _manager
    if _manager is None:
        _manager = VersionAdapterManager()
    return _manager


def get_selector(element_id: str, version: str, module: Optional[str] = None) -> Optional[str]:
    """便捷函数：获取选择器"""
    return get_version_adapter().get_selector(element_id, version, module)


def adapt_steps(steps: List[GuideStep], version: str, module: Optional[str] = None) -> List[GuideStep]:
    """便捷函数：适配步骤"""
    return get_version_adapter().adapt_steps(steps, version, module)