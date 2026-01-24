#!/bin/bash

# 创建目录结构
echo "正在创建目录结构..."
mkdir -p backend/app
mkdir -p frontend/public
mkdir -p frontend/admin
mkdir -p data/uploads

# ---------------------------------------------------------
# 1. 创建 Docker Compose 文件
# ---------------------------------------------------------
cat > docker-compose.yml <<EOF
services:
  backend:
    build: ./backend
    container_name: fs-backend
    restart: always
    volumes:
      - ./data:/data
    environment:
      - DATA_DIR=/data
      - SECRET_KEY=changethissecretkey123456
    networks:
      - fs-net

  frontend:
    image: nginx:alpine
    container_name: fs-frontend
    restart: always
    volumes:
      - ./frontend/public:/usr/share/nginx/html
      - ./frontend/admin:/usr/share/nginx/html/admin
      - ./frontend/nginx.conf:/etc/nginx/conf.d/default.conf
      - ./data/uploads:/usr/share/nginx/html/files:ro
    ports:
      - "80:80"
    depends_on:
      - backend
    networks:
      - fs-net

networks:
  fs-net:
    driver: bridge
EOF

# ---------------------------------------------------------
# 2. 后端开发 (FastAPI + SQLite)
# ---------------------------------------------------------

# 2.1 Requirements
cat > backend/requirements.txt <<EOF
fastapi==0.109.0
uvicorn==0.27.0
python-multipart==0.0.6
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4
sqlalchemy==2.0.25
aiofiles==23.2.1
EOF

# 2.2 Dockerfile
cat > backend/Dockerfile <<EOF
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ./app /app/app

# 确保启动时包含 /app 在 python path
ENV PYTHONPATH=/app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
EOF

# 2.3 Main Application Logic (单文件全功能)
cat > backend/app/main.py <<EOF
import os
import hashlib
import shutil
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
from jose import JWTError, jwt
from pydantic import BaseModel

# --- 配置 ---
DATA_DIR = os.getenv("DATA_DIR", "./data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "fs.db")
SECRET_KEY = os.getenv("SECRET_KEY", "secret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 24小时

os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- 数据库模型 ---
Base = declarative_base()
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String, default="user") # admin or user

class FileRecord(Base):
    __tablename__ = "files"
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String)
    filepath = Column(String)
    md5 = Column(String)
    version = Column(String)
    changelog = Column(String)
    submitter = Column(String)
    upload_date = Column(DateTime, default=datetime.now)

class Settings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    show_md5 = Column(Boolean, default=True)
    show_date = Column(Boolean, default=True)
    show_version = Column(Boolean, default=True)
    show_changelog = Column(Boolean, default=True)
    show_submitter = Column(Boolean, default=True)

Base.metadata.create_all(bind=engine)

# --- 初始化默认数据 ---
def init_db():
    db = SessionLocal()
    # 默认 Admin
    if not db.query(User).filter(User.username == "admin").first():
        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        hashed = pwd_context.hash("admin123")
        db.add(User(username="admin", hashed_password=hashed, role="admin"))
    
    # 默认设置
    if not db.query(Settings).first():
        db.add(Settings(id=1))
    
    db.commit()
    db.close()

init_db()

# --- Auth 工具 ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None: raise HTTPException(status_code=401)
    except JWTError:
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    user = db.query(User).filter(User.username == username).first()
    if user is None: raise HTTPException(status_code=401)
    return user

def get_admin_user(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="权限不足")
    return current_user

# --- Pydantic Schemas ---
class SettingsUpdate(BaseModel):
    show_md5: bool
    show_date: bool
    show_version: bool
    show_changelog: bool
    show_submitter: bool

class UserCreate(BaseModel):
    username: str
    password: str

