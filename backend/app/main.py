import os
import hashlib
import logging
from datetime import datetime, timedelta
from typing import List
from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, or_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
from jose import JWTError, jwt
from pydantic import BaseModel

# --- 基础配置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR", "./data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "fs.db")
SECRET_KEY = os.getenv("SECRET_KEY", "final_secret_key_2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440 

os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- 数据库模型 ---
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

Base.metadata.create_all(bind=engine)

# --- 核心修复：启动清洗与初始化 ---
def init_db():
    db = SessionLocal()
    try:
        # 1. 强力清洗：删除所有 username 为空或 None 的脏数据
        deleted = db.query(User).filter(or_(User.username == None, User.username == "")).delete(synchronize_session=False)
        if deleted > 0:
            logger.warning(f">>> 已清理 {deleted} 条异常用户数据")
            db.commit()

        # 2. 确保 Admin 存在
        if not db.query(User).filter(User.username == "admin").first():
            logger.info(">>> 初始化 Admin 账号")
            db.add(User(username="admin", hashed_password=pwd_context.hash("admin123"), role="admin"))
            db.commit()
        
        # 3. 确保配置存在
        if not db.query(Settings).first():
            db.add(Settings(id=1))
            db.commit()
    except Exception as e:
        logger.error(f"DB Init Failed: {e}")
    finally:
        db.close()

init_db()

# --- 依赖项 ---
def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def get_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username: raise HTTPException(status_code=401)
    except: raise HTTPException(status_code=401)
    
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(status_code=401)
    return user

# --- Pydantic Models ---
class PwdUpdate(BaseModel): new_password: str
class UserCreate(BaseModel): username: str; password: str
class SetUpdate(BaseModel): show_md5: bool; show_date: bool; show_version: bool; show_changelog: bool; show_submitter: bool

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- 认证接口 ---
@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    token = jwt.encode({"sub": user.username, "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)}, SECRET_KEY, algorithm=ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "role": user.role}

# --- 公共接口 ---
@app.get("/api/public/config")
def get_cfg(db: Session = Depends(get_db)): return db.query(Settings).first()

@app.get("/api/public/files")
def list_f(db: Session = Depends(get_db)):
    files = db.query(FileRecord).order_by(FileRecord.upload_date.desc()).all()
    return [{"id":f.id,"name":f.filename,"md5":f.md5,"version":f.version,"changelog":f.changelog,"submitter":f.submitter,"date":f.upload_date.strftime("%Y-%m-%d %H:%M")} for f in files]

@app.post("/api/upload")
async def upload(file: UploadFile = File(...), version: str = Form(""), changelog: str = Form(""), user: User = Depends(get_user), db: Session = Depends(get_db)):
    content = await file.read()
    save_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(save_path, "wb") as f: f.write(content)
    db.add(FileRecord(filename=file.filename, filepath=save_path, md5=hashlib.md5(content).hexdigest(), version=version, changelog=changelog, submitter=user.username))
    db.commit()
    return {"status": "success"}

# --- 核心修复：密码修改 ---
@app.put("/api/user/password")
def change_pw(data: PwdUpdate, user: User = Depends(get_user), db: Session = Depends(get_db)):
    # 使用 update 语句直接操作数据库，避免 ORM 对象状态同步问题
    new_hash = pwd_context.hash(data.new_password)
    db.query(User).filter(User.id == user.id).update({"hashed_password": new_hash})
    db.commit()
    return {"status": "success"}

# --- 管理员接口 ---
@app.get("/api/admin/users")
def list_users(user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(status_code=403)
    # 过滤掉任何可能的脏数据
    return db.query(User).filter(User.username != None, User.username != "").all()

@app.post("/api/admin/users")
def create_u(data: UserCreate, user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != 'admin': raise HTTPException(status_code=403)
    if not data.username or not data.password: raise HTTPException(status_code=400, detail="信息不完整")
    if db.query(User).filter(User.username == data.username).first(): raise HTTPException(status_code=400, detail="用户已存在")
    
    db.add(User(username=data.username, hashed_password=pwd_context.hash(data.password), role="user"))
    db.commit()
    return {"status": "success"}

@app.delete("/api/admin/users/{uid}")
def delete_u(uid: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(status_code=403)
    target = db.query(User).filter(User.id == uid).first()
    if target and target.username != "admin":
        db.delete(target)
        db.commit()
    return {"status": "success"}

@app.put("/api/admin/users/{uid}/reset")
def reset_pw(uid: int, data: PwdUpdate, user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(status_code=403)
    # 同样使用 update 语句直接操作
    new_hash = pwd_context.hash(data.new_password)
    db.query(User).filter(User.id == uid).update({"hashed_password": new_hash})
    db.commit()
    return {"status": "success"}

@app.post("/api/admin/settings")
def set_cfg(data: SetUpdate, user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != 'admin': raise HTTPException(status_code=403)
    s = db.query(Settings).first()
    for k,v in data.dict().items(): setattr(s, k, v)
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
