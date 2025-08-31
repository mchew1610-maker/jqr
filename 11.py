"""
多用户Web监控系统 - 支持用户认证、权限管理、数据隔离
每个用户有独立的关键词和通知设置
"""

import asyncio
import json
import os
import hashlib
import secrets
import time
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional
from dataclasses import dataclass, asdict
import threading
from collections import deque
import jwt

# Telegram相关
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# Flask Web相关
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, make_response
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

# Bot相关
from telegram import Bot

import logging

# ==================== 配置 ====================

# Bot Token
BOT_TOKEN = "8444637475:AAEna0reuDASI0ypYDUoFVAhrlJT7RMZgN8"

# Web配置
WEB_HOST = "0.0.0.0"
WEB_PORT = 5000
SECRET_KEY = secrets.token_hex(32)  # 生成随机密钥
JWT_SECRET_KEY = secrets.token_hex(32)  # JWT密钥

# 默认管理员账号
DEFAULT_ADMIN = {
    "username": "admin",
    "password": "admin123",  # 首次运行后请修改
    "email": "admin@example.com"
}

# 默认关键词
DEFAULT_KEYWORDS = [
    "供押", "月结", "Rx—pay", "印度", "韩律",
    "巴西", "印尼", "泰国", "越南", "马来"
]

# ==================== 日志配置 ====================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

# ==================== 数据模型 ====================

@dataclass
class User:
    """用户模型"""
    user_id: str
    username: str
    password_hash: str
    email: str
    role: str  # admin, user, viewer
    created_at: datetime
    last_login: Optional[datetime] = None
    is_active: bool = True
    telegram_user_id: Optional[int] = None  # 绑定的Telegram账号
    keywords: List[str] = None  # 用户自定义关键词
    notify_web: bool = True  # Web通知
    notify_telegram: bool = True  # Telegram通知

    def __post_init__(self):
        if self.keywords is None:
            self.keywords = DEFAULT_KEYWORDS.copy()

@dataclass
class UserSession:
    """用户会话"""
    session_id: str
    user_id: str
    username: str
    role: str
    login_time: datetime
    last_activity: datetime
    ip_address: str

# ==================== 多用户Web系统 ====================

