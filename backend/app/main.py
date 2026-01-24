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

pwd_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

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
    try:
        # 1. 检查是否存在 admin
        admin = db.query(User).filter(User.username == "admin").first()
        
        # 强制重置逻辑：如果 admin 存在，我们也更新一下它的密码哈希
        # 这样就不需要手动删数据库了，重启即重置
        hashed_pwd = pwd_context.hash("admin123")
        
        if not admin:
            print("正在创建初始 admin 账号...")
            db.add(User(username="admin", hashed_password=hashed_pwd, role="admin"))
        else:
            print("正在同步 admin 账号哈希算法...")
            admin.hashed_password = hashed_pwd
        
        # 2. 默认设置
        if not db.query(Settings).first():
            db.add(Settings(id=1))
        
        db.commit()
    except Exception as e:
        print(f"数据库初始化失败: {e}")
    finally:
        db.close()

init_db()
# --- Auth 工具 ---

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
