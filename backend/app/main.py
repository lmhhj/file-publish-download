import os, hashlib, logging, httpx, time
import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query, BackgroundTasks
from fastapi.responses import PlainTextResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, text, or_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
from jose import jwt
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
DATA_DIR = os.getenv("DATA_DIR", "./data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "fs.db")
SECRET_KEY = os.getenv("SECRET_KEY", "fixed_key_2026_user_update")
ALGORITHM = "HS256"

os.makedirs(UPLOAD_DIR, exist_ok=True)
Base = declarative_base()
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    full_name = Column(String, default="")
    hashed_password = Column(String)
    role = Column(String, default="user")

class Folder(Base):
    __tablename__ = "folders"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    parent_id = Column(Integer, default=0)
    creator = Column(String)

class FileRecord(Base):
    __tablename__ = "files"
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String); filepath = Column(String); md5 = Column(String)
    version = Column(String); changelog = Column(String); submitter = Column(String)
    git_commit = Column(String, default="")
    upload_date = Column(DateTime, default=datetime.now)
    folder_id = Column(Integer, default=0) 
    filesize = Column(Integer, default=0)

class Settings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    show_md5 = Column(Boolean, default=True)
    show_date = Column(Boolean, default=True)
    show_version = Column(Boolean, default=True)
    show_changelog = Column(Boolean, default=True)
    show_submitter = Column(Boolean, default=True)
    show_git_commit = Column(Boolean, default=True)
    site_title = Column(String, default="硬件资源下载站点")
    site_subtitle = Column(String, default="研发资源分发平台")
    # --- 增量：微信通知配置字段 ---
    wechat_enabled = Column(Boolean, default=False)
    wechat_corpid = Column(String, default="")
    wechat_agentid = Column(String, default="")
    wechat_secret = Column(String, default="")
    wechat_token = Column(String, default="")
    wechat_aes_key = Column(String, default="")
    wechat_proxy_url = Column(String, default="https://qyapi.weixin.qq.com")
    wechat_whitelist = Column(String, default="")
    wechat_template = Column(String, default="{{user}} 在 {{path}} 目录发布了 {{filename}} 文件\n版本：{{version}}\n修改记录：{{changelog}}")

Base.metadata.create_all(bind=engine)

class WXBizMsgCrypt:
    def __init__(self, token: str, key: str, receiveid: str):
        self.key = base64.b64decode(key + "=")
        self.token = token
        self.receiveid = receiveid

    def verify_signature(self, timestamp, nonce, echostr, msg_signature):
        # 验证签名确保请求来自微信
        l = [self.token, timestamp, nonce, echostr]
        l.sort()
        if hashlib.sha1("".join(l).encode()).hexdigest() == msg_signature:
            return True
        return False

    def decrypt(self, text: str):
        # AES-256-CBC 解密逻辑
        cryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16]), backend=default_backend()).decryptor()
        plain_text = cryptor.update(base64.b64decode(text)) + cryptor.finalize()
        pad = plain_text[-1]
        content = plain_text[16:-pad] # 去掉16位随机前缀和末尾填充
        xml_len = int.from_bytes(content[:4], byteorder='big')
        return content[4:4+xml_len].decode()

def init_db():
    db = SessionLocal()
    try:
        # 字段自动迁移逻辑
        migrations = [
            "ALTER TABLE users ADD COLUMN full_name VARCHAR DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN show_git_commit BOOLEAN DEFAULT 1",
            "ALTER TABLE settings ADD COLUMN wechat_enabled BOOLEAN DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN wechat_corpid VARCHAR DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN wechat_agentid VARCHAR DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN wechat_secret VARCHAR DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN wechat_token VARCHAR DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN wechat_aes_key VARCHAR DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN wechat_proxy_url VARCHAR DEFAULT 'https://qyapi.weixin.qq.com'",
            "ALTER TABLE settings ADD COLUMN wechat_whitelist VARCHAR DEFAULT ''",
            "ALTER TABLE files ADD COLUMN filesize INTEGER DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN wechat_template TEXT DEFAULT '{{user}} 在 {{path}} 目录发布了 {{filename}} 文件\n版本：{{version}}\n修改记录：{{changelog}}'"
        ]
        for cmd in migrations:
            try: db.execute(text(cmd)); db.commit()
            except: db.rollback()
        
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            db.add(User(username="admin", full_name="管理员", hashed_password=pwd_context.hash("admin123"), role="admin"))
        elif not admin.full_name: admin.full_name = "管理员"
        
        if not db.query(Settings).first(): db.add(Settings(id=1))
        db.execute(text("UPDATE files SET folder_id = 0 WHERE folder_id IS NULL"))
        db.commit()
    finally: db.close()

