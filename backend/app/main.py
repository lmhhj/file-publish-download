import os
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
# 引入 text 用于执行原生 SQL 进行迁移
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, or_, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
from jose import jwt
from pydantic import BaseModel

# --- 配置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR", "./data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "fs.db")
SECRET_KEY = os.getenv("SECRET_KEY", "fixed_key_2026_v5_migration")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440 

os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- 数据库 ---
Base = declarative_base()
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String, default="user")

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
    # 新增字段
    site_title = Column(String, default="RD Download Center")
    site_subtitle = Column(String, default="研发资源分发平台")

Base.metadata.create_all(bind=engine)

def init_db():
    db = SessionLocal()
    try:
        # --- 0. 自动迁移逻辑 (关键修复) ---
        # 检查 settings 表是否存在 site_title 列，如果不存在则通过 SQL 补上
        try:
            db.execute(text("SELECT site_title FROM settings LIMIT 1"))
        except Exception:
            logger.warning(">>> 检测到旧数据库缺少 site_title 字段，正在自动迁移...")
            try:
                # SQLite 不支持一次添加多列，需分步执行
                db.execute(text("ALTER TABLE settings ADD COLUMN site_title VARCHAR DEFAULT 'RD Download Center'"))
                db.execute(text("ALTER TABLE settings ADD COLUMN site_subtitle VARCHAR DEFAULT '研发资源分发平台'"))
                db.commit()
                logger.info(">>> 数据库字段迁移成功")
            except Exception as e:
                logger.error(f">>> 迁移失败 (可能是字段已存在或其他错误): {e}")
                db.rollback()

        # --- 1. 业务初始化 ---
        # 清理脏数据
        db.query(User).filter(or_(User.username == None, User.username == "")).delete(synchronize_session=False)
        
        # 确保 Admin
        if not db.query(User).filter(User.username == "admin").first():
            db.add(User(username="admin", hashed_password=pwd_context.hash("admin123"), role="admin"))
        
        # 确保 Settings
        s = db.query(Settings).first()
        if not s:
            db.add(Settings(id=1, site_title="RD Download Center", site_subtitle="研发资源分发平台"))
        else:
            # 即使表结构有了，如果这一行数据里的值为NULL，也给它赋默认值
            if not s.site_title: s.site_title = "RD Download Center"
            if not s.site_subtitle: s.site_subtitle = "研发资源分发平台"
        
        db.commit()
    except Exception as e:
        logger.error(f"DB Init Error: {e}")
    finally:
        db.close()

init_db()

# --- 依赖 ---
def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def get_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
    except: raise HTTPException(status_code=401)
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(status_code=401)
    return user

# --- Models ---
class PwdUpdate(BaseModel): new_password: str
class UserCreate(BaseModel): username: str; password: str
class SetUpdate(BaseModel): 
    show_md5: bool
    show_date: bool
    show_version: bool
    show_changelog: bool
    show_submitter: bool
    site_title: str 
    site_subtitle: str

class FileUpdate(BaseModel):
    version: Optional[str] = ""
    changelog: Optional[str] = ""

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- 接口 ---
@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="错误")
    token = jwt.encode({"sub": user.username, "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)}, SECRET_KEY, algorithm=ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "role": user.role}

@app.get("/api/public/config")
def get_cfg(db: Session = Depends(get_db)): return db.query(Settings).first()

@app.get("/api/public/files")
def list_f(db: Session = Depends(get_db)):
    files = db.query(FileRecord).order_by(FileRecord.upload_date.desc()).all()
    return [
        {
            "id": f.id,
            "name": f.filename,
            "url": f"/files/{f.filename}",  # 【关键修复】补全下载地址字段
            "md5": f.md5,
            "version": f.version,
            "changelog": f.changelog,
            "submitter": f.submitter,
            "date": f.upload_date.strftime("%Y-%m-%d %H:%M")
        } for f in files
    ]

@app.post("/api/upload")
async def upload(file: UploadFile = File(...), version: str = Form(""), changelog: str = Form(""), user: User = Depends(get_user), db: Session = Depends(get_db)):
    content = await file.read()
    save_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(save_path, "wb") as f: f.write(content)
    db.add(FileRecord(filename=file.filename, filepath=save_path, md5=hashlib.md5(content).hexdigest(), version=version, changelog=changelog, submitter=user.username))
    db.commit()
    return {"status": "success"}

@app.put("/api/admin/files/{fid}")
def update_file(fid: int, data: FileUpdate, user: User = Depends(get_user), db: Session = Depends(get_db)):
    f = db.query(FileRecord).filter(FileRecord.id == fid).first()
    if not f: raise HTTPException(status_code=404)
    if user.role != 'admin' and f.submitter != user.username: raise HTTPException(status_code=403)
    
    if data.version is not None: f.version = data.version
    if data.changelog is not None: f.changelog = data.changelog
    db.commit()
    return {"status": "success"}

@app.delete("/api/admin/files/{fid}")
def del_f(fid: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    f = db.query(FileRecord).filter(FileRecord.id == fid).first()
    if not f: raise HTTPException(status_code=404)
    if user.role != 'admin' and f.submitter != user.username: raise HTTPException(status_code=403)
    if os.path.exists(f.filepath): os.remove(f.filepath)
    db.delete(f)
    db.commit()
    return {"status": "success"}

@app.get("/api/admin/users")
def list_users(user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(status_code=403)
    return db.query(User).filter(User.username != None, User.username != "").all()

@app.post("/api/admin/users")
def create_u(data: UserCreate, user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != 'admin': raise HTTPException(status_code=403)
    if not data.username or not data.password: raise HTTPException(status_code=400)
    if db.query(User).filter(User.username == data.username).first(): raise HTTPException(status_code=400)
    db.add(User(username=data.username, hashed_password=pwd_context.hash(data.password), role="user"))
    db.commit()
    return {"status": "success"}

@app.delete("/api/admin/users/{uid}")
def delete_u(uid: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(status_code=403)
    target = db.query(User).filter(User.id == uid).first()
    if target and target.username != "admin": db.delete(target); db.commit()
    return {"status": "success"}

@app.put("/api/admin/users/{uid}/reset")
def reset_pw(uid: int, data: PwdUpdate, user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(status_code=403)
    db.query(User).filter(User.id == uid).update({"hashed_password": pwd_context.hash(data.new_password)})
    db.commit()
    return {"status": "success"}

@app.put("/api/user/password")
def change_pw(data: PwdUpdate, user: User = Depends(get_user), db: Session = Depends(get_db)):
    db.query(User).filter(User.id == user.id).update({"hashed_password": pwd_context.hash(data.new_password)})
    db.commit()
    return {"status": "success"}

@app.post("/api/admin/settings")
def set_cfg(data: SetUpdate, user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != 'admin': raise HTTPException(status_code=403)
    s = db.query(Settings).first()
    for k,v in data.dict().items(): setattr(s, k, v)
    db.commit()
    return {"status": "success"}