# --- App ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. 登录
@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = jwt.encode({"sub": user.username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "role": user.role}

# 2. 获取公共配置 (无需登录)
@app.get("/api/public/config")
def get_public_config(db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    return settings

# 3. 获取公共文件列表 (无需登录)
@app.get("/api/public/files")
def get_public_files(db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    files = db.query(FileRecord).order_by(FileRecord.upload_date.desc()).all()
    
    # 简单脱敏逻辑：如果前端不显示，后端理论上也应该根据 setting 隐藏数据，
    # 但为了逻辑简单，这里返回全量，主要依赖前端开关控制显示。
    # 实际生产中建议在这里根据 settings 置空字段。
    
    result = []
    for f in files:
        item = {
            "id": f.id,
            "name": f.filename,
            "url": f"/files/{f.filename}",
            # 如果配置为隐藏，虽然返回了但前端不会渲染。更安全做法是在这里判断 settings.show_md5
            "md5": f.md5 if settings.show_md5 else None,
            "version": f.version if settings.show_version else None,
            "changelog": f.changelog if settings.show_changelog else None,
            "submitter": f.submitter if settings.show_submitter else None,
            "date": f.upload_date.strftime("%Y-%m-%d %H:%M") if settings.show_date else None
        }
        result.append(item)
    return result

# 4. 上传文件 (需登录)
@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    version: str = Form(""),
    changelog: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    content = await file.read()
    md5_val = hashlib.md5(content).hexdigest()
    
    # 防止重名覆盖，实际生产可以使用 UUID 重命名
    save_path = os.path.join(UPLOAD_DIR, file.filename)
    
    # 写文件
    with open(save_path, "wb") as f:
        f.write(content)
        
    new_file = FileRecord(
        filename=file.filename,
        filepath=save_path,
        md5=md5_val,
        version=version,
        changelog=changelog,
        submitter=user.username,
        upload_date=datetime.now()
    )
    db.add(new_file)
    db.commit()
    return {"status": "success"}

# 5. 管理员：更新设置
@app.post("/api/admin/settings")
def update_settings(conf: SettingsUpdate, user: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    settings.show_md5 = conf.show_md5
    settings.show_date = conf.show_date
    settings.show_version = conf.show_version
    settings.show_changelog = conf.show_changelog
    settings.show_submitter = conf.show_submitter
    db.commit()
    return {"status": "updated"}

# 6. 管理员：创建账号
@app.post("/api/admin/users")
def create_user(new_user: UserCreate, user: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == new_user.username).first():
        raise HTTPException(status_code=400, detail="用户已存在")
    hashed = pwd_context.hash(new_user.password)
    db.add(User(username=new_user.username, hashed_password=hashed))
    db.commit()
    return {"status": "created"}

# 7. 管理员：删除文件
@app.delete("/api/admin/files/{file_id}")
def delete_file(file_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 允许 admin 或提交人删除
    file_record = db.query(FileRecord).filter(FileRecord.id == file_id).first()
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")
    
    if user.role != "admin" and file_record.submitter != user.username:
        raise HTTPException(status_code=403, detail="无权删除")
        
    # 删除物理文件
    if os.path.exists(file_record.filepath):
        os.remove(file_record.filepath)
        
    db.delete(file_record)
    db.commit()
    return {"status": "deleted"}
EOF

# ---------------------------------------------------------
# 3. 前端配置 (Nginx)
# ---------------------------------------------------------
cat > frontend/nginx.conf <<EOF
server {
    listen 80;
    server_name localhost;

    # 公共端入口
    location / {
        root /usr/share/nginx/html;
        index index.html;
        try_files \$uri \$uri/ /index.html;
    }

    # 管理端入口
    location /admin {
        alias /usr/share/nginx/html/admin;
        index index.html;
        try_files \$uri \$uri/ /admin/index.html;
    }

    # API 代理
    location /api {
        proxy_pass http://backend:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
    
    # 登录 Auth
    location /token {
        proxy_pass http://backend:8000;
    }

    # 静态文件下载 (直接由 Nginx 提供，高效)
    location /files {
        alias /usr/share/nginx/html/files;
        autoindex off;
    }
}
EOF

# ---------------------------------------------------------
# 4. 公共前端代码 (Tailwind + Vue3)
# ---------------------------------------------------------
cat > frontend/public/index.html <<EOF
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>资源下载中心</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    <link href="https://cdn.jsdelivr.net/npm/remixicon@2.5.0/fonts/remixicon.css" rel="stylesheet">
</head>
<body class="bg-slate-50 text-slate-800">
    <div id="app" class="min-h-screen py-10 px-4">
        <div class="max-w-6xl mx-auto">
            <div class="flex justify-between items-end mb-8">
                <div>
                    <h1 class="text-3xl font-bold text-slate-900 tracking-tight">
                        <i class="ri-folder-shield-2-line text-blue-600 mr-2"></i>研发资源中心
                    </h1>
                    <p class="text-slate-500 mt-1">内部文档与固件分发平台</p>
                </div>
                <input v-model="search" type="text" placeholder="搜索文件..." class="bg-white border border-gray-200 rounded-lg px-4 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none shadow-sm w-64">
            </div>

            <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
                <div class="grid grid-cols-12 gap-4 px-6 py-3 bg-slate-50 border-b border-slate-100 text-xs font-semibold text-slate-500 uppercase">
                    <div class="col-span-5">文件名</div>
                    <div v-if="config.show_version" class="col-span-2">版本</div>
                    <div v-if="config.show_date" class="col-span-2">日期</div>
                    <div v-if="config.show_submitter" class="col-span-2">提交人</div>
                    <div class="col-span-1 text-right">下载</div>
                </div>

                <div v-if="loading" class="p-10 text-center text-slate-400">加载中...</div>

                <div v-else-if="filteredFiles.length === 0" class="p-10 text-center text-slate-400">暂无文件</div>

                <ul v-else class="divide-y divide-slate-50">
                    <li v-for="file in filteredFiles" :key="file.id" class="hover:bg-blue-50/50 transition duration-150 group">
                        <div class="grid grid-cols-12 gap-4 px-6 py-4 items-center">
                            <div class="col-span-5 min-w-0">
                                <div class="flex items-center">
                                    <div class="p-2 bg-blue-100 text-blue-600 rounded-lg mr-3">
                                        <i class="ri-file-line"></i>
                                    </div>
                                    <div class="truncate">
                                        <div class="font-medium text-slate-900 truncate" :title="file.name">{{ file.name }}</div>
                                        <div v-if="config.show_md5 && file.md5" class="text-xs text-slate-400 font-mono mt-0.5">MD5: {{ file.md5 }}</div>
                                        <div v-if="config.show_changelog && file.changelog" class="text-xs text-slate-500 mt-1 truncate max-w-md" :title="file.changelog">
                                            <i class="ri-git-commit-line mr-1"></i>{{ file.changelog }}
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <div v-if="config.show_version" class="col-span-2">
                                <span v-if="file.version" class="bg-green-100 text-green-700 px-2 py-0.5 rounded text-xs font-medium">{{ file.version }}</span>
                            </div>
                            <div v-if="config.show_date" class="col-span-2 text-sm text-slate-500">{{ file.date }}</div>
                            <div v-if="config.show_submitter" class="col-span-2 text-sm text-slate-600 flex items-center">
                                <span v-if="file.submitter" class="w-6 h-6 rounded-full bg-slate-200 flex items-center justify-center text-xs mr-2 font-bold">{{ file.submitter[0].toUpperCase() }}</span>
                                {{ file.submitter }}
                            </div>
                            <div class="col-span-1 text-right">
                                <a :href="file.url" download class="text-slate-400 hover:text-blue-600 p-2"><i class="ri-download-2-line text-xl"></i></a>
                            </div>
                        </div>
                    </li>
                </ul>
            </div>
            
            <div class="text-center mt-8">
               <a href="/admin/" class="text-xs text-slate-300 hover:text-slate-500">管理员入口</a>
            </div>
        </div>
    </div>

    <script>
        const { createApp, ref, computed, onMounted } = Vue;
        createApp({
            setup() {
                const config = ref({});
                const files = ref([]);
                const loading = ref(true);
                const search = ref("");

                const loadData = async () => {
                    try {
                        const [resConfig, resFiles] = await Promise.all([
                            fetch('/api/public/config').then(r => r.json()),
                            fetch('/api/public/files').then(r => r.json())
                        ]);
                        config.value = resConfig;
                        files.value = resFiles;
                    } catch (e) {
                        console.error(e);
                    } finally {
                        loading.value = false;
                    }
                };

                const filteredFiles = computed(() => {
                    if (!search.value) return files.value;
                    return files.value.filter(f => f.name.toLowerCase().includes(search.value.toLowerCase()));
                });

                onMounted(loadData);
                return { config, files, loading, search, filteredFiles };
            }
        }).mount('#app');
    </script>
</body>
</html>
EOF

# ---------------------------------------------------------
# 5. 管理员前端代码
# ---------------------------------------------------------
cat > frontend/admin/index.html <<EOF
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理后台</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    <link href="https://cdn.jsdelivr.net/npm/remixicon@2.5.0/fonts/remixicon.css" rel="stylesheet">
</head>
<body class="bg-gray-100 text-gray-800">
    <div id="app" class="min-h-screen">
        
        <div v-if="!token" class="fixed inset-0 bg-gray-900 bg-opacity-50 flex items-center justify-center z-50">
            <div class="bg-white p-8 rounded-lg shadow-xl w-96">
                <h2 class="text-xl font-bold mb-4">管理员登录</h2>
                <input v-model="loginForm.username" type="text" placeholder="用户名" class="w-full mb-3 p-2 border rounded">
                <input v-model="loginForm.password" type="password" placeholder="密码" class="w-full mb-4 p-2 border rounded" @keyup.enter="doLogin">
                <button @click="doLogin" class="w-full bg-blue-600 text-white py-2 rounded hover:bg-blue-700">登录</button>
            </div>
        </div>

        <div v-else>
            <nav class="bg-white shadow px-6 py-4 flex justify-between items-center">
                <div class="font-bold text-lg flex items-center">
                    <span class="bg-blue-600 text-white px-2 py-1 rounded text-xs mr-2">ADMIN</span>
                    后台管理
                </div>
                <div class="flex items-center gap-4">
                    <span class="text-sm text-gray-500">当前用户: {{ currentUser }}</span>
                    <button @click="logout" class="text-sm text-red-500">退出</button>
                </div>
            </nav>

            <div class="max-w-6xl mx-auto mt-8 px-4 grid grid-cols-1 md:grid-cols-3 gap-6">
                
                <div class="md:col-span-1 space-y-6">
                    <div class="bg-white p-6 rounded-lg shadow-sm">
                        <h3 class="font-bold mb-4 flex items-center"><i class="ri-upload-cloud-line mr-2"></i>上传文件</h3>
                        <input type="file" ref="fileInput" class="mb-3 block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-full file:border-0 file:text-sm file:font-semibold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"/>
                        <input v-model="uploadForm.version" type="text" placeholder="版本号 (如 v1.0)" class="w-full mb-2 p-2 text-sm border rounded">
                        <textarea v-model="uploadForm.changelog" placeholder="变更记录" class="w-full mb-2 p-2 text-sm border rounded h-20"></textarea>
                        <button @click="uploadFile" :disabled="uploading" class="w-full bg-green-600 text-white py-2 rounded hover:bg-green-700 text-sm">
                            {{ uploading ? '上传中...' : '开始上传' }}
                        </button>
                    </div>

                    <div v-if="role === 'admin'" class="bg-white p-6 rounded-lg shadow-sm">
                        <h3 class="font-bold mb-4 flex items-center"><i class="ri-user-add-line mr-2"></i>创建账号</h3>
                        <input v-model="userForm.username" type="text" placeholder="新用户名" class="w-full mb-2 p-2 text-sm border rounded">
                        <input v-model="userForm.password" type="text" placeholder="新密码" class="w-full mb-2 p-2 text-sm border rounded">
                        <button @click="createUser" class="w-full bg-gray-800 text-white py-2 rounded hover:bg-gray-900 text-sm">创建用户</button>
                    </div>
                </div>

                <div class="md:col-span-2 space-y-6">
                    
                    <div v-if="role === 'admin'" class="bg-white p-6 rounded-lg shadow-sm">
                        <h3 class="font-bold mb-4 flex items-center"><i class="ri-settings-4-line mr-2"></i>前端展示开关</h3>
                        <div class="flex flex-wrap gap-4">
                            <label class="flex items-center space-x-2 cursor-pointer">
                                <input type="checkbox" v-model="settings.show_md5" @change="saveSettings" class="form-checkbox text-blue-600">
                                <span class="text-sm">MD5</span>
                            </label>
                            <label class="flex items-center space-x-2 cursor-pointer">
                                <input type="checkbox" v-model="settings.show_version" @change="saveSettings" class="form-checkbox text-blue-600">
                                <span class="text-sm">版本号</span>
                            </label>
                            <label class="flex items-center space-x-2 cursor-pointer">
                                <input type="checkbox" v-model="settings.show_date" @change="saveSettings" class="form-checkbox text-blue-600">
                                <span class="text-sm">日期</span>
                            </label>
                            <label class="flex items-center space-x-2 cursor-pointer">
                                <input type="checkbox" v-model="settings.show_changelog" @change="saveSettings" class="form-checkbox text-blue-600">
                                <span class="text-sm">变更记录</span>
                            </label>
                            <label class="flex items-center space-x-2 cursor-pointer">
                                <input type="checkbox" v-model="settings.show_submitter" @change="saveSettings" class="form-checkbox text-blue-600">
                                <span class="text-sm">提交人</span>
                            </label>
                        </div>
                    </div>

                    <div class="bg-white p-6 rounded-lg shadow-sm">
                        <h3 class="font-bold mb-4">文件管理</h3>
                        <div class="overflow-x-auto">
                            <table class="min-w-full text-sm text-left">
                                <thead class="bg-gray-50 text-gray-500">
                                    <tr>
                                        <th class="px-4 py-2">文件名</th>
                                        <th class="px-4 py-2">提交人</th>
                                        <th class="px-4 py-2 text-right">操作</th>
                                    </tr>
                                </thead>
                                <tbody class="divide-y divide-gray-100">
                                    <tr v-for="file in files" :key="file.id" class="hover:bg-gray-50">
                                        <td class="px-4 py-2">{{ file.name }}</td>
                                        <td class="px-4 py-2">
                                            <span class="bg-gray-100 text-gray-600 px-2 py-0.5 rounded text-xs">{{ file.submitter }}</span>
                                        </td>
                                        <td class="px-4 py-2 text-right">
                                            <button @click="deleteFile(file.id)" class="text-red-500 hover:text-red-700">删除</button>
                                        </td>
                                    </tr>
                                </tbody>
                            </table>
                            <div v-if="files.length === 0" class="text-center py-4 text-gray-400">暂无文件</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const { createApp, ref, onMounted } = Vue;
        createApp({
            setup() {
                const token = ref(localStorage.getItem('token') || '');
                const currentUser = ref(''); 
                const role = ref('');
                
                const loginForm = ref({ username: '', password: '' });
                const userForm = ref({ username: '', password: '' });
                const uploadForm = ref({ version: '', changelog: '' });
                const fileInput = ref(null);
                
                const uploading = ref(false);
                const settings = ref({});
                const files = ref([]);

                // API Helper
                const api = async (url, options = {}) => {
                    const headers = { ...options.headers, 'Authorization': \`Bearer \${token.value}\` };
                    const res = await fetch(url, { ...options, headers });
                    if (res.status === 401) logout();
                    return res;
                };

                const doLogin = async () => {
                    const formData = new FormData();
                    formData.append('username', loginForm.value.username);
                    formData.append('password', loginForm.value.password);
                    
                    const res = await fetch('/token', { method: 'POST', body: formData });
                    if (res.ok) {
                        const data = await res.json();
                        token.value = data.access_token;
                        role.value = data.role;
                        localStorage.setItem('token', data.access_token);
                        
                        // Parse JWT specifically for user display would need a library, 
                        // so here we just assume the login username for simplicity or fetch profile
                        currentUser.value = loginForm.value.username; 
                        
                        loadData();
                    } else {
                        alert('登录失败');
                    }
                };

                const logout = () => {
                    token.value = '';
                    localStorage.removeItem('token');
                    role.value = '';
                };

                const loadData = async () => {
                    if (!token.value) return;
                    // Load Config
                    const s = await api('/api/public/config').then(r => r.json());
                    settings.value = s;
                    // Load Files (Re-use public API for list, but admin delete logic is separate)
                    const f = await api('/api/public/files').then(r => r.json());
                    files.value = f;
                };

                const saveSettings = async () => {
                    await api('/api/admin/settings', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(settings.value)
                    });
                };

                const createUser = async () => {
                    if(!userForm.value.username || !userForm.value.password) return alert('请填写完整');
                    const res = await api('/api/admin/users', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(userForm.value)
                    });
                    if (res.ok) {
                        alert('创建成功');
                        userForm.value = {username:'', password:''};
                    } else {
                        const d = await res.json();
                        alert(d.detail);
                    }
                };

                const uploadFile = async () => {
                    const file = fileInput.value.files[0];
                    if (!file) return alert('请选择文件');
                    
                    uploading.value = true;
                    const fd = new FormData();
                    fd.append('file', file);
                    fd.append('version', uploadForm.value.version);
                    fd.append('changelog', uploadForm.value.changelog);

                    const res = await api('/api/upload', { method: 'POST', body: fd });
                    uploading.value = false;
                    
                    if (res.ok) {
                        fileInput.value.value = null;
                        uploadForm.value = { version: '', changelog: '' };
                        loadData(); // Refresh list
                    } else {
                        alert('上传失败');
                    }
                };

                const deleteFile = async (id) => {
                    if(!confirm('确定删除吗？')) return;
                    const res = await api(\`/api/admin/files/\${id}\`, { method: 'DELETE' });
                    if (res.ok) loadData();
                    else alert('删除失败');
                }

                onMounted(() => {
                    if (token.value) {
                        // Decode token roughly to get role if needed, or just let API fail
                        loadData();
                    }
                });

                return { 
                    token, loginForm, doLogin, logout, 
                    uploadForm, userForm, fileInput, uploading, uploadFile,
                    settings, saveSettings, createUser, files, deleteFile, role, currentUser
                };
            }
        }).mount('#app');
    </script>
</body>
</html>
EOF

echo "所有文件生成完毕！"
echo "请运行: docker-compose up -d --build"