init_db()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return db.query(User).filter(User.username == payload.get("sub")).first()
    except: raise HTTPException(status_code=401)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def get_folder_path(folder_id: int, db: Session) -> str:
    if folder_id == 0: return "根目录"
    path_parts = []
    curr_id = folder_id
    while curr_id != 0:
        f = db.query(Folder).filter(Folder.id == curr_id).first()
        if not f: break
        path_parts.insert(0, f.name)
        curr_id = f.parent_id
    return "/" + "/".join(path_parts)

# 发送微信消息的核心逻辑
# 修改后的发送函数
def send_wechat_notify(content: str):
    db = SessionLocal() # 创建独立连接
    try:
        s = db.query(Settings).first()
        if not s or not s.wechat_enabled: return
        
        # 使用同步 Client
        with httpx.Client() as client:
            # 1. 获取 Token
            t_res = client.get(f"{s.wechat_proxy_url}/cgi-bin/gettoken?corpid={s.wechat_corpid}&corpsecret={s.wechat_secret}")
            tk = t_res.json().get("access_token")
            if tk:
                # 2. 发送消息 (强制转换 agentid 为 int)
                msg = {
                    "touser": s.wechat_whitelist or "@all",
                    "msgtype": "text",
                    "agentid": int(s.wechat_agentid), # 关键修复：转为整数
                    "text": {"content": content}
                }
                client.post(f"{s.wechat_proxy_url}/cgi-bin/message/send?access_token={tk}", json=msg)
    except Exception as e:
        logger.error(f"WeChat Notify Failed: {e}")
    finally:
        db.close()

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400)
    return {"access_token": jwt.encode({"sub": user.username}, SECRET_KEY), "token_type": "bearer", "role": user.role}

@app.get("/api/public/data")
def get_data(db: Session = Depends(get_db)):
    users_map = {u.username: (u.full_name or u.username) for u in db.query(User).all()}
    files_db = db.query(FileRecord).order_by(FileRecord.upload_date.desc()).all()
    # 关键修复：URL 使用 filepath 的真实文件名，确保 nginx 能找到物理文件
    files = [{
        "id": f.id, "name": f.filename, 
        "url": f"/files/{os.path.basename(f.filepath)}",  # <-- 这里修改了：使用物理文件名作为下载路径
        "md5": f.md5, "version": f.version, "changelog": f.changelog,
        "git_commit": f.git_commit, 
        "submitter": f.submitter,
        "publisher": users_map.get(f.submitter, f.submitter),
        "date": f.upload_date.strftime("%Y-%m-%d %H:%M"), "folder_id": f.folder_id or 0,
        "size": f.filesize
    } for f in files_db]
    folders = [{"id": f.id, "name": f.name, "parent_id": f.parent_id or 0} for f in db.query(Folder).all()]
    return {"files": files, "folders": folders, "config": db.query(Settings).first()}

# --- 增量接口：微信通知测试 ---
@app.post("/api/admin/wechat/test")
async def test_wechat(user: User = Depends(get_user), db: Session = Depends(get_db)):
    s = db.query(Settings).first()
    async with httpx.AsyncClient() as client:
        try:
            t_res = await client.get(f"{s.wechat_proxy_url}/cgi-bin/gettoken?corpid={s.wechat_corpid}&corpsecret={s.wechat_secret}")
            tk = t_res.json().get("access_token")
            if not tk: return {"errcode": 1, "errmsg": "AccessToken 获取失败"}
            msg = {"touser": s.wechat_whitelist or "@all", "msgtype": "text", "agentid": s.wechat_agentid, "text": {"content": f"配置测试成功！\n操作人: {user.full_name or user.username}"}}
            res = await client.post(f"{s.wechat_proxy_url}/cgi-bin/message/send?access_token={tk}", json=msg)
            return res.json()
        except Exception as e: return {"errcode": -1, "errmsg": str(e)}

