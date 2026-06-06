"""
环境识别服务 - 识别用户系统环境类型
用于判断公有云/专属云/私有云，选择合适的引导策略
"""

import json
import logging
import os
from typing import Dict, Any, Optional, Literal
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# 获取项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 从 APP/backend/services/ 回到项目根目录
PROJECT_ROOT = os.path.normpath(os.path.join(BASE_DIR, "../../.."))


class EnvironmentType(Enum):
    """环境类型枚举"""
    PUBLIC_CLOUD = "public_cloud"      # 公有云租户
    DEDICATED_CLOUD = "dedicated_cloud" # 专属云
    PRIVATE_CLOUD = "private_cloud"     # 私有云


class AccessMethod(Enum):
    """访问方式枚举"""
    AGENT = "agent"       # Agent自动访问
    PLUGIN = "plugin"     # 本地插件
    SCREENSHOT = "screenshot"  # 截图分析


@dataclass
class EnvironmentInfo:
    """环境信息数据类"""
    env_type: EnvironmentType
    version: str
    access_method: AccessMethod
    ui_rules_version: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            'env_type': self.env_type.value,
            'version': self.version,
            'access_method': self.access_method.value,
            'ui_rules_version': self.ui_rules_version,
            'description': self.description
        }


class EnvironmentDetector:
    """
    环境识别服务

    根据租户信息判断用户系统环境类型，选择合适的引导方案：
    - 公有云：最新代码，可Agent访问
    - 专属云：非最新代码，可Agent访问，需版本适配
    - 私有云：不能远程接入，使用截图/插件方案
    """

    def __init__(self, manifest_path: Optional[str] = None):
        """
        初始化环境识别器

        Args:
            manifest_path: UI规则清单文件路径，默认使用data/ui_rules/manifest.json
        """
        if manifest_path:
            self.manifest_path = manifest_path
        else:
            self.manifest_path = os.path.join(
                PROJECT_ROOT, "data", "ui_rules", "manifest.json"
            )
        self._manifest = None
        self._load_manifest()

    def _load_manifest(self) -> None:
        """加载版本清单"""
        try:
            if os.path.exists(self.manifest_path):
                with open(self.manifest_path, 'r', encoding='utf-8') as f:
                    self._manifest = json.load(f)
                logger.info(f"✅ 加载版本清单成功: {self.manifest_path}")
            else:
                logger.warning(f"⚠️ 版本清单文件不存在: {self.manifest_path}")
                self._manifest = self._get_default_manifest()
        except Exception as e:
            logger.error(f"❌ 加载版本清单失败: {e}")
            self._manifest = self._get_default_manifest()

    def _get_default_manifest(self) -> Dict:
        """获取默认清单配置"""
        return {
            "versions": {
                "latest": {
                    "code_version": "3.5+",
                    "deployment": ["public_cloud"],
                    "access_method": "agent"
                },
                "v3.2": {
                    "code_version": "3.2.x",
                    "deployment": ["dedicated_cloud"],
                    "access_method": "agent"
                },
                "fallback": {
                    "code_version": "unknown",
                    "deployment": ["private_cloud"],
                    "access_method": "screenshot"
                }
            },
            "deployment_type_mapping": {
                "public": "latest",
                "dedicated": "v3.2",
                "private": "fallback"
            }
        }

    def detect(self, tenant_info: Dict[str, Any]) -> EnvironmentInfo:
        """
        识别用户系统环境

        Args:
            tenant_info: 租户信息，包含：
                - deployment_type: 部署类型 (public/dedicated/private)
                - system_version: 系统版本号 (可选)
                - tenant_id: 租户ID (可选)
                - tenant_name: 租户名称 (可选)

        Returns:
            EnvironmentInfo: 环境信息
        """
        deployment_type = tenant_info.get('deployment_type', 'private')
        system_version = tenant_info.get('system_version', 'unknown')

        # 根据部署类型映射版本
        version_key = self._manifest.get('deployment_type_mapping', {}).get(
            deployment_type, 'fallback'
        )

        version_info = self._manifest.get('versions', {}).get(
            version_key, self._manifest['versions']['fallback']
        )

        # 确定环境类型
        env_type = self._determine_env_type(deployment_type)

        # 确定访问方式
        access_method = self._determine_access_method(
            deployment_type,
            version_info.get('access_method', 'screenshot')
        )

        # 确定UI规则版本
        ui_rules_version = version_key

        # 如果有具体版本号，尝试匹配更精确的规则
        if system_version and system_version != 'unknown':
            matched_version = self._match_version(system_version)
            if matched_version:
                ui_rules_version = matched_version

        return EnvironmentInfo(
            env_type=env_type,
            version=system_version,
            access_method=access_method,
            ui_rules_version=ui_rules_version,
            description=version_info.get('description', '')
        )

    def _determine_env_type(self, deployment_type: str) -> EnvironmentType:
        """确定环境类型"""
        mapping = {
            'public': EnvironmentType.PUBLIC_CLOUD,
            'dedicated': EnvironmentType.DEDICATED_CLOUD,
            'private': EnvironmentType.PRIVATE_CLOUD
        }
        return mapping.get(deployment_type, EnvironmentType.PRIVATE_CLOUD)

    def _determine_access_method(
        self,
        deployment_type: str,
        default_method: str
    ) -> AccessMethod:
        """确定访问方式"""
        mapping = {
            'agent': AccessMethod.AGENT,
            'plugin': AccessMethod.PLUGIN,
            'screenshot': AccessMethod.SCREENSHOT
        }
        return mapping.get(default_method, AccessMethod.SCREENSHOT)

    def _match_version(self, system_version: str) -> Optional[str]:
        """
        匹配系统版本到规则版本

        Args:
            system_version: 系统版本号，如 "3.5.2", "3.2.1", "2.8.0"

        Returns:
            匹配的规则版本key，如 "latest", "v3.2"
        """
        versions = self._manifest.get('versions', {})

        # 尝试精确匹配
        for key, info in versions.items():
            code_version = info.get('code_version', '')
            if self._version_matches(system_version, code_version):
                return key

        return None

    def _version_matches(self, system_version: str, code_version: str) -> bool:
        """
        检查版本是否匹配

        Args:
            system_version: 系统版本，如 "3.5.2"
            code_version: 代码版本模式，如 "3.5+", "3.2.x"
        """
        try:
            # 处理 "3.5+" 格式 (3.5及以上)
            if code_version.endswith('+'):
                min_version = code_version[:-1]
                return self._compare_versions(system_version, min_version) >= 0

            # 处理 "3.2.x" 格式 (3.2系列)
            if code_version.endswith('.x'):
                prefix = code_version[:-2]
                return system_version.startswith(prefix + '.')

            # 精确匹配
            return system_version == code_version

        except Exception:
            return False

    def _compare_versions(self, v1: str, v2: str) -> int:
        """
        比较两个版本号

        Returns:
            正数: v1 > v2
            0: v1 == v2
            负数: v1 < v2
        """
        parts1 = [int(x) for x in v1.split('.')]
        parts2 = [int(x) for x in v2.split('.')]

        for p1, p2 in zip(parts1, parts2):
            if p1 != p2:
                return p1 - p2

        return len(parts1) - len(parts2)

    def get_strategy_for_environment(self, env_info: EnvironmentInfo) -> Dict[str, Any]:
        """
        根据环境信息获取引导策略

        Args:
            env_info: 环境信息

        Returns:
            引导策略配置
        """
        strategies = {
            EnvironmentType.PUBLIC_CLOUD: {
                'primary_method': 'agent',
                'fallback_method': 'screenshot',
                'supports_realtime': True,
                'requires_user_action': False,
                'description': 'Agent自动访问生成实时引导'
            },
            EnvironmentType.DEDICATED_CLOUD: {
                'primary_method': 'agent',
                'fallback_method': 'screenshot',
                'supports_realtime': True,
                'requires_user_action': False,
                'requires_version_adaptation': True,
                'description': 'Agent访问 + 版本适配层'
            },
            EnvironmentType.PRIVATE_CLOUD: {
                'primary_method': 'screenshot',
                'fallback_method': 'plugin',
                'supports_realtime': False,
                'requires_user_action': True,
                'user_actions': ['上传截图', '或安装本地插件'],
                'description': '用户上传截图，AI分析生成指引'
            }
        }

        return strategies.get(env_info.env_type, strategies[EnvironmentType.PRIVATE_CLOUD])


# 全局实例
_detector: Optional[EnvironmentDetector] = None


def get_detector() -> EnvironmentDetector:
    """获取环境识别器单例"""
    global _detector
    if _detector is None:
        _detector = EnvironmentDetector()
    return _detector


def detect_environment(tenant_info: Dict[str, Any]) -> EnvironmentInfo:
    """便捷函数：识别环境"""
    return get_detector().detect(tenant_info)