class MultiUserWebSystem:
    """多用户Web监控系统"""

    def __init__(self):
        # Flask应用
        self.app = Flask(__name__)
        self.app.config['SECRET_KEY'] = SECRET_KEY
        self.app.config['SESSION_TYPE'] = 'filesystem'
        self.app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

        self.socketio = SocketIO(self.app, cors_allowed_origins="*", async_mode='threading')
        CORS(self.app)

        # Bot
        self.bot = Bot(BOT_TOKEN)

        # 用户管理
        self.users: Dict[str, User] = {}  # user_id -> User
        self.sessions: Dict[str, UserSession] = {}  # session_id -> UserSession
        self.online_users: Set[str] = set()  # 在线用户ID

        # 监控账号（全局共享）
        self.monitor_accounts = {}  # phone -> account_info
        self.clients = {}  # phone -> TelegramClient
        self.monitored_groups = {}

        # 消息去重
        self.deduplicator = MessageDeduplicator(expire_time=30)

        # 用户消息队列（每个用户独立）
        self.user_messages: Dict[str, deque] = {}  # user_id -> messages

        # 全局统计
        self.global_stats = {
            'total_messages': 0,
            'matched_messages': 0,
            'filtered_duplicates': 0,
            'start_time': datetime.now()
        }

        # 用户统计
        self.user_stats: Dict[str, dict] = {}  # user_id -> stats

        # 加载数据
        self.load_data()

        # 初始化默认管理员
        self.init_default_admin()

        # 设置路由
        self.setup_routes()

        # 设置WebSocket
        self.setup_websocket()

    def init_default_admin(self):
        """初始化默认管理员账号"""
        admin_id = "admin"
        if admin_id not in self.users:
            self.users[admin_id] = User(
                user_id=admin_id,
                username=DEFAULT_ADMIN["username"],
                password_hash=generate_password_hash(DEFAULT_ADMIN["password"]),
                email=DEFAULT_ADMIN["email"],
                role="admin",
                created_at=datetime.now()
            )
            self.save_data()
            logger.info(f"创建默认管理员账号: {DEFAULT_ADMIN['username']}")

    def load_data(self):
        """加载所有数据"""
        if os.path.exists("multi_user_data.json"):
            try:
                with open("multi_user_data.json", "r", encoding="utf-8") as f:
                    data = json.load(f)

                    # 加载用户
                    for user_id, user_data in data.get("users", {}).items():
                        # 转换时间字符串
                        user_data['created_at'] = datetime.fromisoformat(user_data['created_at'])
                        if user_data.get('last_login'):
                            user_data['last_login'] = datetime.fromisoformat(user_data['last_login'])

                        self.users[user_id] = User(**user_data)

                    # 加载监控账号
                    self.monitor_accounts = data.get("monitor_accounts", {})

                    logger.info(f"加载数据: {len(self.users)} 个用户, {len(self.monitor_accounts)} 个监控账号")
            except Exception as e:
                logger.error(f"加载数据失败: {e}")

    def save_data(self):
        """保存所有数据"""
        try:
            # 准备用户数据
            users_data = {}
            for user_id, user in self.users.items():
                user_dict = asdict(user)
                # 转换时间为字符串
                user_dict['created_at'] = user.created_at.isoformat()
                if user.last_login:
                    user_dict['last_login'] = user.last_login.isoformat()
                users_data[user_id] = user_dict

            # 保存监控账号数据
            data = {
                "users": users_data,
                "monitor_accounts": self.monitor_accounts,  # 保存监控账号
                "updated_at": datetime.now().isoformat()
            }

            with open("multi_user_data.json", "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            logger.info("数据已保存")
        except Exception as e:
            logger.error(f"保存数据失败: {e}")

    # ==================== 认证装饰器 ====================

    def login_required(self, f):
        """需要登录的装饰器"""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            token = None

            # 从Cookie获取token
            if 'auth_token' in request.cookies:
                token = request.cookies.get('auth_token')

            # 从Header获取token（API调用）
            if 'Authorization' in request.headers:
                auth_header = request.headers.get('Authorization')
                if auth_header.startswith('Bearer '):
                    token = auth_header.split(' ')[1]

            if not token:
                if request.path.startswith('/api/'):
                    return jsonify({'error': '未登录'}), 401
                return redirect('/login')

            try:
                # 验证JWT
                payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=['HS256'])
                user_id = payload['user_id']

                # 检查用户是否存在
                if user_id not in self.users:
                    raise Exception("用户不存在")

                # 检查用户是否激活
                if not self.users[user_id].is_active:
                    raise Exception("用户已禁用")

                # 设置当前用户
                request.current_user = self.users[user_id]
                request.current_user_id = user_id

                return f(*args, **kwargs)

            except Exception as e:
                logger.error(f"认证失败: {e}")
                if request.path.startswith('/api/'):
                    return jsonify({'error': '认证失败'}), 401
                return redirect('/login')

        return decorated_function

    def admin_required(self, f):
        """需要管理员权限的装饰器"""
        @wraps(f)
        @self.login_required
        def decorated_function(*args, **kwargs):
            if request.current_user.role != 'admin':
                return jsonify({'error': '权限不足'}), 403
            return f(*args, **kwargs)

        return decorated_function

    # ==================== 路由设置 ====================
    def setup_routes(self):
        """设置所有路由"""

        # ========== 页面路由 ==========

        @self.app.route('/')
        @self.login_required
        def index():
            """主页（需要登录）"""
            return self.render_dashboard()

        @self.app.route('/login')
        def login_page():
            """登录页面"""
            return self.render_login()

        @self.app.route('/register')
        def register_page():
            """注册页面"""
            return self.render_register()

        @self.app.route('/admin')
        @self.admin_required
        def admin_panel():
            """管理员面板"""
            return self.render_admin()

        # ========== API 路由 ==========

        @self.app.route('/api/login', methods=['POST'])
        def api_login():
            """用户登录"""
            data = request.json
            username = data.get('username')
            password = data.get('password')

            # 查找用户
            user = None
            for u in self.users.values():
                if u.username == username:
                    user = u
                    break

            if not user:
                return jsonify({'error': '用户名或密码错误'}), 401

            # 验证密码
            if not check_password_hash(user.password_hash, password):
                return jsonify({'error': '用户名或密码错误'}), 401

            # 检查是否激活
            if not user.is_active:
                return jsonify({'error': '账号已被禁用'}), 403

            # 更新登录时间
            user.last_login = datetime.now()
            self.save_data()

            # 生成JWT Token
            payload = {
                'user_id': user.user_id,
                'username': user.username,
                'role': user.role,
                'exp': datetime.utcnow() + timedelta(hours=24)
            }
            token = jwt.encode(payload, JWT_SECRET_KEY, algorithm='HS256')

            # 添加到在线用户
            self.online_users.add(user.user_id)

            response = make_response(jsonify({
                'status': 'success',
                'user': {
                    'username': user.username,
                    'role': user.role,
                    'email': user.email
                }
            }))

            # 设置Cookie
            response.set_cookie('auth_token', token, max_age=86400, httponly=True)

            return response

        @self.app.route('/api/logout', methods=['POST'])
        @self.login_required
        def api_logout():
            """用户登出"""
            user_id = request.current_user_id

            # 从在线用户移除
            self.online_users.discard(user_id)

            response = make_response(jsonify({'status': 'success'}))
            response.set_cookie('auth_token', '', max_age=0)

            return response

        @self.app.route('/api/register', methods=['POST'])
        def api_register():
            """用户注册"""
            data = request.json
            username = data.get('username')
            password = data.get('password')
            email = data.get('email')

            # 验证输入
            if not username or not password or not email:
                return jsonify({'error': '请填写所有字段'}), 400

            # 检查用户名是否存在
            for u in self.users.values():
                if u.username == username:
                    return jsonify({'error': '用户名已存在'}), 400

            # 创建新用户
            user_id = secrets.token_hex(16)
            new_user = User(
                user_id=user_id,
                username=username,
                password_hash=generate_password_hash(password),
                email=email,
                role='user',  # 默认普通用户
                created_at=datetime.now()
            )

            self.users[user_id] = new_user
            self.user_stats[user_id] = {'total': 0, 'matched': 0}
            self.user_messages[user_id] = deque(maxlen=100)

            self.save_data()

            return jsonify({'status': 'success', 'message': '注册成功，请登录'})

        @self.app.route('/api/profile', methods=['GET', 'PUT'])
        @self.login_required
        def api_profile():
            """用户资料"""
            user = request.current_user

            if request.method == 'GET':
                return jsonify({
                    'username': user.username,
                    'email': user.email,
                    'role': user.role,
                    'created_at': user.created_at.isoformat(),
                    'last_login': user.last_login.isoformat() if user.last_login else None,
                    'telegram_user_id': user.telegram_user_id,
                    'notify_web': user.notify_web,
                    'notify_telegram': user.notify_telegram
                })

            elif request.method == 'PUT':
                data = request.json

                # 更新允许的字段
                if 'email' in data:
                    user.email = data['email']
                if 'notify_web' in data:
                    user.notify_web = data['notify_web']
                if 'notify_telegram' in data:
                    user.notify_telegram = data['notify_telegram']
                if 'telegram_user_id' in data:
                    user.telegram_user_id = data['telegram_user_id']

                # 修改密码
                if 'old_password' in data and 'new_password' in data:
                    if check_password_hash(user.password_hash, data['old_password']):
                        user.password_hash = generate_password_hash(data['new_password'])
                    else:
                        return jsonify({'error': '原密码错误'}), 400

                self.save_data()
                return jsonify({'status': 'success'})

        @self.app.route('/api/keywords', methods=['GET', 'POST', 'DELETE'])
        @self.login_required
        def api_keywords():
            """用户关键词管理"""
            user = request.current_user

            if request.method == 'GET':
                return jsonify(user.keywords)

            elif request.method == 'POST':
                new_keywords = request.json.get('keywords', [])
                for kw in new_keywords:
                    if kw.lower() not in user.keywords:
                        user.keywords.append(kw.lower())
                self.save_data()
                return jsonify({'status': 'success', 'total': len(user.keywords)})

            elif request.method == 'DELETE':
                remove_keywords = request.json.get('keywords', [])
                user.keywords = [kw for kw in user.keywords if kw not in remove_keywords]
                self.save_data()
                return jsonify({'status': 'success', 'total': len(user.keywords)})

        @self.app.route('/api/messages')
        @self.login_required
        def api_messages():
            """获取用户消息"""
            user_id = request.current_user_id
            messages = list(self.user_messages.get(user_id, []))
            return jsonify(messages)

        @self.app.route('/api/stats')
        @self.login_required
        def api_stats():
            """获取统计信息"""
            user_id = request.current_user_id
            user_stat = self.user_stats.get(user_id, {'total': 0, 'matched': 0})

            return jsonify({
                'user_stats': user_stat,
                'global_stats': {
                    'total_messages': self.global_stats['total_messages'],
                    'matched_messages': self.global_stats['matched_messages'],
                    'filtered_duplicates': self.global_stats['filtered_duplicates'],
                    'online_users': len(self.online_users),
                    'total_users': len(self.users)
                }
            })

        # ========== 管理员API ==========

        @self.app.route('/api/admin/users')
        @self.admin_required
        def api_admin_users():
            """管理员：用户列表"""
            users_list = []
            for user in self.users.values():
                users_list.append({
                    'user_id': user.user_id,
                    'username': user.username,
                    'email': user.email,
                    'role': user.role,
                    'is_active': user.is_active,
                    'is_online': user.user_id in self.online_users,
                    'created_at': user.created_at.isoformat(),
                    'last_login': user.last_login.isoformat() if user.last_login else None
                })

            return jsonify(users_list)

        @self.app.route('/api/admin/user/<user_id>', methods=['PUT', 'DELETE'])
        @self.admin_required
        def api_admin_user(user_id):
            """管理员：用户管理"""
            if user_id not in self.users:
                return jsonify({'error': '用户不存在'}), 404

            if request.method == 'PUT':
                data = request.json
                user = self.users[user_id]

                # 更新用户信息
                if 'role' in data:
                    user.role = data['role']
                if 'is_active' in data:
                    user.is_active = data['is_active']

                self.save_data()
                return jsonify({'status': 'success'})

            elif request.method == 'DELETE':
                # 删除用户
                del self.users[user_id]
                self.save_data()
                return jsonify({'status': 'success'})

        @self.app.route('/api/admin/accounts', methods=['GET', 'POST', 'DELETE'])
        @self.admin_required
        def api_admin_accounts():
            """管理员：监控账号管理"""
            if request.method == 'GET':
                # 获取所有监控账号
                return jsonify(self.monitor_accounts)

            elif request.method == 'POST':
                # 添加新的监控账号
                data = request.json
                phone = data.get('phone')
                if not phone:
                    return jsonify({'error': '手机号码必填'}), 400

                if phone in self.monitor_accounts:
                    return jsonify({'error': '该监控账号已存在'}), 400

                account_info = data.get('account_info', {})
                self.monitor_accounts[phone] = account_info
                self.save_data()
                return jsonify({'status': 'success', 'message': '监控账号已添加'}), 201

            elif request.method == 'DELETE':
                # 删除监控账号
                phone = request.args.get('phone')
                if not phone or phone not in self.monitor_accounts:
                    return jsonify({'error': '监控账号不存在'}), 404

                del self.monitor_accounts[phone]
                self.save_data()
                return jsonify({'status': 'success', 'message': '监控账号已删除'})

    # ==================== 页面渲染 ====================

    def render_login(self):
        """渲染登录页面"""
        return '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登录 - Telegram监控系统</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        
        .login-container {
            background: white;
            padding: 40px;
            border-radius: 15px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            width: 100%;
            max-width: 400px;
        }
        
        .login-header {
            text-align: center;
            margin-bottom: 30px;
        }
        
        .login-header h1 {
            color: #333;
            margin-bottom: 10px;
        }
        
        .login-header p {
            color: #666;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 8px;
            color: #555;
            font-weight: 500;
        }
        
        .form-group input {
            width: 100%;
            padding: 12px;
            border: 2px solid #e1e8ed;
            border-radius: 8px;
            font-size: 14px;
            transition: all 0.3s;
        }
        
        .form-group input:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .btn {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s;
        }
        
        .btn:hover {
            transform: translateY(-2px);
        }
        
        .form-footer {
            text-align: center;
            margin-top: 20px;
            color: #666;
        }
        
        .form-footer a {
            color: #667eea;
            text-decoration: none;
        }
        
        .error-message {
            background: #f8d7da;
            color: #721c24;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 20px;
            display: none;
        }
        
        .success-message {
            background: #d4edda;
            color: #155724;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 20px;
            display: none;
        }
        
        .demo-info {
            background: #d1ecf1;
            color: #0c5460;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        
        .demo-info h4 {
            margin-bottom: 10px;
        }
        
        .demo-account {
            background: white;
            padding: 8px;
            border-radius: 5px;
            margin-top: 5px;
            font-family: monospace;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="login-header">
            <h1>🔐 用户登录</h1>
            <p>Telegram监控系统</p>
        </div>
        
        <div class="demo-info">
            <h4>📌 测试账号</h4>
            <div class="demo-account">
                管理员: admin / admin123<br>
                普通用户: 请先注册
            </div>
        </div>
        
        <div class="error-message" id="error-message"></div>
        <div class="success-message" id="success-message"></div>
        
        <form id="login-form">
            <div class="form-group">
                <label for="username">用户名</label>
                <input type="text" id="username" name="username" required>
            </div>
            
            <div class="form-group">
                <label for="password">密码</label>
                <input type="password" id="password" name="password" required>
            </div>
            
            <button type="submit" class="btn">登录</button>
        </form>
        
        <div class="form-footer">
            还没有账号？ <a href="/register">立即注册</a>
        </div>
    </div>
    
    <script>
        document.getElementById('login-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            
            try {
                const response = await fetch('/api/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({username, password})
                });
                
                const data = await response.json();
                
                if (response.ok) {
                    document.getElementById('success-message').textContent = '登录成功，正在跳转...';
                    document.getElementById('success-message').style.display = 'block';
                    document.getElementById('error-message').style.display = 'none';
                    
                    setTimeout(() => {
                        window.location.href = '/';
                    }, 1000);
                } else {
                    document.getElementById('error-message').textContent = data.error || '登录失败';
                    document.getElementById('error-message').style.display = 'block';
                    document.getElementById('success-message').style.display = 'none';
                }
            } catch (error) {
                document.getElementById('error-message').textContent = '网络错误';
                document.getElementById('error-message').style.display = 'block';
            }
        });
    </script>
