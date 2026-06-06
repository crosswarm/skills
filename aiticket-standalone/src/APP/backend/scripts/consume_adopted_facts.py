#!/usr/bin/env python3
"""
消费 adopted_facts_pending.json → product_facts.md

从 adopted_facts_pending 取出待审核规则写入 product_facts.md 并触发重索引。
JobMaster 调度：hourly-adopted-facts-consume.json（每小时 :00）
"""
import os
import sys

BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(BACKEND))

from services.adopted_facts_consumer import consume_pending_facts

if __name__ == "__main__":
    n = consume_pending_facts()
    print(f"[ConsumeAdoptedFacts] 写入 {n} 条规则到 product_facts.md")
