"""
API 搜索模块 - 提供 ZStack API 的搜索功能

通过驼峰拆分建立倒排索引，支持模糊匹配和部分匹配
"""

import json
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ApiInfo:
    """API 信息"""
    name: str  # 简短名称，如 QueryVmInstance
    full_name: str  # 完整 API 名称，如 org.zstack.header.vm.APIQueryVmInstanceMsg
    description: str
    category: str
    call_type: str  # sync 或 async
    parameters: list[dict]
    response_name: str
    primitive_fields: list[str] = field(default_factory=list)  # 可查询/排序的字段
    
    # 搜索用的 tokens（驼峰拆分后的小写词）
    tokens: list[str] = field(default_factory=list)


class ApiSearchIndex:
    """API 搜索索引"""
    
    def __init__(self):
        self.apis: dict[str, ApiInfo] = {}  # name -> ApiInfo
        self.inverted_index: dict[str, set[str]] = {}  # token -> set of api names
        self.categories: set[str] = set()
    
    def load_from_file(self, file_path: str | Path) -> None:
        """从 JSON 文件加载 API 文档并构建索引"""
        with open(file_path, 'r', encoding='utf-8') as f:
            api_docs = json.load(f)
        
        for name, info in api_docs.items():
            api_info = ApiInfo(
                name=name,
                full_name=info.get('apiName', ''),
                description=info.get('description', ''),
                category=info.get('category', 'other'),
                call_type=info.get('callType', 'sync'),
                parameters=info.get('parameters', []),
                response_name=info.get('responseName', ''),
                primitive_fields=info.get('primitive_fields', []),
            )
            
            # 驼峰拆分建立 tokens
            api_info.tokens = self._split_camel_case(name)
            
            self.apis[name] = api_info
            self.categories.add(api_info.category)
            
            # 建立倒排索引
            for token in api_info.tokens:
                if token not in self.inverted_index:
                    self.inverted_index[token] = set()
                self.inverted_index[token].add(name)
    
    def _split_camel_case(self, name: str) -> list[str]:
        """
        驼峰拆分
        如 QueryVmInstance -> ["query", "vm", "instance"]
        """
        # 在大写字母前插入空格，然后拆分
        words = re.sub(r'([A-Z])', r' \1', name).split()
        return [w.lower() for w in words if w]
    
    def search(
        self,
        keywords: list[str],
        category: Optional[str] = None,
        limit: int = 15
    ) -> list[dict]:
        """
        搜索 API
        
        Args:
            keywords: 搜索关键词列表
            category: 可选的分类过滤
            limit: 最多返回数量
            
        Returns:
            匹配的 API 列表
        """
        if not keywords:
            return []
        
        # 将关键词转为小写
        keywords_lower = [kw.lower() for kw in keywords]
        
        # 找出包含所有关键词的 API（交集）
        matched_apis: Optional[set[str]] = None
        
        for kw in keywords_lower:
            # 查找包含该关键词的所有 API
            matching_names: set[str] = set()
            
            # 1. 首先检查是否直接匹配 API 名称
            for api_name in self.apis.keys():
                if kw in api_name.lower():
                    matching_names.add(api_name)
            
            # 2. 然后检查倒排索引中的 token
            for token, names in self.inverted_index.items():
                # 支持部分匹配（关键词是 token 的前缀或包含关系）
                if token.startswith(kw) or kw in token:
                    matching_names.update(names)
            
            if matched_apis is None:
                matched_apis = matching_names
            else:
                matched_apis = matched_apis.intersection(matching_names)
        
        if not matched_apis:
            return []
        
        # 按分类过滤
        if category:
            category_lower = category.lower()
            matched_apis = {
                name for name in matched_apis
                if self.apis[name].category.lower() == category_lower
            }
        
        # 计算匹配分数并排序
        scored_results: list[tuple[str, float]] = []
        for name in matched_apis:
            api = self.apis[name]
            score = self._calculate_score(api, keywords_lower)
            scored_results.append((name, score))
        
        # 按分数降序排序
        scored_results.sort(key=lambda x: x[1], reverse=True)
        
        # 返回结果
        results = []
        for name, score in scored_results[:limit]:
            api = self.apis[name]
            results.append({
                'name': api.name,
                'description': api.description,
                'category': api.category,
                'callType': api.call_type,
            })
        
        return results
    
    def _calculate_score(self, api: ApiInfo, keywords: list[str]) -> float:
        """
        计算 API 与关键词的匹配分数
        
        分数规则：
        - API 名称完全匹配得 100 分（最高优先级）
        - API 名称包含关键词得 10 分
        - 完全匹配 token 得 2 分
        - 前缀匹配得 1.5 分
        - 包含匹配得 1 分
        - 描述中包含关键词得 0.5 分
        - token 数量越少越好（更精确匹配）
        """
        score = 0.0
        api_name_lower = api.name.lower()
        
        for kw in keywords:
            # 检查 API 名称是否完全等于关键词
            if api_name_lower == kw:
                score += 100.0
            # 检查 API 名称是否包含关键词
            elif kw in api_name_lower:
                score += 10.0
            
            # 检查 tokens 中的匹配
            for token in api.tokens:
                if token == kw:
                    score += 2.0  # 完全匹配
                elif token.startswith(kw):
                    score += 1.5  # 前缀匹配
                elif kw in token:
                    score += 1.0  # 包含匹配
            
            # 检查描述中的匹配
            if kw in api.description.lower():
                score += 0.5
        
        # 减少冗长名称的分数（更短的名称更可能是用户想要的）
        score -= len(api.tokens) * 0.1
        
        return score
    
    def get_api(self, name: str) -> Optional[ApiInfo]:
        """获取指定 API 的信息"""
        return self.apis.get(name)
    
    def get_api_detail(self, name: str) -> Optional[dict]:
        """
        获取 API 的极简信息，最大限度节省 Token
        """
        api = self.apis.get(name)
        if not api:
            return None
        
        result = {
            'api': api.name,
            'desc': api.description,
        }

        # 标准查询参数列表
        std_query_params = {'limit', 'start', 'count', 'groupBy', 'replyWithCount', 'filterName', 'sortBy', 'sortDirection', 'fields'}
        
        params = []
        is_query = api.name.startswith('Query')
        
        for p in api.parameters:
            # 对于查询 API，隐藏标准参数
            if is_query and p['name'] in std_query_params:
                continue
                
            param = {'name': p['name'], 'type': p['type']}
            if p.get('required'):
                param['req'] = True
            
            # 只有非标准且非 conditions 的参数才保留描述
            if not is_query or p['name'] != 'conditions':
                desc = p.get('description', '')
                if desc:
                    param['desc'] = desc
            params.append(param)

        result['params'] = params
        
        if is_query:
            if api.primitive_fields:
                result['fields'] = api.primitive_fields
            result['usage'] = (
                "Query: conditions=[{name,op,value}], fields is array. "
                "Ops: =,!=,>,<,>=,<=,?=(like,use %),like(compat),~=(regex,optional),in,is null. "
                "Sort: sortBy, sortDirection. "
                "Pagination: limit (default 50), start (offset). Use fields to reduce response size."
            )
        
        return result
    
    def list_categories(self) -> list[str]:
        """列出所有 API 分类"""
        return sorted(list(self.categories))

