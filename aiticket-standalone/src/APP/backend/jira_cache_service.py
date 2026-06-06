#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jira 缓存服务 (QCL 端)

功能：
- 代理请求到主机一 (通过 frp 隧道)
- 缓存响应数据，减少内网访问压力
- TTL 过期自动刷新
- 节点自动切换和故障转移

部署位置: QCL 公网服务器
"""

import os
import sys
import json
import time
import hashlib
import threading
import logging
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
from pathlib import Path
from functools import wraps
from queue import Queue, Empty

import requests
from requests.exceptions import RequestException

# 添加项目根目录到路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 缓存项类
class CacheItem:
    """缓存项"""

    def __init__(self, key: str, data: Any, ttl: int):
        self.key = key
        self.data = data
        self.expiry_time = time.time() + ttl
        self.created_at = time.time()
        self.hit_count = 0

    def is_expired(self) -> bool:
        """检查是否过期"""
        return time.time() > self.expiry_time

    def is_near_expiry(self, threshold: int = 30) -> bool:
        """检查是否接近过期 (默认30秒)"""
        return self.expiry_time - time.time() < threshold

    def time_to_expiry(self) -> int:
        """返回距离过期的时间(秒)"""
        return max(0, int(self.expiry_time - time.time()))


# 代理节点类
class ProxyNode:
    """代理节点"""

    def __init__(self, name: str, base_url: str, weight: int = 1, required: bool = False):
        self.name = name
        self.base_url = base_url
        self.weight = weight  # 权重，越高优先级越高
        self.required = required  # 是否为必要节点 (如 mini)
        self.healthy = True
        self.last_health_check = 0
        self.failure_count = 0
        self.success_count = 0
        self.last_error = None
        self._lock = threading.Lock()

    def mark_healthy(self):
        """标记为健康"""
        with self._lock:
            self.healthy = True
            self.failure_count = 0
            self.last_health_check = time.time()
            self.last_error = None

    def mark_unhealthy(self, error: str = None):
        """标记为不健康"""
        with self._lock:
            self.healthy = False
            self.failure_count += 1
            self.last_health_check = time.time()
            self.last_error = error

    def record_success(self):
        """记录成功请求"""
        with self._lock:
            self.success_count += 1

    def is_available(self) -> bool:
        """检查节点是否可用"""
        return self.healthy


# Jira 缓存服务
class JiraCacheService:
    """Jira 缓存服务"""

    def __init__(self, config: Dict[str, Any] = None):
        """
        初始化缓存服务

        Args:
            config: 配置字典
        """
        self.config = config or self._default_config()

        # 初始化日志
        self.logger = self._setup_logging()

        # 初始化缓存存储
        self._cache: Dict[str, CacheItem] = {}
        self._cache_lock = threading.RLock()

        # 初始化代理节点
        self._nodes: List[ProxyNode] = []
        self._load_nodes()

        # 初始化请求超时配置
        self.timeout = self.config.get('timeout', {
            'connect': 10,
            'read': 30,
            'total': 60
        })

        # 初始化重试配置
        self.retry_config = self.config.get('retry', {
            'max_attempts': 3,
            'retry_delay': 1,
            'retry_on': ['ConnectionError', 'Timeout', 'HTTPError']
        })

        # 初始化缓存目录
        self.cache_dir = self.config.get('cache', {}).get('cache_dir', 'data_cache')
        self._persistent_cache = {}  # 持久化缓存
        self._load_persistent_cache()

        # 启动后台任务
        self._running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_expired_cache, daemon=True)
        self._cleanup_thread.start()

        # 健康检查线程
        self._health_check_interval = self.config.get('monitoring', {}).get('health_check_interval', 30)
        self._health_check_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self._health_check_thread.start()

        self.logger.info(f"Jira 缓存服务初始化完成，节点数: {len(self._nodes)}")

    def _default_config(self) -> Dict[str, Any]:
        """默认配置"""
        return {
            'timeout': {
                'connect': 10,
                'read': 30,
                'total': 60
            },
            'retry': {
                'max_attempts': 3,
                'retry_delay': 1,
                'retry_on': ['ConnectionError', 'Timeout', 'HTTPError']
            },
            'cache': {
                'enabled': True,
                'ttl': {
                    'board_data': 300,
                    'field_data': 1800,
                    'search_results': 120,
                    'operations': 0
                },
                'cache_dir': 'data_cache',
                'max_cache_size': 104857600,
                'cleanup_interval': 3600
            },
            'monitoring': {
                'enabled': True,
                'health_check_interval': 30,
                'node_timeout': 10,
                'log_level': 'INFO'
            },
            'fallback': {
                'enabled': True,
                'use_local_cache': True,
                'cache_ttl_extend': 300
            }
        }

    def _setup_logging(self) -> logging.Logger:
        """设置日志"""
        log_level = self.config.get('monitoring', {}).get('log_level', 'INFO')
        logger = logging.getLogger('JiraCacheService')
        logger.setLevel(getattr(logging, log_level))

        # 简单的配置，避免复杂的 RotatingFileHandler
        handler = logging.StreamHandler()
        handler.setLevel(getattr(logging, log_level))
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        return logger

    def _load_nodes(self):
        """加载代理节点配置"""
        nodes_config = self.config.get('proxy_nodes', [])

        for node_config in nodes_config:
            if node_config.get('enabled', True):
                node = ProxyNode(
                    name=node_config['name'],
                    base_url=node_config['base_url'],
                    weight=node_config.get('weight', 1),
                    required=node_config.get('required', False)
                )
                self._nodes.append(node)
                required_status = "必要" if node.required else "可选"
                self.logger.info(f"加载代理节点: {node.name} -> {node.base_url} (权重:{node.weight}, {required_status})")

        if not self._nodes:
            self.logger.warning("没有可用的代理节点")

    def _get_cache_key(self, key_type: str, **params) -> str:
        """生成缓存键"""
        # 参数按排序后拼接，确保一致性
        params_str = '&'.join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
        key = f"{key_type}:{params_str}"

        # 使用 MD5 缩短键名
        return hashlib.md5(key.encode()).hexdigest()

    def _load_persistent_cache(self):
        """加载持久化缓存"""
        cache_file = os.path.join(self.cache_dir, 'jira_persistent_cache.json')

        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    self._persistent_cache = json.load(f)
                self.logger.info(f"加载持久化缓存: {len(self._persistent_cache)} 项")
            except Exception as e:
                self.logger.warning(f"加载持久化缓存失败: {e}")

    def _save_persistent_cache(self):
        """保存持久化缓存"""
        os.makedirs(self.cache_dir, exist_ok=True)
        cache_file = os.path.join(self.cache_dir, 'jira_persistent_cache.json')

        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(self._persistent_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.warning(f"保存持久化缓存失败: {e}")

    def _cleanup_expired_cache(self):
        """清理过期缓存的后台任务"""
        while self._running:
            try:
                time.sleep(60)  # 每分钟检查一次

                with self._cache_lock:
                    expired_keys = [
                        key for key, item in self._cache.items()
                        if item.is_expired()
                    ]

                    for key in expired_keys:
                        del self._cache[key]

                    if expired_keys:
                        self.logger.debug(f"清理过期缓存: {len(expired_keys)} 项")

            except Exception as e:
                self.logger.error(f"清理缓存异常: {e}")

    def _health_check_loop(self):
        """健康检查后台任务"""
        while self._running:
            try:
                self._check_nodes_health()
                time.sleep(self._health_check_interval)
            except Exception as e:
                self.logger.error(f"健康检查异常: {e}")

    def _check_nodes_health(self):
        """检查节点健康状态"""
        for node in self._nodes:
            try:
                url = f"{node.base_url}/proxy/health"
                response = requests.get(
                    url,
                    timeout=self.config.get('monitoring', {}).get('node_timeout', 10)
                )

                if response.status_code == 200:
                    if not node.is_available():
                        self.logger.info(f"节点恢复健康: {node.name}")
                    node.mark_healthy()
                    node.record_success()
                else:
                    self.logger.warning(f"节点健康检查失败: {node.name}, 状态码: {response.status_code}")
                    node.mark_unhealthy(f"HTTP {response.status_code}")

            except RequestException as e:
                self.logger.warning(f"节点健康检查异常: {node.name}, {e}")
                node.mark_unhealthy(str(e))

    def _get_available_node(self) -> Optional[ProxyNode]:
        """
        获取可用的节点

        节点选择策略:
        1. 优先选择必要节点 (required=True)
        2. 在同类节点中按权重排序选择
        3. 如果所有必要节点不可用，才选择可选节点
        """
        # 分离必要节点和可选节点
        required_nodes = [n for n in self._nodes if n.required and n.is_available()]
        optional_nodes = [n for n in self._nodes if not n.required and n.is_available()]

        # 按权重排序（权重越高越优先）
        required_nodes.sort(key=lambda n: n.weight, reverse=True)
        optional_nodes.sort(key=lambda n: n.weight, reverse=True)

        # 优先返回必要节点
        if required_nodes:
            return required_nodes[0]

        # 如果没有可用节点，记录警告
        if not required_nodes and not optional_nodes:
            self.logger.warning("没有可用的代理节点")

        # 返回可选节点 (如 lap)
        if optional_nodes:
            # 可选节点可能不在线，记录降级日志
            node_name = optional_nodes[0].name
            self.logger.info(f"使用可选节点: {node_name} (降级模式)")
            return optional_nodes[0]

        return None

    def _request_with_retry(self, url: str, method: str = 'GET',
                           params: Dict = None, json_data: Dict = None) -> Optional[Dict]:
        """
        带重试的请求

        Args:
            url: 请求 URL
            method: 请求方法
            params: 查询参数
            json_data: JSON 数据

        Returns:
            响应数据，失败返回 None
        """
        max_attempts = self.retry_config['max_attempts']
        retry_delay = self.retry_config['retry_delay']

        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    params=params,
                    json=json_data,
                    timeout=(self.timeout['connect'], self.timeout['read'])
                )

                response.raise_for_status()

                # 记录节点成功
                node = self._get_node_by_url(url)
                if node:
                    node.record_success()

                return response.json()

            except RequestException as e:
                self.logger.warning(f"请求失败 (尝试 {attempt}/{max_attempts}): {url}, {e}")

                if attempt < max_attempts:
                    time.sleep(retry_delay)

        return None

    def _get_node_by_url(self, url: str) -> Optional[ProxyNode]:
        """根据 URL 获取节点"""
        for node in self._nodes:
            if url.startswith(node.base_url):
                return node
        return None

    def _get_from_proxy(self, endpoint: str, key_type: str, ttl: int,
                        params: Dict = None, json_data: Dict = None) -> Dict[str, Any]:
        """
        从代理节点获取数据

        Args:
            endpoint: 端点路径
            key_type: 缓存键类型
            ttl: 缓存时间(秒)
            params: 查询参数
            json_data: JSON 数据

        Returns:
            响应数据
        """
        cache_key = self._get_cache_key(key_type, **(params or {}))

        # 检查缓存
        if self.config.get('cache', {}).get('enabled', True) and ttl > 0:
            with self._cache_lock:
                cache_item = self._cache.get(cache_key)
                if cache_item and not cache_item.is_expired():
                    cache_item.hit_count += 1
                    self.logger.debug(f"缓存命中: {cache_key}, 剩余TTL: {cache_item.time_to_expiry()}s")
                    return cache_item.data

        # 从代理节点获取
        node = self._get_available_node()
        if not node:
            self.logger.error("没有可用的代理节点")

            # 尝试使用过期缓存
            if self.config.get('fallback', {}).get('use_local_cache', True):
                with self._cache_lock:
                    cache_item = self._cache.get(cache_key)
                    if cache_item:
                        extend_ttl = self.config.get('fallback', {}).get('cache_ttl_extend', 300)
                        # 延长过期时间
                        cache_item.expiry_time = time.time() + extend_ttl
                        self.logger.warning(f"使用过期缓存 (降级): {cache_key}")
                        return cache_item.data

            return {'status': 'error', 'code': 'NO_AVAILABLE_NODE', 'message': '没有可用的代理节点'}

        url = f"{node.base_url}/{endpoint}"
        self.logger.info(f"请求代理节点: {node.name}, URL: {url}")

        response_data = self._request_with_retry(url, 'GET', params, json_data)

        if response_data and response_data.get('status') == 'success':
            # 缓存响应
            if ttl > 0:
                with self._cache_lock:
                    self._cache[cache_key] = CacheItem(cache_key, response_data, ttl)

            return response_data
        else:
            self.logger.error(f"代理节点响应失败: {response_data}")
            node.mark_unhealthy()
            return response_data or {'status': 'error', 'code': 'PROXY_ERROR', 'message': '代理节点响应失败'}

    # 公开 API 方法

    def get_fields(self) -> Dict[str, Any]:
        """
        获取 Jira 字段列表

        Returns:
            字段数据
        """
        ttl = self.config.get('cache', {}).get('ttl', {}).get('field_data', 1800)
        return self._get_from_proxy('proxy/jira/fields', 'fields', ttl)

    def search_issues(self, jql: str, start_at: int = 0, max_results: int = 50) -> Dict[str, Any]:
        """
        搜索工单

        Args:
            jql: JQL 查询语句
            start_at: 起始位置
            max_results: 最大结果数

        Returns:
            工单数据
        """
        ttl = self.config.get('cache', {}).get('ttl', {}).get('search_results', 120)
        return self._get_from_proxy(
            'proxy/jira/search',
            'search',
            ttl,
            params={'jql': jql, 'startAt': start_at, 'maxResults': max_results}
        )

    def get_issue(self, issue_key: str) -> Dict[str, Any]:
        """
        获取单个工单

        Args:
            issue_key: 工单键

        Returns:
            工单数据
        """
        ttl = self.config.get('cache', {}).get('ttl', {}).get('board_data', 300)
        return self._get_from_proxy(
            f'proxy/jira/issue/{issue_key}',
            'issue',
            ttl
        )

    def get_board_data(self, jql: str = "project=MYPROJECT") -> Dict[str, Any]:
        """
        获取看板数据

        Args:
            jql: JQL 查询语句

        Returns:
            看板数据
        """
        ttl = self.config.get('cache', {}).get('ttl', {}).get('board_data', 300)
        return self.search_issues(jql, 0, 500)

    def assign_issue(self, issue_key: str, assignee: str, comment: str = None) -> Dict[str, Any]:
        """
        分配工单 (不缓存)

        Args:
            issue_key: 工单键
            assignee: 受理人
            comment: 评论

        Returns:
            操作结果
        """
        return self._get_from_proxy(
            'proxy/jira/assign',
            'assign',
            0,  # 不缓存
            json_data={'issue_key': issue_key, 'assignee': assignee, 'comment': comment}
        )

    def add_comment(self, issue_key: str, comment: str, close: bool = False) -> Dict[str, Any]:
        """
        添加评论 (不缓存)

        Args:
            issue_key: 工单键
            comment: 评论内容
            close: 是否关闭工单

        Returns:
            操作结果
        """
        return self._get_from_proxy(
            'proxy/jira/comment',
            'comment',
            0,  # 不缓存
            json_data={'issue_key': issue_key, 'comment': comment, 'close': close}
        )

    def get_field_options(self, issue_id: str, field_ids: List[str]) -> Dict[str, Any]:
        """
        获取字段选项

        Args:
            issue_id: 工单ID
            field_ids: 字段ID列表

        Returns:
            字段选项
        """
        ttl = self.config.get('cache', {}).get('ttl', {}).get('field_data', 1800)
        cache_key = self._get_cache_key('field_options', issue_id=issue_id, field_ids=','.join(field_ids))

        # 检查缓存
        if ttl > 0:
            with self._cache_lock:
                cache_item = self._cache.get(cache_key)
                if cache_item and not cache_item.is_expired():
                    cache_item.hit_count += 1
                    return cache_item.data

        # 从代理获取
        node = self._get_available_node()
        if not node:
            return {'status': 'error', 'code': 'NO_AVAILABLE_NODE', 'message': '没有可用的代理节点'}

        url = f"{node.base_url}/proxy/jira/field-options"
        response_data = self._request_with_retry(url, 'POST', json_data={'issue_id': issue_id, 'field_ids': field_ids})

        if response_data and response_data.get('status') == 'success':
            # 缓存响应
            with self._cache_lock:
                self._cache[cache_key] = CacheItem(cache_key, response_data, ttl)
            return response_data

        return response_data or {'status': 'error', 'code': 'PROXY_ERROR', 'message': '代理节点响应失败'}

    def get_metrics(self) -> Dict[str, Any]:
        """获取服务指标"""
        with self._cache_lock:
            cache_stats = {
                'total_items': len(self._cache),
                'items': [
                    {
                        'key': item.key,
                        'created_at': datetime.fromtimestamp(item.created_at).isoformat(),
                        'time_to_expiry': item.time_to_expiry(),
                        'hit_count': item.hit_count
                    }
                    for item in self._cache.values()
                ]
            }

        node_stats = [
            {
                'name': node.name,
                'healthy': node.is_available(),
                'failure_count': node.failure_count,
                'success_count': node.success_count,
                'last_error': node.last_error
            }
            for node in self._nodes
        ]

        return {
            'status': 'success',
            'data': {
                'cache': cache_stats,
                'nodes': node_stats,
                'service': 'jira_cache',
                'version': '1.0.0'
            }
        }

    def clear_cache(self, key_type: str = None):
        """清理缓存"""
        with self._cache_lock:
            if key_type:
                # 清理指定类型的缓存
                keys_to_delete = [
                    key for key in self._cache.keys()
                    if key.startswith(hashlib.md5(f"{key_type}:".encode()).hexdigest()[:8])
                ]
                for key in keys_to_delete:
                    del self._cache[key]
                self.logger.info(f"清理缓存类型: {key_type}, 数量: {len(keys_to_delete)}")
            else:
                # 清理所有缓存
                count = len(self._cache)
                self._cache.clear()
                self.logger.info(f"清理所有缓存: {count} 项")

    def shutdown(self):
        """关闭服务"""
        self._running = False
        self._cleanup_thread.join(timeout=5)
        self._health_check_thread.join(timeout=5)
        self.logger.info("Jira 缓存服务已关闭")


# 全局实例
_jira_cache_service = None


def get_jira_cache_service(config: Dict[str, Any] = None) -> JiraCacheService:
    """获取 Jira 缓存服务实例"""
    global _jira_cache_service

    if _jira_cache_service is None:
        _jira_cache_service = JiraCacheService(config)

    return _jira_cache_service


# 命令行测试
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Jira 缓存服务测试')
    parser.add_argument('--test', action='store_true', help='运行测试')
    parser.add_argument('--metrics', action='store_true', help='显示指标')
    parser.add_argument('--clear-cache', type=str, nargs='?', const='all',
                        help='清理缓存 (可指定类型: fields, search, issue)')

    args = parser.parse_args()

    service = get_jira_cache_service()

    if args.test:
        print("=== 测试 Jira 缓存服务 ===")

        # 测试字段获取
        print("\n1. 获取字段列表...")
        fields = service.get_fields()
        print(f"   结果: {fields.get('status')}, 数量: {len(fields.get('data', []))}")

        # 测试工单搜索
        print("\n2. 搜索工单...")
        result = service.search_issues('project=MYPROJECT', 0, 1)
        print(f"   结果: {result.get('status')}, 工单数: {len(result.get('data', {}).get('issues', []))}")

        print("\n=== 测试完成 ===")

    elif args.metrics:
        metrics = service.get_metrics()
        print(json.dumps(metrics, ensure_ascii=False, indent=2))

    elif args.clear_cache:
        service.clear_cache(args.clear_cache if args.clear_cache != 'all' else None)
        print(f"缓存已清理: {args.clear_cache or 'all'}")

    else:
        print("使用 --test 运行测试, --metrics 显示指标, --clear-cache 清理缓存")
