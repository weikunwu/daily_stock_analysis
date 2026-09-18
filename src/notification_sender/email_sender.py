# -*- coding: utf-8 -*-
"""
Email 发送提醒服务

职责：
1. 通过 Resend SMTP 中继（smtp.resend.com）发送 Email 消息
   - 用户名固定为 resend，密码为 Resend API Key（re_ 开头，无需邮箱授权码）
   - 发件域名必须先在 Resend 控制台（Domains 页）验证
"""
import logging
from typing import Optional, List
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.header import Header
from email.utils import formataddr
import smtplib

from data_provider.base import normalize_stock_code
from src.config import Config
from src.formatters import markdown_to_html_document, strip_hidden_markdown_metadata


logger = logging.getLogger(__name__)

# Resend SMTP 中继（https://resend.com/settings/smtp）
RESEND_SMTP_HOST = "smtp.resend.com"
RESEND_SMTP_PORT = 465
RESEND_SMTP_USER = "resend"


class EmailSender:
    
    def __init__(self, config: Config):
        """
        初始化 Email（Resend）配置

        Args:
            config: 配置对象
        """
        self._email_config = {
            'sender': config.email_sender,
            'sender_name': getattr(config, 'email_sender_name', 'daily_stock_analysis股票分析助手'),
            'api_key': (getattr(config, 'resend_api_key', None) or '').strip(),
            'receivers': config.email_receivers or ([config.email_sender] if config.email_sender else []),
        }
        self._stock_email_groups = getattr(config, 'stock_email_groups', None) or []
        
    def _is_email_configured(self) -> bool:
        """检查邮件配置是否完整（发件人地址 + Resend API Key）"""
        return bool(self._email_config['sender'] and self._email_config['api_key'])
    
    def get_receivers_for_stocks(self, stock_codes: List[str]) -> List[str]:
        """
        Look up email receivers for given stock codes based on stock_email_groups.
        Returns union of receivers for all matching groups; falls back to default if none match.
        Stock codes are canonicalized before comparison so that equivalent
        formats (e.g. SH600519 vs 600519) match correctly.
        """
        if not stock_codes or not self._stock_email_groups:
            return self._email_config['receivers']
        normalized_codes = [normalize_stock_code(c) for c in stock_codes]
        seen: set = set()
        result: List[str] = []
        for stocks, emails in self._stock_email_groups:
            for code in normalized_codes:
                if code in stocks:
                    for e in emails:
                        if e not in seen:
                            seen.add(e)
                            result.append(e)
                    break
        return result if result else self._email_config['receivers']

    def get_all_email_receivers(self) -> List[str]:
        """
        Return union of all configured email receivers (all groups + default).
        Used for market review which should go to everyone.
        """
        seen: set = set()
        result: List[str] = []
        for _, emails in self._stock_email_groups:
            for e in emails:
                if e not in seen:
                    seen.add(e)
                    result.append(e)
        for e in self._email_config['receivers']:
            if e not in seen:
                seen.add(e)
                result.append(e)
        return result

    def _format_sender_address(self, sender: str) -> str:
        """Encode display name safely so non-ASCII sender names work through Resend."""
        sender_name = self._email_config.get('sender_name') or '股票分析助手'
        return formataddr((str(Header(str(sender_name), 'utf-8')), sender))

    @staticmethod
    def _close_server(server: Optional[smtplib.SMTP]) -> None:
        """Best-effort SMTP cleanup to avoid leaving sockets open on header/build errors.

        Exceptions from quit()/close() are intentionally silenced — connection may already
        be in a broken state, and there is nothing useful to do at this point.
        """
        if server is None:
            return
        try:
            server.quit()
        except Exception:
            try:
                server.close()
            except Exception:
                pass
    
    def _connect_resend_smtp(self, timeout_seconds: Optional[float] = None) -> smtplib.SMTP:
        """Connect to the Resend SMTP relay (SSL, port 465) and login with the API key."""
        logger.info(f"使用 Resend SMTP 中继: {RESEND_SMTP_HOST}:{RESEND_SMTP_PORT}")
        server = smtplib.SMTP_SSL(RESEND_SMTP_HOST, RESEND_SMTP_PORT, timeout=timeout_seconds or 30)
        server.login(RESEND_SMTP_USER, self._email_config['api_key'])
        return server
    
    def send_to_email(
        self,
        content: str,
        subject: Optional[str] = None,
        receivers: Optional[List[str]] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        通过 Resend SMTP 中继发送邮件
        
        Args:
            content: 邮件内容（支持 Markdown，会转换为 HTML）
            subject: 邮件主题（可选，默认自动生成）
            receivers: 收件人列表（可选，默认使用配置的 receivers）
            
        Returns:
            是否发送成功
        """
        if not self._is_email_configured():
            logger.warning("邮件配置不完整（EMAIL_SENDER + RESEND_API_KEY），跳过推送")
            return False
        
        sender = self._email_config['sender']
        receivers = receivers or self._email_config['receivers']
        server: Optional[smtplib.SMTP] = None
        
        try:
            # 生成主题
            if subject is None:
                date_str = datetime.now().strftime('%Y-%m-%d')
                subject = f"📈 股票智能分析报告 - {date_str}"

            sanitized_content = strip_hidden_markdown_metadata(content).strip()
            
            # 将 Markdown 转换为简单 HTML
            html_content = markdown_to_html_document(sanitized_content)
            
            # 构建邮件
            msg = MIMEMultipart('alternative')
            msg['Subject'] = Header(subject, 'utf-8')
            msg['From'] = self._format_sender_address(sender)
            msg['To'] = ', '.join(receivers)
            
            # 添加纯文本和 HTML 两个版本
            text_part = MIMEText(sanitized_content, 'plain', 'utf-8')
            html_part = MIMEText(html_content, 'html', 'utf-8')
            msg.attach(text_part)
            msg.attach(html_part)
            
            # Resend SMTP 中继（用户名固定 resend，密码为 API Key）
            server = self._connect_resend_smtp(timeout_seconds)
            server.send_message(msg)
            
            logger.info(f"邮件发送成功（Resend），收件人: {receivers}")
            return True
            
        except smtplib.SMTPAuthenticationError:
            logger.error("邮件发送失败：Resend API Key（RESEND_API_KEY）认证错误，请检查 Key 是否正确")
            return False
        except smtplib.SMTPConnectError as e:
            logger.error(f"邮件发送失败：无法连接 Resend SMTP 中继（{RESEND_SMTP_HOST}）- {e}")
            return False
        except Exception as e:
            logger.error(f"发送邮件失败: {e}")
            return False
        finally:
            self._close_server(server)

    def _send_email_with_inline_image(
        self, image_bytes: bytes, receivers: Optional[List[str]] = None
    ) -> bool:
        """Send email with inline image attachment (Issue #289)."""
        if not self._is_email_configured():
            return False
        sender = self._email_config['sender']
        receivers = receivers or self._email_config['receivers']
        server: Optional[smtplib.SMTP] = None
        try:
            date_str = datetime.now().strftime('%Y-%m-%d')
            subject = f"📈 股票智能分析报告 - {date_str}"
            msg = MIMEMultipart('related')
            msg['Subject'] = Header(subject, 'utf-8')
            msg['From'] = self._format_sender_address(sender)
            msg['To'] = ', '.join(receivers)

            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText('报告已生成，详见下方图片。', 'plain', 'utf-8'))
            html_body = (
                '<p>报告已生成，详见下方图片（点击可查看大图）：</p>'
                '<p><img src="cid:report-image" alt="股票分析报告" style="max-width:100%%;" /></p>'
            )
            alt.attach(MIMEText(html_body, 'html', 'utf-8'))
            msg.attach(alt)

            img_part = MIMEImage(image_bytes, _subtype='png')
            img_part.add_header('Content-Disposition', 'inline', filename='report.png')
            img_part.add_header('Content-ID', '<report-image>')
            msg.attach(img_part)

            server = self._connect_resend_smtp()
            server.send_message(msg)
            logger.info("邮件（内联图片，Resend）发送成功，收件人: %s", receivers)
            return True
        except Exception as e:
            logger.error("邮件（内联图片，Resend）发送失败: %s", e)
            return False
        finally:
            self._close_server(server)
