import os, hashlib, logging, httpx, time, re, uuid, shutil, subprocess, json
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Header
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from passlib.context import CryptContext
from jose import jwt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
DATA_DIR = os.getenv("DATA_DIR", "./data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
PREVIEW_DIR = os.path.join(DATA_DIR, "previews")
DB_PATH = os.path.join(DATA_DIR, "fs.db")
SECRET_KEY = os.getenv("SECRET_KEY", "fixed_key_2026_user_update")
CI_UPLOAD_TOKEN = os.getenv("CI_UPLOAD_TOKEN", "")
ALGORITHM = "HS256"
OFFICE_PREVIEW_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
SPREADSHEET_PREVIEW_EXTENSIONS = {".xls", ".xlsx"}
OFFICE_PREVIEW_CACHE_VERSION = "v2"
OFFICE_CONVERSION_TIMEOUT = int(os.getenv("OFFICE_CONVERSION_TIMEOUT", "120"))

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PREVIEW_DIR, exist_ok=True)
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
    wechat_template = Column(String, default="{{user}} 在 {{path}} 目录发布了 {{filename}} 文件\n版本：{{version}}\n修改记录：{{changelog}}")
    wechat_webhook_url = Column(String, default="")
    wechat_mentioned_list = Column(String, default="")
    wechat_mentioned_mobile_list = Column(String, default="")
    wechat_mention_all = Column(Boolean, default=False)

Base.metadata.create_all(bind=engine)

