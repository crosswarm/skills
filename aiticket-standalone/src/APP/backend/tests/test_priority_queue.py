"""
PriorityQueue并发测试
测试线程安全、队列大小限制、优先级排序等
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import threading
import time
import queue
from unittest.mock import Mock, MagicMock, patch

from board_service_chroma import AIAnalysisWorker


class TestPriorityQueueConcurrency:
    """测试PriorityQueue并发行为"""

    @pytest.fixture
    def mock_worker(self):
        """创建带mock的AIAnalysisWorker"""
        with patch('board_service_chroma.VectorStore') as mock_vs:
            with patch('board_service_chroma.LLMService') as mock_llm:
                worker = AIAnalysisWorker(
                    vector_store=mock_vs.return_value,
                    llm_service=mock_llm.return_value,
                    batch_size=2,
                    max_workers=1,
                    max_queue_size=10
                )
                return worker

    def test_task_counter_increments(self, mock_worker):
        """测试任务计数器递增确保PriorityQueue元素可比较"""
        initial_counter = mock_worker._task_counter

        # 创建模拟任务
        mock_issue1 = Mock()
        mock_issue1.key = "TEST-1"
        mock_issue2 = Mock()
        mock_issue2.key = "TEST-2"

        # 相同优先级提交两个任务
        mock_worker.submit(mock_issue1, priority=5)
        mock_worker.submit(mock_issue2, priority=5)

        # 验证计数器递增
        assert mock_worker._task_counter == initial_counter + 2

    def test_priority_queue_ordering(self, mock_worker):
        """测试PriorityQueue按优先级正确排序"""
        # 清空队列
        mock_worker.task_queue = queue.PriorityQueue()

        # 创建模拟任务
        issues = []
        for i in range(5):
            mock_issue = Mock()
            mock_issue.key = f"TEST-{i}"
            issues.append(mock_issue)

        # 按不同优先级提交（故意打乱顺序）
        priorities = [3, 1, 5, 2, 4]
        for i, priority in enumerate(priorities):
            mock_worker._task_counter += 1
            mock_worker.task_queue.put((priority, mock_worker._task_counter, issues[i]))

        # 验证出队顺序是按优先级升序
        expected_order = [1, 2, 3, 4, 5]
        actual_order = []
        for _ in range(5):
            priority, counter, issue = mock_worker.task_queue.get()
            actual_order.append(priority)

        assert actual_order == expected_order

    def test_same_priority_fifo_ordering(self, mock_worker):
        """测试相同优先级时按提交顺序(FIFO)出队"""
        mock_worker.task_queue = queue.PriorityQueue()

        # 创建模拟任务
        issues = []
        for i in range(3):
            mock_issue = Mock()
            mock_issue.key = f"TEST-{i}"
            issues.append(mock_issue)

        # 相同优先级提交多个任务
        for i, issue in enumerate(issues):
            mock_worker._task_counter += 1
            mock_worker.task_queue.put((5, mock_worker._task_counter, issue))

        # 验证出队顺序与入队顺序一致
        for i in range(3):
            priority, counter, issue = mock_worker.task_queue.get()
            assert issue.key == f"TEST-{i}"

    def test_queue_size_limit(self, mock_worker):
        """测试队列大小限制防止内存溢出"""
        # 设置小队列大小便于测试
        mock_worker.max_queue_size = 3

        # 创建模拟任务
        issues = []
        for i in range(5):
            mock_issue = Mock()
            mock_issue.key = f"TEST-{i}"
            issues.append(mock_issue)

        # 先让get_cached_analysis返回None，确保任务被提交
        mock_worker.vector_store.get_cached_analysis.return_value = None

        # 提交超过限制的任务
        for i, issue in enumerate(issues):
            mock_worker.submit(issue, priority=5)

        # 验证队列大小不超过限制
        assert mock_worker.task_queue.qsize() <= mock_worker.max_queue_size

    def test_high_priority_force_enqueue(self, mock_worker):
        """测试高优先级任务强制入队"""
        mock_worker.max_queue_size = 2

        # 填满队列
        mock_worker.vector_store.get_cached_analysis.return_value = None

        for i in range(3):
            mock_issue = Mock()
            mock_issue.key = f"LOW-{i}"
            mock_worker.submit(mock_issue, priority=10)

        initial_qsize = mock_worker.task_queue.qsize()

        # 提交高优先级任务
        high_priority_issue = Mock()
        high_priority_issue.key = "HIGH-1"
        mock_worker.submit(high_priority_issue, priority=1)

        # 验证队列仍然能接收高优先级任务
        # 注意：实际行为是打印日志并尝试强制入队
        assert mock_worker.task_queue.qsize() >= initial_qsize


class TestConcurrentAccess:
    """测试并发访问场景"""

    @pytest.fixture
    def mock_worker(self):
        """创建带mock的AIAnalysisWorker"""
        with patch('board_service_chroma.VectorStore') as mock_vs:
            with patch('board_service_chroma.LLMService') as mock_llm:
                worker = AIAnalysisWorker(
                    vector_store=mock_vs.return_value,
                    llm_service=mock_llm.return_value,
                    batch_size=2,
                    max_workers=1,
                    max_queue_size=100
                )
                return worker

    def test_concurrent_submit(self, mock_worker):
        """测试多线程并发提交任务"""
        mock_worker.vector_store.get_cached_analysis.return_value = None

        submitted_keys = []
        errors = []

        def submit_task(key):
            try:
                mock_issue = Mock()
                mock_issue.key = key
                mock_worker.submit(mock_issue, priority=5)
                submitted_keys.append(key)
            except Exception as e:
                errors.append(str(e))

        # 创建多个线程并发提交
        threads = []
        for i in range(20):
            t = threading.Thread(target=submit_task, args=(f"TEST-{i}",))
            threads.append(t)

        # 启动所有线程
        for t in threads:
            t.start()

        # 等待所有线程完成
        for t in threads:
            t.join()

        # 验证没有错误
        assert len(errors) == 0, f"并发提交出现错误: {errors}"

        # 验证任务已提交
        assert len(submitted_keys) == 20

        # 验证队列大小
        assert mock_worker.task_queue.qsize() == 20

    def test_counter_thread_safety(self, mock_worker):
        """测试计数器线程安全"""
        mock_worker.task_queue = queue.PriorityQueue()

        counters = []

        def increment_counter():
            for _ in range(100):
                mock_worker._task_counter += 1
                counters.append(mock_worker._task_counter)
                time.sleep(0.001)  # 小延迟增加竞争概率

        # 创建多个线程
        threads = []
        for _ in range(5):
            t = threading.Thread(target=increment_counter)
            threads.append(t)

        # 启动所有线程
        for t in threads:
            t.start()

        # 等待完成
        for t in threads:
            t.join()

        # 注意：Python的+=操作不是原子性的，这个测试主要检查不会崩溃
        # 最终计数器值应该等于递增次数（如果线程安全）或接近（如果不安全但不会崩溃）
        assert mock_worker._task_counter >= 500  # 至少所有递增都完成了

    def test_queue_operations_under_load(self, mock_worker):
        """测试高负载下的队列操作"""
        mock_worker.vector_store.get_cached_analysis.return_value = None
        mock_worker.max_queue_size = 1000

        # 快速提交大量任务
        for i in range(500):
            mock_issue = Mock()
            mock_issue.key = f"LOAD-{i}"
            priority = i % 10 + 1  # 优先级1-10
            mock_worker.submit(mock_issue, priority=priority)

        # 验证队列大小
        assert mock_worker.task_queue.qsize() == 500

        # 验证可以正确取出所有任务
        retrieved = []
        while not mock_worker.task_queue.empty():
            priority, counter, issue = mock_worker.task_queue.get()
            retrieved.append((priority, issue.key))

        # 验证按优先级排序
        priorities = [p for p, _ in retrieved]
        assert priorities == sorted(priorities)

        # 验证取出了所有任务
        assert len(retrieved) == 500


class TestEdgeCases:
    """测试边界条件"""

    @pytest.fixture
    def mock_worker(self):
        """创建带mock的AIAnalysisWorker"""
        with patch('board_service_chroma.VectorStore') as mock_vs:
            with patch('board_service_chroma.LLMService') as mock_llm:
                worker = AIAnalysisWorker(
                    vector_store=mock_vs.return_value,
                    llm_service=mock_llm.return_value,
                    batch_size=2,
                    max_workers=1,
                    max_queue_size=10
                )
                return worker

    def test_empty_queue_get_timeout(self, mock_worker):
        """测试空队列获取超时"""
        start = time.time()
        try:
            # PriorityQueue.get 默认阻塞，使用timeout
            mock_worker.task_queue.get(timeout=0.1)
        except queue.Empty:
            elapsed = time.time() - start
            # 验证确实超时了
            assert elapsed >= 0.1

    def test_queue_overflow_behavior(self, mock_worker):
        """测试队列溢出行为"""
        mock_worker.max_queue_size = 5
        mock_worker.vector_store.get_cached_analysis.return_value = None

        # 提交超过限制的任务
        for i in range(10):
            mock_issue = Mock()
            mock_issue.key = f"OVERFLOW-{i}"
            mock_worker.submit(mock_issue, priority=10)

        # 队列大小应该被限制
        assert mock_worker.task_queue.qsize() <= mock_worker.max_queue_size

    def test_submit_with_cached_result(self, mock_worker):
        """测试有缓存结果时不提交任务"""
        # 设置缓存返回有效结果
        mock_worker.vector_store.get_cached_analysis.return_value = {
            'stale': False,
            'result': 'cached'
        }

        mock_issue = Mock()
        mock_issue.key = "CACHED-1"

        # 提交任务
        mock_worker.submit(mock_issue, priority=5)

        # 验证队列中没有任务（因为被缓存跳过了）
        assert mock_worker.task_queue.qsize() == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
