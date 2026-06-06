#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络监控模块

功能：
- 节点健康状态监控
- 自动故障告警
- 性能指标收集
- 服务可用性报告

部署位置: QCL 公网服务器
"""

import os
import sys
import time
import json
import logging
import threading
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from enum import Enum
from collections import deque

import requests
from requests.exceptions import RequestException

# 添加项目根目录到路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


class NodeStatus(Enum):
    """节点状态枚举"""
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass
class NodeHealth:
    """节点健康状态"""
    name: str
    status: NodeStatus
    last_check: str
    response_time_ms: float
    error_count: int
    success_count: int
    total_requests: int
    success_rate: float
    last_error: Optional[str] = None
    required: bool = False  # 是否为必要节点
    weight: int = 1  # 权重


@dataclass
class Alert:
    """告警信息"""
    id: str
    type: str  # node_down, high_latency, error_threshold
    level: str  # info, warning, error, critical
    message: str
    node: str
    timestamp: str
    acknowledged: bool = False


class NetworkMonitor:
    """网络监控器"""

    def __init__(self, nodes: List[Dict[str, Any]], config: Dict[str, Any] = None):
        """
        初始化网络监控器

        Args:
            nodes: 节点配置列表 [{'name': 'host1', 'base_url': 'http://...'}, ...]
            config: 配置字典
        """
        self.nodes_config = nodes
        self.config = config or self._default_config()

        # 初始化日志
        self.logger = self._setup_logging()

        # 节点健康状态
        self._node_health: Dict[str, NodeHealth] = {}
        self._init_node_health()

        # 告警历史
        self._alerts: List[Alert] = []
        self._alerts_lock = threading.Lock()

        # 性能指标历史 (保留最近100次)
        self._performance_history: Dict[str, deque] = {}
        for node in nodes:
            self._performance_history[node['name']] = deque(maxlen=100)

        # 监控状态
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None

        # 告警回调函数
        self._alert_callback: Optional[Callable] = None

    def _default_config(self) -> Dict[str, Any]:
        """默认配置"""
        return {
            'health_check_interval': 30,
            'node_timeout': 10,
            'success_rate_threshold': 0.9,
            'latency_threshold_ms': 1000,
            'error_threshold': 5,
            'alert_retention_hours': 24,
            'log_level': 'INFO'
        }

    def _setup_logging(self) -> logging.Logger:
        """设置日志"""
        log_level = self.config.get('log_level', 'INFO')
        logger = logging.getLogger('NetworkMonitor')
        logger.setLevel(getattr(logging, log_level))

        handler = logging.StreamHandler()
        handler.setLevel(getattr(logging, log_level))
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        return logger

    def _init_node_health(self):
        """初始化节点健康状态"""
        for node in self.nodes_config:
            self._node_health[node['name']] = NodeHealth(
                name=node['name'],
                status=NodeStatus.UNKNOWN,
                last_check=datetime.now().isoformat(),
                response_time_ms=0,
                error_count=0,
                success_count=0,
                total_requests=0,
                success_rate=0,
                required=node.get('required', False),
                weight=node.get('weight', 1)
            )

    def _check_node_health(self, node_config: Dict[str, Any]) -> NodeHealth:
        """
        检查单个节点的健康状态

        Args:
            node_config: 节点配置

        Returns:
            节点健康状态
        """
        name = node_config['name']
        base_url = node_config['base_url']
        timeout = self.config.get('node_timeout', 10)

        start_time = time.time()
        error = None

        try:
            # 健康检查接口
            response = requests.get(
                f"{base_url}/proxy/health",
                timeout=timeout
            )

            response_time = (time.time() - start_time) * 1000

            if response.status_code == 200:
                # 节点健康
                health = self._node_health[name]
                health.success_count += 1
                health.total_requests += 1
                health.success_rate = health.success_count / health.total_requests
                health.response_time_ms = response_time
                health.last_check = datetime.now().isoformat()

                # 记录性能历史
                self._performance_history[name].append({
                    'timestamp': datetime.now().isoformat(),
                    'response_time_ms': response_time,
                    'status': 'success'
                })

                # 判断状态
                if health.success_rate < self.config.get('success_rate_threshold', 0.9):
                    health.status = NodeStatus.DEGRADED
                elif response_time > self.config.get('latency_threshold_ms', 1000):
                    health.status = NodeStatus.DEGRADED
                else:
                    health.status = NodeStatus.HEALTHY

                return health
            else:
                error = f"HTTP {response.status_code}"
                raise Exception(error)

        except RequestException as e:
            response_time = (time.time() - start_time) * 1000
            error = str(e)

            # 节点不健康
            health = self._node_health[name]
            health.error_count += 1
            health.total_requests += 1
            health.success_rate = health.success_count / health.total_requests
            health.response_time_ms = response_time
            health.last_check = datetime.now().isoformat()
            health.last_error = error
            health.status = NodeStatus.UNHEALTHY

            # 记录性能历史
            self._performance_history[name].append({
                'timestamp': datetime.now().isoformat(),
                'response_time_ms': response_time,
                'status': 'error',
                'error': error
            })

            return health

    def _generate_alert_id(self) -> str:
        """生成告警 ID"""
        return f"alert-{int(time.time() * 1000)}"

    def _check_alerts(self, health: NodeHealth):
        """
        检查是否需要发送告警

        Args:
            health: 节点健康状态
        """
        # 可选节点 (如 lap) 使用更宽松的告警策略
        if health.required:
            # 必要节点：使用标准阈值
            error_threshold = self.config.get('error_threshold', 5)
            critical_alert = True
        else:
            # 可选节点：使用更高的错误阈值，且不发送 critical 告警
            error_threshold = self.config.get('error_threshold', 5) * 3  # 3倍阈值
            critical_alert = False

        if health.status == NodeStatus.UNHEALTHY and health.error_count >= error_threshold:
            # 节点宕机告警
            level = 'critical' if critical_alert else 'info'
            message_prefix = "" if health.required else "可选节点 "

            alert = Alert(
                id=self._generate_alert_id(),
                type='node_down',
                level=level,
                message=f"{message_prefix}节点 {health.name} 连续失败 {health.error_count} 次",
                node=health.name,
                timestamp=datetime.now().isoformat()
            )
            self._add_alert(alert)

        elif health.status == NodeStatus.DEGRADED:
            if health.response_time_ms > self.config.get('latency_threshold_ms', 1000):
                # 高延迟告警 (仅必要节点)
                if health.required:
                    alert = Alert(
                        id=self._generate_alert_id(),
                        type='high_latency',
                        level='warning',
                        message=f"节点 {health.name} 响应延迟过高: {health.response_time_ms:.2f}ms",
                        node=health.name,
                        timestamp=datetime.now().isoformat()
                    )
                    self._add_alert(alert)

            elif health.success_rate < self.config.get('success_rate_threshold', 0.9):
                # 成功率低告警 (仅必要节点)
                if health.required:
                    alert = Alert(
                        id=self._generate_alert_id(),
                        type='low_success_rate',
                        level='warning',
                        message=f"节点 {health.name} 成功率过低: {health.success_rate:.2%}",
                        node=health.name,
                        timestamp=datetime.now().isoformat()
                    )
                    self._add_alert(alert)

    def _add_alert(self, alert: Alert):
        """
        添加告警

        Args:
            alert: 告警信息
        """
        with self._alerts_lock:
            # 检查是否有重复告警 (同一节点同一类型，最近5分钟)
            recent_alerts = [
                a for a in self._alerts
                if a.node == alert.node and a.type == alert.type
                and (datetime.now() - datetime.fromisoformat(a.timestamp.replace('Z', '+00:00'))).total_seconds() < 300
            ]

            if not recent_alerts:
                self._alerts.append(alert)
                self.logger.warning(f"[告警] {alert.level}: {alert.message}")

                # 调用告警回调
                if self._alert_callback:
                    try:
                        self._alert_callback(alert)
                    except Exception as e:
                        self.logger.error(f"告警回调失败: {e}")

    def _cleanup_old_alerts(self):
        """清理过期告警"""
        retention_hours = self.config.get('alert_retention_hours', 24)
        cutoff = datetime.now() - timedelta(hours=retention_hours)

        with self._alerts_lock:
            self._alerts = [
                alert for alert in self._alerts
                if datetime.fromisoformat(alert.timestamp.replace('Z', '+00:00')) > cutoff
            ]

    def _monitor_loop(self):
        """监控循环"""
        interval = self.config.get('health_check_interval', 30)

        self.logger.info(f"网络监控已启动，检查间隔: {interval}秒")

        while self._running:
            try:
                # 检查所有节点
                for node_config in self.nodes_config:
                    if not node_config.get('enabled', True):
                        continue

                    health = self._check_node_health(node_config)
                    self._check_alerts(health)

                # 清理过期告警
                self._cleanup_old_alerts()

                # 等待下一次检查
                time.sleep(interval)

            except Exception as e:
                self.logger.error(f"监控循环异常: {e}")
                time.sleep(10)  # 异常后等待10秒再重试

    # 公开 API 方法

    def start(self, alert_callback: Callable = None):
        """
        启动监控

        Args:
            alert_callback: 告警回调函数
        """
        if self._running:
            self.logger.warning("监控已在运行")
            return

        self._alert_callback = alert_callback
        self._running = True

        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

        self.logger.info("网络监控已启动")

    def stop(self):
        """停止监控"""
        if not self._running:
            return

        self._running = False

        if self._monitor_thread:
            self._monitor_thread.join(timeout=10)

        self.logger.info("网络监控已停止")

    def get_node_health(self, node_name: str = None) -> Dict[str, Any]:
        """
        获取节点健康状态

        Args:
            node_name: 节点名称，None 表示获取所有节点

        Returns:
            节点健康状态
        """
        if node_name:
            health = self._node_health.get(node_name)
            if health:
                return asdict(health)
            return None

        return [asdict(h) for h in self._node_health.values()]

    def get_alerts(self, level: str = None, acknowledged: bool = None) -> List[Dict[str, Any]]:
        """
        获取告警列表

        Args:
            level: 告警级别过滤
            acknowledged: 是否已确认过滤

        Returns:
            告警列表
        """
        with self._alerts_lock:
            alerts = self._alerts

            if level:
                alerts = [a for a in alerts if a.level == level]

            if acknowledged is not None:
                alerts = [a for a in alerts if a.acknowledged == acknowledged]

            return [asdict(a) for a in alerts]

    def acknowledge_alert(self, alert_id: str) -> bool:
        """
        确认告警

        Args:
            alert_id: 告警 ID

        Returns:
            是否成功
        """
        with self._alerts_lock:
            for alert in self._alerts:
                if alert.id == alert_id:
                    alert.acknowledged = True
                    self.logger.info(f"告警已确认: {alert_id}")
                    return True

        return False

    def get_performance_stats(self, node_name: str, minutes: int = 60) -> Dict[str, Any]:
        """
        获取性能统计

        Args:
            node_name: 节点名称
            minutes: 统计时长（分钟）

        Returns:
            性能统计数据
        """
        if node_name not in self._performance_history:
            return {'error': f'节点不存在: {node_name}'}

        history = list(self._performance_history[node_name])
        cutoff = datetime.now() - timedelta(minutes=minutes)
        recent_history = [
            h for h in history
            if datetime.fromisoformat(h['timestamp'].replace('Z', '+00:00')) > cutoff
        ]

        if not recent_history:
            return {'error': '没有可用的性能数据'}

        # 计算统计指标
        response_times = [h['response_time_ms'] for h in recent_history]
        success_count = sum(1 for h in recent_history if h['status'] == 'success')

        return {
            'node': node_name,
            'period_minutes': minutes,
            'total_requests': len(recent_history),
            'success_count': success_count,
            'success_rate': success_count / len(recent_history) if recent_history else 0,
            'avg_response_time_ms': sum(response_times) / len(response_times),
            'min_response_time_ms': min(response_times),
            'max_response_time_ms': max(response_times),
            'p95_response_time_ms': sorted(response_times)[int(len(response_times) * 0.95)] if response_times else 0,
            'p99_response_time_ms': sorted(response_times)[int(len(response_times) * 0.99)] if response_times else 0
        }

    def get_summary(self) -> Dict[str, Any]:
        """获取监控摘要"""
        healthy_count = sum(1 for h in self._node_health.values() if h.status == NodeStatus.HEALTHY)
        unhealthy_count = sum(1 for h in self._node_health.values() if h.status == NodeStatus.UNHEALTHY)
        degraded_count = sum(1 for h in self._node_health.values() if h.status == NodeStatus.DEGRADED)

        # 必要节点状态
        required_nodes = [h for h in self._node_health.values() if h.required]
        required_healthy = sum(1 for h in required_nodes if h.status == NodeStatus.HEALTHY)

        with self._alerts_lock:
            unacknowledged_alerts = sum(1 for a in self._alerts if not a.acknowledged)
            critical_alerts = sum(1 for a in self._alerts if a.level == 'critical' and not a.acknowledged)

        return {
            'status': 'running' if self._running else 'stopped',
            'nodes': {
                'total': len(self._node_health),
                'healthy': healthy_count,
                'unhealthy': unhealthy_count,
                'degraded': degraded_count,
                'required': {
                    'total': len(required_nodes),
                    'healthy': required_healthy
                }
            },
            'alerts': {
                'unacknowledged': unacknowledged_alerts,
                'critical': critical_alerts
            },
            'timestamp': datetime.now().isoformat()
        }


# 全局实例
_network_monitor = None


def get_network_monitor(nodes: List[Dict[str, Any]] = None,
                        config: Dict[str, Any] = None) -> NetworkMonitor:
    """获取网络监控实例"""
    global _network_monitor

    if _network_monitor is None:
        if nodes is None:
            nodes = []
        _network_monitor = NetworkMonitor(nodes, config)

    return _network_monitor


# 命令行测试
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='网络监控测试')
    parser.add_argument('--nodes', type=str, help='节点配置JSON')
    parser.add_argument('--health', type=str, help='查询指定节点健康状态')
    parser.add_argument('--alerts', action='store_true', help='查看告警')
    parser.add_argument('--summary', action='store_true', help='查看摘要')

    args = parser.parse_args()

    # 加载节点配置
    nodes_config = []
    if args.nodes:
        nodes_config = json.loads(args.nodes)
    else:
        # 默认节点配置
        nodes_config = [
            {
                'name': 'host1',
                'base_url': 'http://localhost:8080/jira_proxy',
                'enabled': True
            }
        ]

    monitor = get_network_monitor(nodes_config)

    if args.health:
        health = monitor.get_node_health(args.health)
        print(json.dumps(health, ensure_ascii=False, indent=2))
    elif args.alerts:
        alerts = monitor.get_alerts()
        print(json.dumps(alerts, ensure_ascii=False, indent=2))
    elif args.summary:
        summary = monitor.get_summary()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("使用 --health <node> 查看节点状态, --alerts 查看告警, --summary 查看摘要")