def init_db():
    db = SessionLocal()
    try:
        # 字段自动迁移逻辑
        migrations = [
            "ALTER TABLE users ADD COLUMN full_name VARCHAR DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN show_git_commit BOOLEAN DEFAULT 1",
            "ALTER TABLE settings ADD COLUMN wechat_enabled BOOLEAN DEFAULT 0",
            "ALTER TABLE files ADD COLUMN filesize INTEGER DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN wechat_template TEXT DEFAULT '{{user}} 在 {{path}} 目录发布了 {{filename}} 文件\n版本：{{version}}\n修改记录：{{changelog}}'",
            "ALTER TABLE settings ADD COLUMN wechat_webhook_url VARCHAR DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN wechat_mentioned_list VARCHAR DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN wechat_mentioned_mobile_list VARCHAR DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN wechat_mention_all BOOLEAN DEFAULT 0"
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
        user = db.query(User).filter(User.username == payload.get("sub")).first()
        if not user:
            raise HTTPException(status_code=401)
        return user
    except: raise HTTPException(status_code=401)

def require_admin(user: User):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user

def verify_ci_upload_token(x_ci_upload_token: Optional[str] = Header(None)):
    if not CI_UPLOAD_TOKEN or x_ci_upload_token != CI_UPLOAD_TOKEN:
        raise HTTPException(status_code=401, detail="CI 上传令牌无效")

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

def parse_folder_path(folder_path: Optional[str]) -> list:
    if not folder_path:
        return []
    if "\x00" in folder_path or "\\" in folder_path:
        raise HTTPException(status_code=400, detail="目录路径非法")
    parts = []
    for raw_part in folder_path.split("/"):
        try:
            parts.append(validate_resource_name(raw_part, "目录名称"))
        except HTTPException:
            if raw_part.strip():
                raise
    return parts

def get_or_create_folder_path(folder_path: Optional[str], creator: str, db: Session) -> int:
    parent_id = 0
    for name in parse_folder_path(folder_path):
        folder = db.query(Folder).filter(
            Folder.name == name,
            Folder.parent_id == parent_id
        ).first()
        if not folder:
            folder = Folder(name=name, parent_id=parent_id, creator=creator)
            db.add(folder)
            db.commit()
            db.refresh(folder)
        parent_id = folder.id
    return parent_id

def version_sort_key(version: Optional[str]):
    nums = [int(n) for n in re.findall(r"\d+", version or "")]
    if not nums:
        return (-1, -1, -1, -1, -1, -1)
    return tuple((nums + [0, 0, 0, 0, 0, 0])[:6])

def file_record_sort_key(file_record: FileRecord):
    upload_ts = file_record.upload_date.timestamp() if file_record.upload_date else 0
    return (version_sort_key(file_record.version), upload_ts, file_record.id or 0)

def normalize_git_commit(git_commit: Optional[str]) -> str:
    git = (git_commit or "").strip()
    if not git:
        return ""
    return git if git.lower().startswith("g") else f"g{git}"

def build_download_name(filename: str, folder_name: str = "", git_commit: str = "") -> str:
    folder = (folder_name or "").strip()
    git = normalize_git_commit(git_commit)
    if folder and folder != "根目录":
        return f"{folder} -{git}-{filename}" if git else f"{folder} -{filename}"
    return f"{git}-{filename}" if git else filename

def build_file_url(filepath: str) -> str:
    return f"/files/{quote(os.path.basename(filepath or ''), safe='')}"

def validate_resource_name(name: str, label: str = "名称") -> str:
    clean_name = (name or "").strip()
    if (
        not clean_name
        or clean_name in {".", ".."}
        or "\x00" in clean_name
        or "/" in clean_name
        or "\\" in clean_name
    ):
        raise HTTPException(status_code=400, detail=f"{label}非法")
    return clean_name

def sanitize_upload_filename(filename: str) -> str:
    normalized = (filename or "").replace("\\", "/").replace("\x00", "")
    basename = os.path.basename(normalized).strip()
    if not basename or basename in {".", ".."}:
        return f"upload-{uuid.uuid4().hex}"
    return basename

def is_office_preview_file(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() in OFFICE_PREVIEW_EXTENSIONS

def is_spreadsheet_preview_file(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() in SPREADSHEET_PREVIEW_EXTENSIONS

def get_office_preview_cache_key(file_record: FileRecord) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", file_record.md5 or str(file_record.id or "source"))

def get_office_preview_pdf_path(file_record: FileRecord) -> str:
    cache_key = get_office_preview_cache_key(file_record)
    return os.path.join(PREVIEW_DIR, f"{file_record.id}_{OFFICE_PREVIEW_CACHE_VERSION}_{cache_key}.pdf")

def remove_office_preview_cache(file_record: FileRecord):
    if not os.path.isdir(PREVIEW_DIR):
        return
    cache_key = get_office_preview_cache_key(file_record)
    prefix = f"{file_record.id}_"
    suffix = f"_{cache_key}.pdf"
    for name in os.listdir(PREVIEW_DIR):
        if name.startswith(prefix) and name.endswith(suffix):
            pdf_path = os.path.join(PREVIEW_DIR, name)
            if os.path.isfile(pdf_path):
                os.remove(pdf_path)

def get_file_for_write(fid: int, user: User, db: Session) -> FileRecord:
    file_record = db.query(FileRecord).filter(FileRecord.id == fid).first()
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")
    if user.role != "admin" and file_record.submitter != user.username:
        raise HTTPException(status_code=403, detail="无权操作该文件")
    return file_record

def office_preview_cache_is_fresh(source_path: str, pdf_path: str) -> bool:
    return os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0 and os.path.getmtime(pdf_path) >= os.path.getmtime(source_path)

def find_office_converter() -> str:
    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter:
        raise HTTPException(status_code=503, detail="Office 预览转换器未安装")
    return converter

def get_office_pdf_export_filter(source_path: str) -> str:
    if is_spreadsheet_preview_file(source_path):
        options = {
            "SinglePageSheets": {
                "type": "boolean",
                "value": "true",
            }
        }
        return f"pdf:calc_pdf_Export:{json.dumps(options, separators=(',', ':'))}"
    return "pdf"

def convert_office_to_pdf(source_path: str, pdf_path: str):
    converter = find_office_converter()
    export_filter = get_office_pdf_export_filter(source_path)
    tmp_dir = os.path.join(PREVIEW_DIR, f"office-{uuid.uuid4().hex}")
    profile_dir = os.path.join(tmp_dir, "profile")
    profile_uri = Path(profile_dir).resolve().as_uri()
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        result = subprocess.run(
            [
                converter,
                "--headless",
                "--nologo",
                "--nofirststartwizard",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to",
                export_filter,
                "--outdir",
                tmp_dir,
                source_path,
            ],
            capture_output=True,
            text=True,
            timeout=OFFICE_CONVERSION_TIMEOUT,
        )
        if result.returncode != 0:
            logger.error("Office preview conversion failed: %s %s", result.stdout, result.stderr)
            raise HTTPException(status_code=500, detail="Office 文档转换失败")

        stem = os.path.splitext(os.path.basename(source_path))[0]
        produced_pdf = os.path.join(tmp_dir, f"{stem}.pdf")
        if not os.path.exists(produced_pdf):
            candidates = [name for name in os.listdir(tmp_dir) if name.lower().endswith(".pdf")]
            if candidates:
                produced_pdf = os.path.join(tmp_dir, candidates[0])
        if not os.path.exists(produced_pdf):
            logger.error("Office preview conversion produced no PDF: %s %s", result.stdout, result.stderr)
            raise HTTPException(status_code=500, detail="Office 文档转换失败")

        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        os.replace(produced_pdf, pdf_path)
    except subprocess.TimeoutExpired:
        logger.error("Office preview conversion timed out: %s", source_path)
        raise HTTPException(status_code=500, detail="Office 文档转换超时")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def parse_wechat_mention_values(value: str) -> list:
    if not value:
        return []
    mentions = []
    for raw_item in re.split(r"[\s,，;；]+", value):
        item = raw_item.strip()
        if not item:
            continue
        if item == "@all":
            mentions.append(item)
            continue
        match = re.fullmatch(r"<@([^>]+)>", item)
        if match:
            item = match.group(1).strip()
        item = item.lstrip("@")
        if item:
            mentions.append(item)
    return list(dict.fromkeys(mentions))

def build_wechat_group_webhook_payload(settings: Settings, content: str) -> dict:
    mentioned_list = parse_wechat_mention_values(getattr(settings, "wechat_mentioned_list", "") or "")
    mentioned_mobile_list = parse_wechat_mention_values(getattr(settings, "wechat_mentioned_mobile_list", "") or "")
    if getattr(settings, "wechat_mention_all", False) and "@all" not in mentioned_list:
        mentioned_list.append("@all")
    text = {"content": content}
    if mentioned_list:
        text["mentioned_list"] = mentioned_list
    if mentioned_mobile_list:
        text["mentioned_mobile_list"] = mentioned_mobile_list
    return {"msgtype": "text", "text": text}

def get_robot_webhook_url(settings: Settings) -> str:
    return (getattr(settings, "wechat_webhook_url", "") or "").strip()

def send_wechat_robot_message(client, settings: Settings, content: str):
    webhook_url = get_robot_webhook_url(settings)
    if not webhook_url:
        return {"errcode": 1, "errmsg": "Webhook 未配置"}
    res = client.post(webhook_url, json=build_wechat_group_webhook_payload(settings, content))
    return res.json()

# 发送微信消息的核心逻辑
# 修改后的发送函数
def send_wechat_notify(content: str):
    db = SessionLocal() # 创建独立连接
    try:
        s = db.query(Settings).first()
        if not s or not s.wechat_enabled: return
        
        # 使用同步 Client
        with httpx.Client() as client:
            send_wechat_robot_message(client, s, content)
    except Exception as e:
        logger.error(f"WeChat Notify Failed: {e}")
    finally:
        db.close()

def render_upload_notify_content(settings: Settings, filename: str, version: str, changelog: str, git_commit: str, folder_id: int, submitter_name: str, db: Session) -> str:
    path_str = get_folder_path(folder_id, db)
    tpl = settings.wechat_template or "{{user}} 在 {{path}} 发布了 {{filename}}\n版本：{{version}}\n描述：{{changelog}}"
    return tpl.replace("{{user}}", submitter_name)\
              .replace("{{path}}", path_str)\
              .replace("{{filename}}", filename)\
              .replace("{{version}}", version or git_commit or "v1.0")\
              .replace("{{changelog}}", changelog or "无")

async def save_upload_record(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    version: str,
    changelog: str,
    git_commit: str,
    folder_id: int,
    submitter: str,
    submitter_name: str,
    db: Session,
):
    content = await file.read()
    safe_filename = sanitize_upload_filename(file.filename)
    real_filename = f"{int(time.time())}_{uuid.uuid4().hex}_{safe_filename}"
    save_path = os.path.join(UPLOAD_DIR, real_filename)
    with open(save_path, "wb") as f:
        f.write(content)

    new_file = FileRecord(
        filename=safe_filename,
        filepath=save_path,
        md5=hashlib.md5(content).hexdigest(),
        version=version,
        changelog=changelog,
        git_commit=git_commit,
        submitter=submitter,
        folder_id=folder_id,
        filesize=len(content),
    )
    db.add(new_file)
    db.commit()
    db.refresh(new_file)

    settings = db.query(Settings).first()
    if settings and settings.wechat_enabled:
        msg_content = render_upload_notify_content(
            settings,
            safe_filename,
            version,
            changelog,
            git_commit,
            folder_id,
            submitter_name,
            db,
        )
        background_tasks.add_task(send_wechat_notify, msg_content)

    return new_file

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400)
    return {"access_token": jwt.encode({"sub": user.username}, SECRET_KEY), "token_type": "bearer", "role": user.role}

@app.get("/api/public/data")
def get_data(db: Session = Depends(get_db)):
    users_map = {u.username: (u.full_name or u.username) for u in db.query(User).all()}
    folders_db = db.query(Folder).all()
    folder_names = {f.id: f.name for f in folders_db}
    files_db = sorted(db.query(FileRecord).all(), key=file_record_sort_key, reverse=True)
    files = [{
        "id": f.id, "name": f.filename, 
        "url": build_file_url(f.filepath),
        "download_name": build_download_name(f.filename, folder_names.get(f.folder_id or 0, ""), f.git_commit),
        "folder_name": folder_names.get(f.folder_id or 0, ""),
        "md5": f.md5, "version": f.version, "changelog": f.changelog,
        "git_commit": f.git_commit, 
        "submitter": f.submitter,
        "publisher": users_map.get(f.submitter, f.submitter),
        "date": f.upload_date.strftime("%Y-%m-%d %H:%M"), "folder_id": f.folder_id or 0,
        "size": f.filesize
    } for f in files_db]
    folders = [{"id": f.id, "name": f.name, "parent_id": f.parent_id or 0} for f in folders_db]
    return {"files": files, "folders": folders, "config": db.query(Settings).first()}

@app.get("/api/preview/office/{file_id}")
def preview_office_file(file_id: int, db: Session = Depends(get_db)):
    file_record = db.query(FileRecord).filter(FileRecord.id == file_id).first()
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not is_office_preview_file(file_record.filename):
        raise HTTPException(status_code=400, detail="该文件类型不支持 Office 预览")
    if not file_record.filepath or not os.path.exists(file_record.filepath):
        raise HTTPException(status_code=404, detail="源文件不存在")

    pdf_path = get_office_preview_pdf_path(file_record)
    if not office_preview_cache_is_fresh(file_record.filepath, pdf_path):
        convert_office_to_pdf(file_record.filepath, pdf_path)

    preview_name = f"{os.path.splitext(os.path.basename(file_record.filename))[0]}.pdf"
    return FileResponse(
        pdf_path,
        headers={"Cache-Control": "no-cache"},
        media_type="application/pdf",
        filename=preview_name,
        content_disposition_type="inline",
    )

# --- 增量接口：微信通知测试 ---
@app.post("/api/admin/wechat/test")
async def test_wechat(user: User = Depends(get_user), db: Session = Depends(get_db)):
    require_admin(user)
    s = db.query(Settings).first()
    webhook_url = get_robot_webhook_url(s)
    if not webhook_url:
        return {"errcode": 1, "errmsg": "Webhook 未配置"}
    async with httpx.AsyncClient() as client:
        try:
            test_id = uuid.uuid4().hex[:8]
            test_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            content = f"配置测试成功！\n操作人: {user.full_name or user.username}\n测试时间: {test_time}\n测试编号: {test_id}"
            res = await client.post(webhook_url, json=build_wechat_group_webhook_payload(s, content))
            return res.json()
        except Exception as e: return {"errcode": -1, "errmsg": str(e)}

# --- 以下逻辑严格保持原始代码不动 ---
@app.post("/api/admin/folders")
def create_folder(data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    require_admin(user)
    name = validate_resource_name(data.get("name"), "目录名称")
    parent_id = int(data.get("parent_id") or 0)
    if parent_id != 0 and not db.query(Folder).filter(Folder.id == parent_id).first():
        raise HTTPException(status_code=404, detail="父目录不存在")
    db.add(Folder(name=name, parent_id=parent_id, creator=user.username))
    db.commit(); return {"status": "success"}

@app.put("/api/admin/folders/{fid}")
def rename_folder(fid: int, data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    require_admin(user)
    name = validate_resource_name(data.get("name"), "目录名称")
    folder = db.query(Folder).filter(Folder.id == fid).first()
    if not folder:
        raise HTTPException(status_code=404, detail="目录不存在")
    folder.name = name
    db.commit()
    return {"status": "success"}

@app.delete("/api/admin/folders/{fid}")
def del_folder(fid: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    require_admin(user)
    folder = db.query(Folder).filter(Folder.id == fid).first()
    if not folder:
        raise HTTPException(status_code=404, detail="目录不存在")
    if db.query(Folder).filter(Folder.parent_id == fid).first():
        raise HTTPException(status_code=400, detail="目录下仍有子目录")
    if db.query(FileRecord).filter(FileRecord.folder_id == fid).first():
        raise HTTPException(status_code=400, detail="目录下仍有文件")
    db.delete(folder); db.commit(); return {"status": "success"}

@app.post("/api/ci/upload")
async def ci_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    folder_path: str = Form(""),
    version: str = Form(""),
    changelog: str = Form(""),
    git_commit: str = Form(""),
    submitter: str = Form("gitlab-ci"),
    _: None = Depends(verify_ci_upload_token),
    db: Session = Depends(get_db),
):
    submitter = (submitter or "gitlab-ci").strip() or "gitlab-ci"
    folder_id = get_or_create_folder_path(folder_path, submitter, db)
    new_file = await save_upload_record(
        background_tasks,
        file,
        version,
        changelog,
        git_commit,
        folder_id,
        submitter,
        submitter,
        db,
    )
    return {
        "status": "success",
        "file": {
            "id": new_file.id,
            "name": new_file.filename,
            "folder_id": new_file.folder_id or 0,
            "md5": new_file.md5,
            "version": new_file.version,
            "changelog": new_file.changelog,
            "git_commit": new_file.git_commit,
            "submitter": new_file.submitter,
            "size": new_file.filesize,
        },
    }

@app.post("/api/upload")
async def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...), version: str = Form(""), changelog: str = Form(""), git_commit: str = Form(""), folder_id: int = Form(0), user: User = Depends(get_user), db: Session = Depends(get_db)):
    new_file = await save_upload_record(
        background_tasks,
        file,
        version,
        changelog,
        git_commit,
        folder_id,
        user.username,
        user.full_name or user.username,
        db,
    )
    return {"status": "success", "id": new_file.id}

@app.put("/api/admin/files/{fid}")
def edit_file(fid: int, data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    f = get_file_for_write(fid, user, db)
    if "folder_id" in data:
        folder_id = int(data.get("folder_id") or 0)
        if folder_id != 0 and not db.query(Folder).filter(Folder.id == folder_id).first():
            raise HTTPException(status_code=404, detail="目标目录不存在")
        f.folder_id = folder_id
    for k in ['version', 'changelog', 'git_commit']:
        if k in data: setattr(f, k, data[k])
    db.commit()
    return {"status": "success"}

@app.put("/api/admin/files/{fid}/group")
def edit_file_group(fid: int, data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    source = get_file_for_write(fid, user, db)

    old_filename = source.filename
    old_folder_id = source.folder_id or 0
    query = db.query(FileRecord).filter(
        FileRecord.filename == old_filename,
        FileRecord.folder_id == old_folder_id
    )
    if user.role != "admin":
        query = query.filter(FileRecord.submitter == user.username)
    records = query.all()

    if "filename" in data:
        filename = validate_resource_name(data.get("filename"), "文件名")
        for item in records:
            item.filename = filename

    if "folder_id" in data:
        folder_id = int(data.get("folder_id") or 0)
        if folder_id != 0 and not db.query(Folder).filter(Folder.id == folder_id).first():
            raise HTTPException(status_code=404, detail="目标目录不存在")
        for item in records:
            item.folder_id = folder_id

    db.commit()
    return {"status": "success", "updated": len(records)}

@app.delete("/api/admin/files/{fid}")
def del_file(fid: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    f = get_file_for_write(fid, user, db)
    if f.filepath and os.path.exists(f.filepath): os.remove(f.filepath)
    remove_office_preview_cache(f)
    db.delete(f); db.commit()
    return {"status": "success"}

@app.get("/api/admin/users")
def list_users(user: User = Depends(get_user), db: Session = Depends(get_db)):
    require_admin(user)
    return db.query(User).all()

@app.post("/api/admin/users")
def add_user(data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    require_admin(user)
    username = validate_resource_name(data.get("username"), "账号")
    password = data.get("password") or ""
    if not password:
        raise HTTPException(status_code=400, detail="密码不能为空")
    db.add(User(username=username, full_name=data.get('full_name',''), hashed_password=pwd_context.hash(password)))
    db.commit(); return {"status": "success"}

@app.put("/api/admin/users/{uid}")
def update_user(uid: int, data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    require_admin(user)
    target = db.query(User).filter(User.id == uid).first()
    if target:
        if 'username' in data: target.username = validate_resource_name(data['username'], "账号")
        if 'full_name' in data: target.full_name = data['full_name']
        if 'password' in data and data['password']: target.hashed_password = pwd_context.hash(data['password'])
        db.commit()
    return {"status": "success"}

@app.delete("/api/admin/users/{uid}")
def del_user(uid: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    require_admin(user)
    db.query(User).filter(User.id == uid).delete(); db.commit(); return {"status": "success"}

@app.post("/api/admin/settings")
def set_cfg(data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    require_admin(user)
    s = db.query(Settings).first()
    for k, v in data.items(): setattr(s, k, v)
    db.commit(); return {"status": "success"}

@app.put("/api/user/password")
def set_pw(data: dict, user: User = Depends(get_user), db: Session = Depends(get_db)):
    db.query(User).filter(User.id == user.id).update({"hashed_password": pwd_context.hash(data['new_password'])})
    db.commit(); return {"status": "success"}
