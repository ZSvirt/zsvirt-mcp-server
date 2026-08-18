"""
监控指标搜索模块 - 提供 ZStack 监控指标的搜索功能
"""

import json
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MetricInfo:
    """监控指标信息"""
    name: str
    description: str
    namespace: str
    label_names: list[str]
    driver: str
    
    # 搜索用的 tokens
    tokens: list[str] = field(default_factory=list)


class MetricSearchIndex:
    """监控指标搜索索引"""
    
    def __init__(self):
        self.metrics: dict[str, MetricInfo] = {}  # metric_key -> MetricInfo
        self.inverted_index: dict[str, set[str]] = {}  # token -> set of metric keys
        self.namespaces: set[str] = set()
    
    def load_from_file(self, file_path: str | Path) -> None:
        """从 JSON 文件加载监控指标元数据并构建索引"""
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        metrics_list = data.get('metrics', [])
        
        for metric in metrics_list:
            name = metric.get('name', '')
            if not name:
                continue
            
            metric_info = MetricInfo(
                name=name,
                description=metric.get('description', ''),
                namespace=metric.get('namespace', ''),
                label_names=metric.get('labelNames', []),
                driver=metric.get('driver', ''),
            )
            
            # 驼峰拆分建立 tokens
            metric_info.tokens = self._split_camel_case(name)
            
            namespace = metric_info.namespace or ""
            metric_key = f"{namespace}::{name}"
            self.metrics[metric_key] = metric_info
            self.namespaces.add(metric_info.namespace)
            
            # 建立倒排索引
            for token in metric_info.tokens:
                if token not in self.inverted_index:
                    self.inverted_index[token] = set()
                self.inverted_index[token].add(metric_key)
    
    def _split_camel_case(self, name: str) -> list[str]:
        """
        驼峰拆分
        如 CPUUsedUtilization -> ["cpu", "used", "utilization"]
        """
        # 处理连续大写字母的情况，如 CPU -> cpu
        # 在大写字母后面跟小写字母前插入空格
        name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)
        # 在小写字母后面跟大写字母前插入空格
        name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
        words = name.split()
        return [w.lower() for w in words if w]
    
    def search(
        self,
        keywords: list[str],
        namespace: Optional[str] = None,
        limit: int = 20,
        match_mode: str = "or",
        prefer_namespaces: Optional[list[str]] = None,
    ) -> list[dict]:
        """
        搜索监控指标
        
        Args:
            keywords: 搜索关键词列表
            namespace: 可选的命名空间过滤
            limit: 最多返回数量
            match_mode: 关键词匹配模式，"and" 或 "or"，默认 "or"
            prefer_namespaces: 优先排序的命名空间列表（精确匹配，大小写不敏感）
            
        Returns:
            匹配的监控指标列表
        """
        if not keywords:
            return []

        mode = (match_mode or "or").lower().strip()
        if mode not in ("and", "or"):
            mode = "or"
        
        # 将关键词转为小写
        keywords_lower = [kw.lower() for kw in keywords]
        
        matched_metrics: set[str] = set()
        first = True
        
        for kw in keywords_lower:
            # 查找包含该关键词的所有指标
            matching_names: set[str] = set()
            
            # 1. 首先检查是否直接匹配指标名称
            for metric_key, metric in self.metrics.items():
                if kw in metric.name.lower():
                    matching_names.add(metric_key)
            
            # 2. 然后检查倒排索引中的 token
            for token, names in self.inverted_index.items():
                # 支持部分匹配
                if token.startswith(kw) or kw in token:
                    matching_names.update(names)
            
            if mode == "and":
                if first:
                    matched_metrics = matching_names
                    first = False
                else:
                    matched_metrics = matched_metrics.intersection(matching_names)
            else:
                matched_metrics.update(matching_names)
        
        if not matched_metrics:
            return []
        
        # 按命名空间过滤（支持模糊匹配）
        if namespace:
            namespace_lower = namespace.lower().strip()
            matched_metrics = {
                name for name in matched_metrics
                    if namespace_lower in self.metrics[name].namespace.lower()
            }
        
        # 计算匹配分数并排序
        prefer_map: dict[str, int] = {}
        if prefer_namespaces:
            for idx, ns in enumerate(prefer_namespaces):
                ns_lower = str(ns).strip().lower()
                if ns_lower and ns_lower not in prefer_map:
                    prefer_map[ns_lower] = idx
        scored_results: list[tuple[str, float, Optional[int]]] = []
        for name in matched_metrics:
            metric = self.metrics[name]
            score = self._calculate_score(metric, keywords_lower)
            prefer_rank = prefer_map.get(metric.namespace.lower()) if prefer_map else None
            scored_results.append((name, score, prefer_rank))
        
        # 按偏好命名空间优先，再按分数降序排序
        if prefer_map:
            scored_results.sort(
                key=lambda x: (
                    1 if x[2] is None else 0,
                    x[2] if x[2] is not None else 0,
                    -x[1],
                )
            )
        else:
            scored_results.sort(key=lambda x: x[1], reverse=True)
        
        # 返回结果
        results = []
        for name, _score, _rank in scored_results[:limit]:
            metric = self.metrics[name]
            results.append({
                'name': metric.name,
                'description': metric.description,
                'namespace': metric.namespace,
                'labelNames': metric.label_names,
            })
        
        return results
    
    def _calculate_score(self, metric: MetricInfo, keywords: list[str]) -> float:
        """计算指标与关键词的匹配分数"""
        score = 0.0
        metric_name_lower = metric.name.lower()
        namespace_lower = metric.namespace.lower()
        namespace_segments = [seg for seg in namespace_lower.split("/") if seg]
        
        for kw in keywords:
            # 检查指标名称是否完全等于关键词
            if metric_name_lower == kw:
                score += 100.0
            # 检查指标名称是否包含关键词
            elif kw in metric_name_lower:
                score += 10.0
            
            for token in metric.tokens:
                if token == kw:
                    score += 2.0
                elif token.startswith(kw):
                    score += 1.5
                elif kw in token:
                    score += 1.0
            
            if kw in metric.description.lower():
                score += 0.5

            # namespace 参与评分（优先精确段匹配）
            if kw in namespace_segments:
                score += 4.0
            elif any(seg.startswith(kw) or kw in seg for seg in namespace_segments):
                score += 1.0
        
        # 减少冗长名称的分数
        score -= len(metric.tokens) * 0.1
        
        return score
    
    def get_metric(self, name: str) -> Optional[MetricInfo]:
        """获取指定指标的信息"""
        if name in self.metrics:
            return self.metrics.get(name)
        for metric in self.metrics.values():
            if metric.name == name:
                return metric
        return None
    
    def list_namespaces(self) -> list[str]:
        """列出所有命名空间"""
        return sorted(list(self.namespaces))

