# -*- coding: utf-8 -*-
"""
Gmail API 客户端
实现 OAuth 2.0 认证、邮件获取、筛选和标记已读功能
参考：https://developers.google.com/workspace/gmail/api/guides
"""
import os
import base64
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from email.utils import parsedate_to_datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .schema import ScholarSettings, EmailMetadata
from ..utils.logger import get_logger

logger = get_logger(__name__)


class GmailClient:
    """
    Gmail API 客户端
    
    功能：
    - OAuth 2.0 认证流程
    - 获取邮件列表
    - 获取邮件内容
    - 筛选 Google Scholar 邮件
    - 标记邮件为已读
    """
    
    # Google Scholar 邮件发件人或关键词
    SCHOLAR_SENDERS = [
        "scholaralerts-noreply@google.com",
        "scholar-alerts-noreply@google.com",
        "scholar-noreply@google.com",
    ]
    
    def __init__(self, settings: ScholarSettings):
        """
        初始化 Gmail 客户端
        
        Args:
            settings: Scholar Digest 设置对象
        """
        self.settings = settings
        self.credentials_path = settings.gmail.credentials_path
        self.token_path = settings.gmail.token_path
        self.scopes = settings.gmail.scopes
        
        self._creds: Optional[Credentials] = None
        self._service = None
        
    @property
    def service(self):
        """懒加载 Gmail API 服务"""
        if self._service is None:
            self._authenticate()
            self._service = build('gmail', 'v1', credentials=self._creds)
            logger.info("✅ Gmail API 服务已初始化")
        return self._service
    
    def _authenticate(self) -> None:
        """
        执行 OAuth 2.0 认证流程
        
        首次运行时会打开浏览器进行授权，
        之后会使用保存的 token 自动认证。
        """
        creds = None
        
        # 尝试加载已保存的 token
        if self.token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(self.token_path), 
                    self.scopes
                )
                logger.info("从 {} 加载已有凭据".format(self.token_path))
            except Exception as e:
                logger.warning("加载凭据失败: {}".format(e))
                creds = None
        
        # 检查凭据是否有效或需要刷新
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("凭据已刷新")
            except Exception as e:
                logger.warning("刷新凭据失败: {}".format(e))
                creds = None
        
        # 如果没有有效凭据，执行授权流程
        if not creds or not creds.valid:
            if not self.credentials_path.exists():
                raise FileNotFoundError(
                    "OAuth 凭据文件不存在: {}\n".format(self.credentials_path) +
                    "请从 Google Cloud Console 下载 OAuth 2.0 客户端凭据文件"
                )
            
            logger.info("🔐 开始 OAuth 2.0 授权流程...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_path),
                self.scopes
            )
            creds = flow.run_local_server(port=0)
            logger.info("✅ 授权成功")
            
            # 保存凭据以供下次使用
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.token_path, 'w') as token_file:
                token_file.write(creds.to_json())
            logger.info(f"💾 凭据已保存到 {self.token_path}")
        
        self._creds = creds
    
    def get_user_profile(self) -> Dict[str, Any]:
        """获取当前登录用户信息"""
        try:
            profile = self.service.users().getProfile(userId='me').execute()
            logger.info(f"👤 当前账户: {profile.get('emailAddress')}")
            return profile
        except HttpError as e:
            logger.error(f"❌ 获取用户信息失败: {e}")
            raise
    
    def list_messages(
        self,
        query: Optional[str] = None,
        max_results: int = 100,  # 0 表示不限制
        label_ids: Optional[List[str]] = None,
        include_spam_trash: bool = False
    ) -> List[Dict[str, Any]]:
        """
        获取邮件列表，支持无限分页
        
        Args:
            query: Gmail 搜索查询
            max_results: 最大返回数量 (0 表示不限制)
            label_ids: 标签过滤
            include_spam_trash: 是否包含垃圾邮件
            
        Returns:
            邮件ID和线程ID列表
        """
        messages = []
        page_token = None
        
        # 如果 max_results 为 0，设置为一个极大的值
        limit = max_results if max_results > 0 else 1000000 
        
        try:
            while len(messages) < limit:
                current_batch_size = min(limit - len(messages), 500)
                request_params = {
                    'userId': 'me',
                    'maxResults': current_batch_size,
                    'includeSpamTrash': include_spam_trash
                }
                
                if query:
                    request_params['q'] = query
                if label_ids:
                    request_params['labelIds'] = label_ids
                if page_token:
                    request_params['pageToken'] = page_token
                
                response = self.service.users().messages().list(**request_params).execute()
                
                if 'messages' in response:
                    messages.extend(response['messages'])
                    logger.debug(f"📬 获取到 {len(response['messages'])} 封邮件 (累计: {len(messages)})")
                
                page_token = response.get('nextPageToken')
                if not page_token:
                    break
            
            logger.info(f"📬 共获取 {len(messages)} 封邮件")
            return messages[:limit]
            
        except HttpError as e:
            logger.error(f"❌ 获取邮件列表失败: {e}")
            raise
    
    def get_message(self, message_id: str, format: str = 'full') -> Dict[str, Any]:
        """
        获取单封邮件的详细内容
        
        Args:
            message_id: 邮件ID
            format: 返回格式 ('minimal', 'full', 'raw', 'metadata')
            
        Returns:
            邮件详细信息
        """
        try:
            message = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format=format
            ).execute()
            return message
        except HttpError as e:
            logger.error(f"❌ 获取邮件 {message_id} 失败: {e}")
            raise
    
    def get_message_body(self, message: Dict[str, Any]) -> str:
        """
        提取邮件正文内容
        
        Args:
            message: 邮件对象
            
        Returns:
            邮件正文（HTML或纯文本）
        """
        body = ""
        payload = message.get('payload', {})
        
        # 处理不同的邮件结构
        if 'body' in payload and payload['body'].get('data'):
            # 简单邮件，正文直接在 body 中
            body = base64.urlsafe_b64decode(
                payload['body']['data']
            ).decode('utf-8', errors='ignore')
        elif 'parts' in payload:
            # 多部分邮件
            body = self._extract_body_from_parts(payload['parts'])
        
        return body
    
    def _extract_body_from_parts(self, parts: List[Dict]) -> str:
        """递归提取多部分邮件的正文"""
        html_body = ""
        text_body = ""
        
        for part in parts:
            mime_type = part.get('mimeType', '')
            
            if mime_type == 'text/html' and part.get('body', {}).get('data'):
                html_body = base64.urlsafe_b64decode(
                    part['body']['data']
                ).decode('utf-8', errors='ignore')
            elif mime_type == 'text/plain' and part.get('body', {}).get('data'):
                text_body = base64.urlsafe_b64decode(
                    part['body']['data']
                ).decode('utf-8', errors='ignore')
            elif 'parts' in part:
                # 递归处理嵌套部分
                nested_body = self._extract_body_from_parts(part['parts'])
                if nested_body:
                    if '<html' in nested_body.lower():
                        html_body = html_body or nested_body
                    else:
                        text_body = text_body or nested_body
        
        # 优先返回 HTML（Google Scholar 邮件通常是 HTML 格式）
        return html_body or text_body
    
    def parse_email_metadata(self, message: Dict[str, Any]) -> EmailMetadata:
        """
        解析邮件元数据
        
        Args:
            message: 邮件对象
            
        Returns:
            EmailMetadata 对象
        """
        headers = {
            h['name'].lower(): h['value'] 
            for h in message.get('payload', {}).get('headers', [])
        }
        
        # 解析时间
        received_at = datetime.now()
        internal_date = message.get('internalDate')
        if internal_date:
            received_at = datetime.fromtimestamp(int(internal_date) / 1000)
        elif 'date' in headers:
            try:
                received_at = parsedate_to_datetime(headers['date'])
            except:
                pass
        
        # 检查是否为 Google Scholar 邮件
        sender = headers.get('from', '')
        is_scholar = any(
            scholar_sender in sender.lower() 
            for scholar_sender in self.SCHOLAR_SENDERS
        )
        
        # 提取 Scholar Alert 的查询词（从主题中）
        subject = headers.get('subject', '')
        alert_query = None
        if is_scholar:
            # Google Scholar 邮件主题格式通常是 "Scholar Alert - <query>"
            if ' - ' in subject:
                alert_query = subject.split(' - ', 1)[1].strip()
        
        # 检查是否已读
        labels = message.get('labelIds', [])
        is_read = 'UNREAD' not in labels
        
        return EmailMetadata(
            email_id=message['id'],
            thread_id=message.get('threadId'),
            subject=subject,
            sender=sender,
            received_at=received_at,
            is_google_scholar=is_scholar,
            alert_query=alert_query,
            is_read=is_read,
            is_processed=False,
            papers_extracted=0
        )
    
    def fetch_scholar_emails(
        self,
        days: Optional[int] = 7,
        max_results: int = 100,  # 0 为不限制
        unread_only: bool = False,
        include_archived: bool = True,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取 Google Scholar 邮件

        Args:
            days: 获取最近 N 天的邮件，如果是 None 或 0 则获取所有历史邮件
            max_results: 最大返回数量 (0 为不限制)
            unread_only: 是否只获取未读邮件
            include_archived: 是否包含已存档邮件（不在收件箱中的）
            after: 起始日期 YYYY/MM/DD（历史区间回填；优先于 days）
            before: 结束日期 YYYY/MM/DD（不含当天，Gmail 语义）

        Returns:
            包含邮件内容和元数据的列表
        """
        # 构建查询
        query_parts = []
        
        # 发件人过滤
        sender_query = ' OR '.join([
            f'from:{sender}' for sender in self.SCHOLAR_SENDERS
        ])
        
        # 扩展主题关键词，涵盖引用提醒和相关研究
        subject_keywords = [
            "Scholar Alert", 
            "Google Scholar Alert",
            "new citations",
            "related research",
            "new results for",
            "Follow articles by"
        ]
        subject_query = ' OR '.join(['subject:"{}"'.format(kw) for kw in subject_keywords])
        
        query_parts.append('({} OR {})'.format(sender_query, subject_query))
        
        # 时间过滤：显式区间(after/before) 优先于相对 days（回填历史月份）
        if after or before:
            if after:
                query_parts.append('after:{}'.format(after))
            if before:
                query_parts.append('before:{}'.format(before))
            logger.info("过滤邮件区间: after={} before={}".format(after or '-', before or '-'))
        elif days and days > 0:
            after_date = (datetime.now() - timedelta(days=days)).strftime('%Y/%m/%d')
            query_parts.append('after:{}'.format(after_date))
            logger.info("过滤最近 {} 天的邮件".format(days))
        else:
            logger.info("获取所有历史邮件（无时间限制）")
        
        query = ' '.join(query_parts)
        logger.info("搜索查询: {}".format(query))
        
        # 获取邮件列表
        # 显式移除 INBOX 限制以允许遍历所有标签/分类中的邮件
        label_ids = []
        if not include_archived:
            label_ids.append('INBOX')
            
        if unread_only:
            label_ids.append('UNREAD')
        
        messages = self.list_messages(
            query=query,
            max_results=max_results,
            label_ids=label_ids if label_ids else None
        )
        
        # 获取完整邮件内容
        scholar_emails = []
        for msg_info in messages:
            try:
                # full 是 metadata 的超集（headers/labelIds/internalDate 全在），一次取回
                # 同时喂元数据解析与正文提取——省掉每封邮件一次串行往返
                details = self.get_message(msg_info['id'], format='full')
                metadata = self.parse_email_metadata(details)
                body = self.get_message_body(details)
                
                scholar_emails.append({
                    'message': details,
                    'metadata': metadata,
                    'body': body
                })
                logger.debug("  📧 {}".format(metadata.subject[:50]))
                    
            except Exception as e:
                logger.warning("处理邮件 {} 时出错: {}".format(msg_info['id'], e))
                continue
        
        logger.info("找到 {} 封 Google Scholar 邮件".format(len(scholar_emails)))
        return scholar_emails

    def mark_all_scholar_read(self) -> int:
        """
        一次性将所有历史 Scholar 邮件标记为已读（忽略收件箱/分页限制）
        
        Returns:
            标记已读的数量
        """
        logger.info("开始全量清理 unread Scholar 邮件...")
        
        # 激进查询，涵盖官方发件人和所有相关关键词
        sender_query = ' OR '.join(['from:{}'.format(sender) for sender in self.SCHOLAR_SENDERS])
        subject_keywords = [
            "Scholar Alert", 
            "Google Scholar Alert",
            "new citations", 
            "related research",
            "new results for",
            "Follow articles by"
        ]
        subject_query = ' OR '.join(['subject:"{}"'.format(kw) for kw in subject_keywords])
        
        query = '({} OR {}) label:UNREAD'.format(sender_query, subject_query)
        
        # 获取所有符合条件的 ID
        messages = self.list_messages(query=query, max_results=0) # 0 为全部
        ids = [m['id'] for m in messages]
        
        if not ids:
            logger.info("没有发现未读的 Scholar 邮件")
            return 0
            
        success = self.mark_as_read(ids)
        if success:
            logger.info("成功将 {} 封历史 Scholar 邮件标记为已读".format(len(ids)))
            return len(ids)
        return 0
    
    def mark_as_read(self, message_ids: List[str]) -> bool:
        """
        将邮件标记为已读
        
        Args:
            message_ids: 要标记的邮件ID列表
            
        Returns:
            是否成功
        """
        if not message_ids:
            return True
            
        try:
            # Gmail batchModify 限制每次最多 1000 个 ID
            batch_size = 1000
            for i in range(0, len(message_ids), batch_size):
                chunk = message_ids[i:i + batch_size]
                body = {
                    'ids': chunk,
                    'removeLabelIds': ['UNREAD']
                }
                
                self.service.users().messages().batchModify(
                    userId='me',
                    body=body
                ).execute()
                
            logger.info(f"✅ 已将 {len(message_ids)} 封邮件标记为已读")
            return True
            
        except HttpError as e:
            logger.error(f"❌ 标记已读失败: {e}")
            return False
    
    def mark_single_as_read(self, message_id: str) -> bool:
        """
        将单封邮件标记为已读
        
        Args:
            message_id: 邮件ID
            
        Returns:
            是否成功
        """
        try:
            self.service.users().messages().modify(
                userId='me',
                id=message_id,
                body={'removeLabelIds': ['UNREAD']}
            ).execute()
            return True
        except HttpError as e:
            logger.error(f"❌ 标记邮件 {message_id} 为已读失败: {e}")
            return False
    
    def add_label(self, message_id: str, label_name: str) -> bool:
        """
        为邮件添加标签
        
        Args:
            message_id: 邮件ID
            label_name: 标签名称
            
        Returns:
            是否成功
        """
        try:
            # 先获取或创建标签
            label_id = self._get_or_create_label(label_name)
            if not label_id:
                return False
            
            self.service.users().messages().modify(
                userId='me',
                id=message_id,
                body={'addLabelIds': [label_id]}
            ).execute()
            return True
            
        except HttpError as e:
            logger.error(f"❌ 添加标签失败: {e}")
            return False
    
    def _get_or_create_label(self, label_name: str) -> Optional[str]:
        """获取或创建标签"""
        try:
            # 列出所有标签
            results = self.service.users().labels().list(userId='me').execute()
            labels = results.get('labels', [])
            
            # 查找现有标签
            for label in labels:
                if label['name'] == label_name:
                    return label['id']
            
            # 创建新标签
            label_body = {
                'name': label_name,
                'labelListVisibility': 'labelShow',
                'messageListVisibility': 'show'
            }
            created_label = self.service.users().labels().create(
                userId='me',
                body=label_body
            ).execute()
            
            logger.info(f"📁 创建新标签: {label_name}")
            return created_label['id']
            
        except HttpError as e:
            logger.error(f"❌ 获取/创建标签失败: {e}")
            return None
