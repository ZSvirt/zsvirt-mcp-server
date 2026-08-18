"""
ZStack API 客户端 - 处理与 ZStack Cloud 的 API 通信

支持两种认证方式:
1. 用户名密码登录获取 Session
2. 直接传入 SessionID（通过环境变量 ZSTACK_SESSION_ID）

支持:
- 自动登录和 session 管理
- 同步和异步 API 调用
- 异步 API 的 Job 轮询
"""

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional
from dataclasses import dataclass

import httpx


class ZStackApiError(Exception):
    """ZStack API 错误"""
    def __init__(self, message: str, code: Optional[str] = None, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass
class ZStackSession:
    """ZStack 会话信息"""
    uuid: str
    account_uuid: str = ""
    user_uuid: str = ""
    expire_date: Optional[str] = None


class ZStackClient:
    """
    ZStack API 客户端
    
    认证方式（按优先级）:
    1. 如果设置了 ZSTACK_SESSION_ID，直接使用该 Session
    2. 否则使用 ZSTACK_ACCOUNT + ZSTACK_PASSWORD 登录获取 Session
    """
    
    # 轮询 Job 的配置
    JOB_POLL_INTERVAL = 1.0  # 秒
    JOB_POLL_MAX_RETRIES = 300  # 最多轮询次数（5分钟）
    
    def __init__(
        self,
        api_url: Optional[str] = None,
        account: Optional[str] = None,
        password: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        """
        初始化 ZStack 客户端
        
        Args:
            api_url: ZStack API 地址，如 http://localhost:8080
            account: 账户名（用户名密码认证时使用）
            password: 密码（明文，会自动进行 SHA512 加密）
            session_id: 直接传入的 Session UUID（优先级高于用户名密码）
        """
        self.api_url = api_url or os.environ.get('ZSTACK_API_URL', 'http://localhost:8080')
        self.account = account or os.environ.get('ZSTACK_ACCOUNT', 'admin')
        self.password = password or os.environ.get('ZSTACK_PASSWORD', '')
        
        # 优先使用直接传入的 session_id
        env_session_id = session_id or os.environ.get('ZSTACK_SESSION_ID', '')
        
        # 如果有 session_id，直接创建 session 对象
        if env_session_id:
            self.session: Optional[ZStackSession] = ZStackSession(uuid=env_session_id)
        else:
            self.session = None
        
        self._http_client: Optional[httpx.AsyncClient] = None
    
    @property
    def api_endpoint(self) -> str:
        """API 端点地址"""
        return f"{self.api_url.rstrip('/')}/zstack/api/"
    
    @property
    def auth_mode(self) -> str:
        """当前认证模式"""
        if self.session and self.session.uuid:
            if not self.session.account_uuid:
                return "session_id"  # 直接传入的 session
            return "session"  # 登录获取的 session
        return "password"  # 需要密码登录
    
    async def _get_http_client(self) -> httpx.AsyncClient:
        """获取 HTTP 客户端（懒加载）"""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=60.0)
        return self._http_client
    
    async def logout(self) -> None:
        """调用 LogOut API 销毁当前 session，然后关闭 HTTP 客户端"""
        if self.session and self.session.uuid:
            try:
                await self.execute(
                    "LogOut",
                    "org.zstack.header.identity.APILogOutMsg",
                    {"sessionUuid": self.session.uuid},
                )
            except Exception:
                pass  # best-effort，忽略 logout 失败
            self.session = None
        await self.close()

    async def close(self) -> None:
        """关闭客户端"""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
    
    @staticmethod
    def _sha512(text: str) -> str:
        """SHA512 加密"""
        return hashlib.sha512(text.encode('utf-8')).hexdigest()

    @staticmethod
    def _normalize_metric_time(value: Any) -> Any:
        """将时间规范化为秒级时间戳（支持 ISO 字符串/毫秒/秒）"""
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > 10_000_000_000:
                return int(value // 1000)
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.isdigit():
                num = int(text)
                if num > 10_000_000_000:
                    num = num // 1000
                return num
            try:
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                if " " in text and "T" not in text:
                    text = text.replace(" ", "T")
                dt = datetime.fromisoformat(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except Exception:
                return value
        return value

    @staticmethod
    def _normalize_metric_period(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
        return value

    @staticmethod
    def _normalize_metric_labels(labels: Any) -> Optional[list[str]]:
        """统一 labels 为字符串列表，支持 dict / list[str] / list[dict]"""
        if labels is None:
            return None
        if isinstance(labels, dict):
            return [f"{key}={value}" for key, value in labels.items()]
        if isinstance(labels, (list, tuple, set)):
            results: list[str] = []
            for item in labels:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        results.append(text)
                    continue
                if isinstance(item, dict):
                    key = item.get("key") or item.get("name")
                    if key is not None and "value" in item:
                        results.append(f"{key}={item.get('value')}")
                        continue
                    if key is not None and "val" in item:
                        results.append(f"{key}={item.get('val')}")
                        continue
                    if "label" in item:
                        results.append(str(item.get("label")))
                        continue
                results.append(str(item))
            return results or None
        return [str(labels)]
    
    def _parse_response(self, response_data: dict) -> dict[str, Any]:
        """
        解析 ZStack API 响应
        
        ZStack 返回格式有两种:
        1. 直接返回: {"org.zstack.xxx.Reply": {...}}
        2. 包装返回: {"state": "Done", "result": "{...json string...}"}
        
        Args:
            response_data: API 响应 JSON
            
        Returns:
            解析后的实际数据
        """
        # 检查是否是包装格式
        if 'state' in response_data and 'result' in response_data:
            state = response_data.get('state')
            
            # 检查状态
            if state == 'Error' or 'error' in response_data:
                error_msg = response_data.get('error', {})
                if isinstance(error_msg, str):
                    raise ZStackApiError(message=error_msg)
                raise ZStackApiError(
                    message=error_msg.get('description', '请求失败'),
                    code=error_msg.get('code'),
                    details=error_msg
                )
            
            # 解析 result JSON 字符串
            result_str = response_data.get('result', '{}')
            if isinstance(result_str, str):
                try:
                    result = json.loads(result_str)
                except json.JSONDecodeError:
                    return {'raw': result_str}
            else:
                result = result_str
            
            # 提取实际数据
            if result:
                reply_key = list(result.keys())[0]
                reply_data = result[reply_key]
                self._raise_if_reply_error(reply_data)
                return reply_data
            return result
        
        # 直接格式：检查错误
        if 'error' in response_data:
            error = response_data['error']
            raise ZStackApiError(
                message=error.get('description', '请求失败'),
                code=error.get('code'),
                details=error
            )
        
        # 提取实际数据
        if response_data:
            reply_key = list(response_data.keys())[0]
            reply_data = response_data[reply_key]
            self._raise_if_reply_error(reply_data)
            return reply_data
        
        return response_data

    @staticmethod
    def _raise_if_reply_error(reply_data: Any) -> None:
        if not isinstance(reply_data, dict):
            return
        if reply_data.get('success') is False and 'error' in reply_data:
            error = reply_data.get('error')
            if isinstance(error, dict):
                raise ZStackApiError(
                    message=error.get('description', '请求失败'),
                    code=error.get('code'),
                    details=error,
                )
            raise ZStackApiError(message=str(error or '请求失败'))

    @staticmethod
    def _is_session_invalid_error(error: ZStackApiError) -> bool:
        if not error:
            return False
        code = getattr(error, 'code', None)
        details = getattr(error, 'details', None)
        if code in ('ID.1001', 'ORG_ZSTACK_IDENTITY_10020'):
            return True
        if isinstance(details, dict):
            if details.get('code') in ('ID.1001', 'ORG_ZSTACK_IDENTITY_10020'):
                return True
            if details.get('globalErrorCode') in ('ORG_ZSTACK_IDENTITY_10020',):
                return True
            detail_text = str(details.get('details') or details.get('description') or '').lower()
            if 'session' in detail_text and ('invalid' in detail_text or 'expired' in detail_text):
                return True
        message = str(error).lower()
        return 'session' in message and ('invalid' in message or 'expired' in message)

    def _can_refresh_session(self) -> bool:
        if self.auth_mode == "session_id":
            return False
        return bool(self.password)

    async def _refresh_session(self) -> None:
        self.session = None
        await self.login()
    
    async def login(self) -> ZStackSession:
        """
        登录 ZStack 获取 session
        
        Returns:
            ZStackSession 对象
        """
        if not self.password:
            raise ZStackApiError("密码未配置，请设置 ZSTACK_PASSWORD 环境变量，或设置 ZSTACK_SESSION_ID 直接使用已有会话")
        
        password_hash = self._sha512(self.password)
        
        request_body = {
            "org.zstack.header.identity.APILogInByAccountMsg": {
                "accountName": self.account,
                "password": password_hash,
            }
        }
        
        client = await self._get_http_client()
        response = await client.post(
            self.api_endpoint,
            json=request_body,
            headers={"Content-Type": "application/json"}
        )
        
        # 检查 HTTP 状态码
        if response.status_code >= 400:
            raise ZStackApiError(
                message=f"HTTP 错误 {response.status_code}: {response.text[:500]}",
                code=str(response.status_code),
            )
        
        try:
            result = response.json()
        except Exception as e:
            raise ZStackApiError(
                message=f"响应解析失败: {str(e)}, 响应内容: {response.text[:500]}",
            )
        
        reply_data = self._parse_response(result)
        
        # 提取 session 信息
        session_data = reply_data.get('inventory', {})
        
        self.session = ZStackSession(
            uuid=session_data.get('uuid', ''),
            account_uuid=session_data.get('accountUuid', ''),
            user_uuid=session_data.get('userUuid', ''),
            expire_date=session_data.get('expiredDate'),
        )
        
        return self.session
    
    async def ensure_session(self) -> ZStackSession:
        """确保有有效的 session，如果没有则登录"""
        if self.session is None:
            await self.login()
        return self.session  # type: ignore
    
    async def execute(
        self,
        api_name: str,
        full_api_name: str,
        parameters: dict[str, Any],
        is_async: bool = False,
    ) -> dict[str, Any]:
        """
        执行 ZStack API
        
        Args:
            api_name: API 简称，如 QueryVmInstance
            full_api_name: 完整 API 名称，如 org.zstack.header.vm.APIQueryVmInstanceMsg
            parameters: API 参数
            is_async: 是否为异步 API
            
        Returns:
            API 返回结果
        """
        base_parameters = dict(parameters)

        async def send_once() -> dict[str, Any]:
            # 确保已登录（除了登录 API 本身）
            if 'LogIn' not in api_name:
                session = await self.ensure_session()
                # 添加 session 信息
                request_parameters = {
                    **base_parameters,
                    "session": {"uuid": session.uuid}
                }
            else:
                request_parameters = base_parameters

            # 构建请求体
            request_body = {
                full_api_name: request_parameters
            }
            
            client = await self._get_http_client()
            response = await client.post(
                self.api_endpoint,
                json=request_body,
                headers={"Content-Type": "application/json"}
            )
            
            # 检查 HTTP 状态码
            if response.status_code >= 400:
                raise ZStackApiError(
                    message=f"HTTP 错误 {response.status_code}: {response.text[:500]}",
                    code=str(response.status_code),
                )
            
            try:
                result = response.json()
            except Exception as e:
                raise ZStackApiError(
                    message=f"响应解析失败: {str(e)}, 响应内容: {response.text[:500]}",
                )
            
            # 检查是否需要轮询 Job（异步 API）
            # 包装格式下，如果 state 不是 Done，需要轮询
            if 'state' in result:
                state = result.get('state')
                if state not in ('Done', 'Error'):
                    # 需要轮询
                    location = result.get('location')
                    if not location and result.get('uuid'):
                        location = f"{self.api_endpoint}result/{result['uuid']}"
                    if location:
                        return await self._poll_job(location)
            
            return self._parse_response(result)

        try:
            return await send_once()
        except ZStackApiError as e:
            if (
                'LogIn' not in api_name
                and self._is_session_invalid_error(e)
                and self._can_refresh_session()
            ):
                await self._refresh_session()
                return await send_once()
            raise
    
    async def _poll_job(self, job_location: str) -> dict[str, Any]:
        """
        轮询异步 Job 直到完成
        
        Args:
            job_location: Job 查询地址
            
        Returns:
            Job 的最终结果
        """
        client = await self._get_http_client()
        
        for _ in range(self.JOB_POLL_MAX_RETRIES):
            await asyncio.sleep(self.JOB_POLL_INTERVAL)
            
            response = await client.get(
                job_location,
                headers={"Content-Type": "application/json"}
            )
            
            # 检查 HTTP 状态码
            if response.status_code >= 400:
                raise ZStackApiError(
                    message=f"Job 查询失败, HTTP {response.status_code}: {response.text[:500]}",
                    code=str(response.status_code),
                )
            
            try:
                result = response.json()
            except Exception as e:
                raise ZStackApiError(
                    message=f"Job 响应解析失败: {str(e)}, 响应内容: {response.text[:500]}",
                )
            
            # 检查状态
            if 'state' in result:
                state = result.get('state')
                if state == 'Done':
                    return self._parse_response(result)
                elif state == 'Error':
                    error = result.get('error', {})
                    raise ZStackApiError(
                        message=error.get('description', 'Job 执行失败') if isinstance(error, dict) else str(error),
                        code=error.get('code') if isinstance(error, dict) else None,
                        details=error if isinstance(error, dict) else None
                    )
                # 其他状态继续轮询
            else:
                # 非包装格式，直接返回
                return self._parse_response(result)
        
        raise ZStackApiError("Job 执行超时，请稍后重试")
    
    async def query_metric_data(
        self,
        namespace: str,
        metric_name: str,
        start_time: Any = None,
        end_time: Any = None,
        period: Any = 60,
        labels: Any = None,
    ) -> dict[str, Any]:
        """
        查询监控数据
        
        使用 ZStack 的 GetMetricData API
        
        Args:
            namespace: 命名空间，如 ZStack/VM
            metric_name: 指标名称
            start_time: 开始时间（ISO 或秒级时间戳）
            end_time: 结束时间（ISO 或秒级时间戳）
            period: 采样周期（秒）
            labels: 标签过滤，如 ["VMUuid=xxx"] 或 {"VMUuid":"xxx"}
            
        Returns:
            监控数据
        """
        start_time = self._normalize_metric_time(start_time)
        end_time = self._normalize_metric_time(end_time)
        period = self._normalize_metric_period(period)
        labels = self._normalize_metric_labels(labels)

        async def send_once() -> dict[str, Any]:
            session = await self.ensure_session()
            payload = {
                "session": {"uuid": session.uuid},
                "namespace": namespace,
                "metricName": metric_name,
                "startTime": start_time,
                "endTime": end_time,
                "period": period,
                "labels": labels,
            }
            payload = {key: value for key, value in payload.items() if value is not None}
            request_body = {
                "org.zstack.zwatch.api.APIGetMetricDataMsg": payload
            }
            
            client = await self._get_http_client()
            response = await client.post(
                self.api_endpoint,
                json=request_body,
                headers={"Content-Type": "application/json"}
            )
            
            # 检查 HTTP 状态码
            if response.status_code >= 400:
                raise ZStackApiError(
                    message=f"HTTP 错误 {response.status_code}: {response.text[:500]}",
                    code=str(response.status_code),
                )
            
            try:
                result = response.json()
            except Exception as e:
                raise ZStackApiError(
                    message=f"响应解析失败: {str(e)}, 响应内容: {response.text[:500]}",
                )
            
            return self._parse_response(result)

        try:
            return await send_once()
        except ZStackApiError as e:
            if self._is_session_invalid_error(e) and self._can_refresh_session():
                await self._refresh_session()
                return await send_once()
            raise