@app.get("/api/wechat/receive")
async def verify_wechat(msg_signature: str = Query(None), timestamp: str = Query(None), nonce: str = Query(None), echostr: str = Query(None), db: Session = Depends(get_db)):
    if not echostr: return PlainTextResponse(content="no_echostr")
    s = db.query(Settings).first()
    try:
        wxcrypt = WXBizMsgCrypt(s.wechat_token, s.wechat_aes_key, s.wechat_corpid)
        # 1. 验证签名
        if not wxcrypt.verify_signature(timestamp, nonce, echostr, msg_signature):
            return PlainTextResponse(content="signature_mismatch", status_code=403)
        # 2. 解密 echostr
        decrypted_str = wxcrypt.decrypt(echostr)
        # 3. 必须返回纯文本解密串
        return PlainTextResponse(content=decrypted_str)
    except Exception as e:
        logger.error(f"微信验证异常: {e}")
        return PlainTextResponse(content="verification_error", status_code=400)

# --- 以下逻辑严格保持原始代码不动 ---
@app.post("/api/admin/folders")
def create_folder(data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    db.add(Folder(name=data['name'], parent_id=data.get('parent_id', 0), creator=user.username))
    db.commit(); return {"status": "success"}

@app.delete("/api/admin/folders/{fid}")
def del_folder(fid: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    db.query(Folder).filter(Folder.id == fid).delete(); db.commit(); return {"status": "success"}

@app.post("/api/upload")
async def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...), version: str = Form(""), changelog: str = Form(""), git_commit: str = Form(""), folder_id: int = Form(0), user: User = Depends(get_user), db: Session = Depends(get_db)):
    content = await file.read()
    # 增量修改：防止物理文件覆盖，添加时间戳，数据库仍存原名
    real_filename = f"{int(time.time())}_{file.filename}"
    save_path = os.path.join(UPLOAD_DIR, real_filename)
    
    with open(save_path, "wb") as f: f.write(content)
    new_file = FileRecord(filename=file.filename, filepath=save_path, md5=hashlib.md5(content).hexdigest(), version=version, changelog=changelog, git_commit=git_commit, submitter=user.username, folder_id=folder_id, filesize=len(content))
    db.add(new_file); db.commit()
    
    # 微信通知模板替换
    s = db.query(Settings).first()
    if s and s.wechat_enabled:
        path_str = get_folder_path(folder_id, db)
        # 获取模板，如果为空则使用默认格式
        tpl = s.wechat_template or "{{user}} 在 {{path}} 发布了 {{filename}}\n版本：{{version}}\n描述：{{changelog}}"
        msg_content = tpl.replace("{{user}}", user.full_name or user.username)\
                         .replace("{{path}}", path_str)\
                         .replace("{{filename}}", file.filename)\
                         .replace("{{version}}", version or git_commit or "v1.0")\
                         .replace("{{changelog}}", changelog or "无")
        
        # 启动后台任务
        background_tasks.add_task(send_wechat_notify, msg_content)
    
    return {"status": "success"}

@app.put("/api/admin/files/{fid}")
def edit_file(fid: int, data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    f = db.query(FileRecord).filter(FileRecord.id == fid).first()
    if f:
        for k in ['version', 'changelog', 'git_commit', 'folder_id']:
            if k in data: setattr(f, k, data[k])
        db.commit()
    return {"status": "success"}

@app.delete("/api/admin/files/{fid}")
def del_file(fid: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    f = db.query(FileRecord).filter(FileRecord.id == fid).first()
    if f:
        if os.path.exists(f.filepath): os.remove(f.filepath)
        db.delete(f); db.commit()
    return {"status": "success"}

@app.get("/api/admin/users")
def list_users(user: User = Depends(get_user), db: Session = Depends(get_db)):
    return db.query(User).all()

@app.post("/api/admin/users")
def add_user(data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    db.add(User(username=data['username'], full_name=data.get('full_name',''), hashed_password=pwd_context.hash(data['password'])))
    db.commit(); return {"status": "success"}

@app.put("/api/admin/users/{uid}")
def update_user(uid: int, data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    target = db.query(User).filter(User.id == uid).first()
    if target:
        if 'username' in data: target.username = data['username']
        if 'full_name' in data: target.full_name = data['full_name']
        if 'password' in data and data['password']: target.hashed_password = pwd_context.hash(data['password'])
        db.commit()
    return {"status": "success"}

@app.delete("/api/admin/users/{uid}")
def del_user(uid: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    db.query(User).filter(User.id == uid).delete(); db.commit(); return {"status": "success"}

@app.post("/api/admin/settings")
def set_cfg(data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    s = db.query(Settings).first()
    for k, v in data.items(): setattr(s, k, v)
    db.commit(); return {"status": "success"}

@app.put("/api/user/password")
def set_pw(data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    db.query(User).filter(User.id == user.id).update({"hashed_password": pwd_context.hash(data['new_password'])})
    db.commit(); return {"status": "success"}