</body>
</html>
        '''

    def render_register(self):
        """渲染注册页面"""
        return '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>注册 - Telegram监控系统</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        
        .register-container {
            background: white;
            padding: 40px;
            border-radius: 15px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            width: 100%;
            max-width: 400px;
        }
        
        .register-header {
            text-align: center;
            margin-bottom: 30px;
        }
        
        .register-header h1 {
            color: #333;
            margin-bottom: 10px;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 8px;
            color: #555;
            font-weight: 500;
        }
        
        .form-group input {
            width: 100%;
            padding: 12px;
            border: 2px solid #e1e8ed;
            border-radius: 8px;
            font-size: 14px;
            transition: all 0.3s;
        }
        
        .form-group input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .btn {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
        }
        
        .form-footer {
            text-align: center;
            margin-top: 20px;
            color: #666;
        }
        
        .form-footer a {
            color: #667eea;
            text-decoration: none;
        }
        
        .error-message {
            background: #f8d7da;
            color: #721c24;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 20px;
            display: none;
        }
        
        .success-message {
            background: #d4edda;
            color: #155724;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 20px;
            display: none;
        }
    </style>
</head>
<body>
    <div class="register-container">
        <div class="register-header">
            <h1>📝 用户注册</h1>
            <p>创建您的账号</p>
        </div>
        
        <div class="error-message" id="error-message"></div>
        <div class="success-message" id="success-message"></div>
        
        <form id="register-form">
            <div class="form-group">
                <label for="username">用户名</label>
                <input type="text" id="username" name="username" required>
            </div>
            
            <div class="form-group">
                <label for="email">邮箱</label>
                <input type="email" id="email" name="email" required>
            </div>
            
            <div class="form-group">
                <label for="password">密码</label>
                <input type="password" id="password" name="password" required minlength="6">
            </div>
            
            <div class="form-group">
                <label for="confirm-password">确认密码</label>
                <input type="password" id="confirm-password" name="confirm-password" required>
            </div>
            
            <button type="submit" class="btn">注册</button>
        </form>
        
        <div class="form-footer">
            已有账号？ <a href="/login">立即登录</a>
        </div>
    </div>
    
    <script>
        document.getElementById('register-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const username = document.getElementById('username').value;
            const email = document.getElementById('email').value;
            const password = document.getElementById('password').value;
            const confirmPassword = document.getElementById('confirm-password').value;
            
            if (password !== confirmPassword) {
                document.getElementById('error-message').textContent = '两次密码不一致';
                document.getElementById('error-message').style.display = 'block';
                return;
            }
            
            try {
                const response = await fetch('/api/register', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({username, email, password})
                });
                
                const data = await response.json();
                
                if (response.ok) {
                    document.getElementById('success-message').textContent = '注册成功，正在跳转到登录页...';
                    document.getElementById('success-message').style.display = 'block';
                    document.getElementById('error-message').style.display = 'none';
                    
                    setTimeout(() => {
                        window.location.href = '/login';
                    }, 2000);
                } else {
                    document.getElementById('error-message').textContent = data.error || '注册失败';
                    document.getElementById('error-message').style.display = 'block';
                }
            } catch (error) {
                document.getElementById('error-message').textContent = '网络错误';
                document.getElementById('error-message').style.display = 'block';
            }
        });
    </script>
</body>
</html>
        '''

    def render_dashboard(self):
        """渲染用户仪表板"""
        return '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>控制台 - Telegram监控系统</title>
    <style>
        /* 样式与之前类似，这里省略部分 */
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
        }
        
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .header h1 {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .user-info {
            display: flex;
            align-items: center;
            gap: 20px;
        }
        
        .user-badge {
            background: rgba(255, 255, 255, 0.2);
            padding: 5px 15px;
            border-radius: 20px;
        }
        
        .btn-logout {
            background: rgba(255, 255, 255, 0.3);
            color: white;
            border: none;
            padding: 8px 20px;
            border-radius: 5px;
            cursor: pointer;
        }
        
        .container {
            max-width: 1400px;
            margin: 20px auto;
            padding: 0 20px;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .stat-card {
            background: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        }
        
        .stat-value {
            font-size: 32px;
            font-weight: bold;
            color: #667eea;
        }
        
        .stat-label {
            color: #666;
            margin-top: 5px;
        }
        
        .main-grid {
            display: grid;
            grid-template-columns: 1fr 2fr;
            gap: 20px;
        }
        
        .panel {
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        }
        
        .panel h2 {
            margin-bottom: 15px;
            color: #333;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🤖 Telegram监控系统</h1>
        <div class="user-info">
            <span class="user-badge" id="username">用户</span>
            <span class="user-badge" id="role">角色</span>
            <button class="btn-logout" onclick="logout()">退出</button>
        </div>
    </div>
    
    <div class="container">
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value" id="stat-matched">0</div>
                <div class="stat-label">我的匹配消息</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="stat-keywords">0</div>
                <div class="stat-label">我的关键词</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="stat-online">0</div>
                <div class="stat-label">在线用户</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="stat-global">0</div>
                <div class="stat-label">全局消息</div>
            </div>
        </div>
        
        <div class="main-grid">
            <div>
                <div class="panel">
                    <h2>🔍 我的关键词</h2>
                    <div id="keywords-list"></div>
                </div>
            </div>
            
            <div class="panel">
                <h2>📨 我的消息</h2>
                <div id="messages-list"></div>
            </div>
        </div>
    </div>
    
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <script>
        // 初始化WebSocket
        const socket = io();
        
        // 加载用户信息
        async function loadUserInfo() {
            const response = await fetch('/api/profile');
            const data = await response.json();
            document.getElementById('username').textContent = data.username;
            document.getElementById('role').textContent = data.role === 'admin' ? '管理员' : '用户';
        }
        
        // 登出
        async function logout() {
            await fetch('/api/logout', {method: 'POST'});
            window.location.href = '/login';
        }
        
        // 加载数据
        loadUserInfo();
    </script>
</body>
</html>
        '''

    # ==================== WebSocket ====================

    def setup_websocket(self):
        """设置WebSocket事件"""

        @self.socketio.on('connect')
        def handle_connect():
            logger.info(f'WebSocket连接: {request.sid}')

        @self.socketio.on('disconnect')
        def handle_disconnect():
            logger.info(f'WebSocket断开: {request.sid}')

    # ==================== 运行系统 ====================

    def run(self):
        """运行系统"""
        print("""
    ╔════════════════════════════════════════════════════════╗
    ║     多用户Web监控系统                                  ║
    ║                                                        ║
    ║     ✅ 用户认证系统                                    ║
    ║     ✅ 权限管理（管理员/用户）                         ║
    ║     ✅ 独立关键词设置                                  ║
    ║     ✅ 消息去重机制                                    ║
    ║                                                        ║
    ║     默认管理员: admin / admin123                       ║
    ╚════════════════════════════════════════════════════════╗
        """)

        print(f"\n🌐 访问地址: http://localhost:{WEB_PORT}")
        print(f"📌 默认管理员账号: admin / admin123")
        print(f"💡 用户可以自行注册账号\n")

        # 在此行添加allow_unsafe_werkzeug=True，解决Werkzeug的错误：
        self.socketio.run(
            self.app,
            host=WEB_HOST,
            port=WEB_PORT,
            debug=False,
            allow_unsafe_werkzeug=True  # 添加这个参数
        )


# ==================== 消息去重器（复用）====================

class MessageDeduplicator:
    """消息去重器"""

    def __init__(self, expire_time: int = 30):
        self.expire_time = expire_time
        self.message_cache = {}
        self.lock = threading.Lock()

    def _generate_hash(self, group_id: int, sender_id: int, content: str) -> str:
        data = f"{group_id}:{sender_id}:{content[:100]}"
        return hashlib.md5(data.encode()).hexdigest()

    def is_duplicate(self, group_id: int, sender_id: int, content: str) -> bool:
        msg_hash = self._generate_hash(group_id, sender_id, content)

        with self.lock:
            current_time = time.time()

            if msg_hash in self.message_cache:
                if current_time - self.message_cache[msg_hash] < self.expire_time:
                    return True
                else:
                    self.message_cache[msg_hash] = current_time
                    return False
            else:
                self.message_cache[msg_hash] = current_time
                return False


# ==================== 主程序 ====================

def main():
    """主程序"""
    system = MultiUserWebSystem()
    system.run()


if __name__ == "__main__":
    main()