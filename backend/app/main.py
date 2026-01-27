import os, hashlib, logging
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
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
    full_name = Column(String, default="") # 新增：姓名
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

Base.metadata.create_all(bind=engine)

def init_db():
    db = SessionLocal()
    try:
        # 字段自动迁移
        try: db.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR DEFAULT ''")); db.commit()
        except: db.rollback()
        try: db.execute(text("ALTER TABLE settings ADD COLUMN show_git_commit BOOLEAN DEFAULT 1")); db.commit()
        except: pass
        try: db.execute(text("ALTER TABLE files ADD COLUMN filesize INTEGER DEFAULT 0")); db.commit()
        except: pass
        
        # 确保管理员姓名
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            db.add(User(username="admin", full_name="管理员", hashed_password=pwd_context.hash("admin123"), role="admin"))
        elif not admin.full_name:
            admin.full_name = "管理员"
        
        if not db.query(Settings).first(): db.add(Settings(id=1))
        db.commit()
    finally: db.close()

init_db()

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def get_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return db.query(User).filter(User.username == payload.get("sub")).first()
    except: raise HTTPException(status_code=401)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400)
    token = jwt.encode({"sub": user.username}, SECRET_KEY)
    return {"access_token": token, "token_type": "bearer", "role": user.role}

@app.get("/api/public/data")
def get_data(db: Session = Depends(get_db)):
    # 建立账号ID到姓名的映射
    users_map = {u.username: (u.full_name or u.username) for u in db.query(User).all()}
    
    files_db = db.query(FileRecord).order_by(FileRecord.upload_date.desc()).all()
    files = [{
        "id": f.id, "name": f.filename, "url": f"/files/{f.filename}",
        "md5": f.md5, "version": f.version, "changelog": f.changelog,
        "git_commit": f.git_commit, 
        "submitter": users_map.get(f.submitter, f.submitter), # 显示姓名而非ID
        "date": f.upload_date.strftime("%Y-%m-%d %H:%M"), "folder_id": f.folder_id or 0,
        "size": f.filesize
    } for f in files_db]
    
    folders = [{"id": f.id, "name": f.name, "parent_id": f.parent_id or 0} for f in db.query(Folder).all()]
    return {"files": files, "folders": folders, "config": db.query(Settings).first()}

@app.post("/api/admin/folders")
def create_folder(data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    db.add(Folder(name=data['name'], parent_id=data.get('parent_id', 0), creator=user.username))
    db.commit(); return {"status": "success"}

@app.delete("/api/admin/folders/{fid}")
def del_folder(fid: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    db.query(Folder).filter(Folder.id == fid).delete(); db.commit(); return {"status": "success"}

@app.post("/api/upload")
async def upload(file: UploadFile = File(...), version: str = Form(""), changelog: str = Form(""), git_commit: str = Form(""), folder_id: int = Form(0), user: User = Depends(get_user), db: Session = Depends(get_db)):
    content = await file.read()
    save_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(save_path, "wb") as f: f.write(content)
    db.add(FileRecord(filename=file.filename, filepath=save_path, md5=hashlib.md5(content).hexdigest(), version=version, changelog=changelog, git_commit=git_commit, submitter=user.username, folder_id=folder_id, filesize=len(content)))
    db.commit(); return {"status": "success"}

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
    if user.role != "admin": raise HTTPException(status_code=403)
    return db.query(User).all()

@app.post("/api/admin/users")
def add_user(data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == data['username']).first(): raise HTTPException(status_code=400)
    db.add(User(username=data['username'], full_name=data.get('full_name', ''), hashed_password=pwd_context.hash(data['password']), role="user"))
    db.commit(); return {"status": "success"}

# 新增：修改用户信息
@app.put("/api/admin/users/{uid}")
def update_user(uid: int, data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(status_code=403)
    target = db.query(User).filter(User.id == uid).first()
    if not target: raise HTTPException(status_code=404)
    
    if 'username' in data: target.username = data['username']
    if 'full_name' in data: target.full_name = data['full_name']
    if 'password' in data and data['password']: target.hashed_password = pwd_context.hash(data['password'])
    db.commit()
    return {"status": "success"}

@app.delete("/api/admin/users/{uid}")
def del_user(uid: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(status_code=403)
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
