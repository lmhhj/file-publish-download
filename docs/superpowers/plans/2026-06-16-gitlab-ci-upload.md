# GitLab CI Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CI-only upload API so GitLab kernel pipelines can publish `uImage` and `system-top.dtb` with folder, version, changelog, and Git commit metadata.

**Architecture:** Keep the current single-file FastAPI app and add small helper functions beside the existing upload helpers. The new `/api/ci/upload` endpoint authenticates with `X-CI-Upload-Token`, resolves or creates a slash-separated folder path, saves the file using the same record format as browser uploads, and returns metadata for GitLab logs. Tests use FastAPI `TestClient` with a temporary `DATA_DIR`.

**Tech Stack:** FastAPI, SQLAlchemy, SQLite, Python `unittest`, GitLab CI `curl`.

---

## File Structure

- Modify `backend/app/main.py`
  - Add `CI_UPLOAD_TOKEN = os.getenv("CI_UPLOAD_TOKEN", "")`.
  - Import `Header`.
  - Add CI token validation helper.
  - Add folder path parsing and `get_or_create_folder_path`.
  - Add shared `save_upload_record` helper used by browser and CI uploads.
  - Add `POST /api/ci/upload`.
- Modify `tests/test_file_management.py`
  - Remove stale WeChat API-bot tests that no longer match the current webhook-only code.
  - Add tests for CI token rejection, nested folder creation, and metadata persistence.
- Modify `docker-compose.yml`
  - Add backend `CI_UPLOAD_TOKEN` environment passthrough.
- Create `docs/gitlab-ci-upload-example.yml`
  - Provide a copyable GitLab CI example that appends upload steps to the kernel build job.

---

### Task 1: Repair Current Backend Tests

**Files:**
- Modify: `tests/test_file_management.py`

- [ ] **Step 1: Run the current tests to confirm the existing failure**

Run:

```bash
python -m unittest tests/test_file_management.py -v
```

Expected: FAIL or ERROR in stale WeChat tests because `build_wechat_robot_payload` and `get_api_bot_target` do not exist in the current backend.

- [ ] **Step 2: Remove stale WeChat tests and add webhook-current tests**

Edit `tests/test_file_management.py`.

Delete these tests:

```python
    def test_wechat_robot_payload_is_plain_text_message(self):
        payload = self.main.build_wechat_robot_payload("发布完成")

        self.assertEqual(payload, {"msgtype": "text", "text": {"content": "发布完成"}})

    def test_wechat_channel_url_selects_group_webhook_only(self):
        settings = self.main.Settings(
            wechat_channel="group_webhook",
            wechat_webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=group",
        )

        self.assertEqual(
            self.main.get_robot_webhook_url(settings),
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=group",
        )

    def test_api_bot_uses_long_connection_target_not_webhook(self):
        settings = self.main.Settings(wechat_channel="bot_api", wechat_whitelist="zhangsan")

        settings.wechat_channel = "bot_api"
        self.assertEqual(
            self.main.get_api_bot_target(settings),
            "zhangsan",
        )
```

Insert these tests in their place:

```python
    def test_wechat_group_webhook_payload_is_plain_text_message(self):
        settings = self.main.Settings()

        payload = self.main.build_wechat_group_webhook_payload(settings, "发布完成")

        self.assertEqual(payload, {"msgtype": "text", "text": {"content": "发布完成"}})

    def test_wechat_group_webhook_payload_includes_mentions(self):
        settings = self.main.Settings(
            wechat_mentioned_list="zhangsan, <@lisi>",
            wechat_mentioned_mobile_list="13800138000 13900139000",
            wechat_mention_all=True,
        )

        payload = self.main.build_wechat_group_webhook_payload(settings, "发布完成")

        self.assertEqual(
            payload,
            {
                "msgtype": "text",
                "text": {
                    "content": "发布完成",
                    "mentioned_list": ["zhangsan", "lisi", "@all"],
                    "mentioned_mobile_list": ["13800138000", "13900139000"],
                },
            },
        )

    def test_wechat_webhook_url_reads_current_setting(self):
        settings = self.main.Settings(
            wechat_webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=group",
        )

        self.assertEqual(
            self.main.get_robot_webhook_url(settings),
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=group",
        )
```

- [ ] **Step 3: Run tests to verify the baseline is green**

Run:

```bash
python -m unittest tests/test_file_management.py -v
```

Expected: PASS for all current tests.

- [ ] **Step 4: Commit the test repair**

Run:

```bash
git add tests/test_file_management.py
git commit -m "test: align file management tests with webhook notifications"
```

---

### Task 2: Add CI Token Authentication Tests

**Files:**
- Modify: `tests/test_file_management.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Add failing tests for missing and invalid CI token**

Edit `tests/test_file_management.py`.

In `setUpClass`, after `os.environ["SECRET_KEY"] = "test-secret"`, add:

```python
        os.environ["CI_UPLOAD_TOKEN"] = "ci-test-token"
```

Add these tests after the WeChat tests:

```python
    def test_ci_upload_rejects_missing_token(self):
        response = self.client.post(
            "/api/ci/upload",
            files={"file": ("uImage", b"kernel-image", "application/octet-stream")},
        )

        self.assertEqual(response.status_code, 401)

    def test_ci_upload_rejects_invalid_token(self):
        response = self.client.post(
            "/api/ci/upload",
            headers={"X-CI-Upload-Token": "wrong-token"},
            files={"file": ("uImage", b"kernel-image", "application/octet-stream")},
        )

        self.assertEqual(response.status_code, 401)
```

- [ ] **Step 2: Run tests to verify they fail because the route is missing**

Run:

```bash
python -m unittest tests.test_file_management.FileManagementTest.test_ci_upload_rejects_missing_token tests.test_file_management.FileManagementTest.test_ci_upload_rejects_invalid_token -v
```

Expected: FAIL with `404 != 401`.

- [ ] **Step 3: Add minimal CI auth and empty route**

Edit `backend/app/main.py`.

Change the FastAPI import:

```python
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Header
```

After `SECRET_KEY = os.getenv("SECRET_KEY", "fixed_key_2026_user_update")`, add:

```python
CI_UPLOAD_TOKEN = os.getenv("CI_UPLOAD_TOKEN", "")
```

After `get_user`, add:

```python
def verify_ci_upload_token(x_ci_upload_token: Optional[str] = Header(None)):
    if not CI_UPLOAD_TOKEN or x_ci_upload_token != CI_UPLOAD_TOKEN:
        raise HTTPException(status_code=401, detail="CI 上传令牌无效")
```

Before the browser `@app.post("/api/upload")` route, add:

```python
@app.post("/api/ci/upload")
async def ci_upload(
    file: UploadFile = File(...),
    _: None = Depends(verify_ci_upload_token),
):
    return {"status": "success"}
```

- [ ] **Step 4: Run token tests to verify they pass**

Run:

```bash
python -m unittest tests.test_file_management.FileManagementTest.test_ci_upload_rejects_missing_token tests.test_file_management.FileManagementTest.test_ci_upload_rejects_invalid_token -v
```

Expected: PASS.

- [ ] **Step 5: Commit CI token auth**

Run:

```bash
git add backend/app/main.py tests/test_file_management.py
git commit -m "feat: require token for ci uploads"
```

---

### Task 3: Add Folder Path Resolution and CI Upload Persistence

**Files:**
- Modify: `tests/test_file_management.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Add failing test for valid CI upload with nested folder metadata**

Edit `tests/test_file_management.py`.

Add this test after the CI token rejection tests:

```python
    def test_ci_upload_creates_nested_folder_and_stores_metadata(self):
        response = self.client.post(
            "/api/ci/upload",
            headers={"X-CI-Upload-Token": "ci-test-token"},
            data={
                "folder_path": "内核/ZC7015/test",
                "version": "test-a1b2c3d",
                "changelog": "Add kernel driver",
                "git_commit": "a1b2c3d",
                "submitter": "gitlab-ci",
            },
            files={"file": ("uImage", b"kernel-image", "application/octet-stream")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["file"]["name"], "uImage")
        self.assertEqual(body["file"]["version"], "test-a1b2c3d")
        self.assertEqual(body["file"]["changelog"], "Add kernel driver")
        self.assertEqual(body["file"]["git_commit"], "a1b2c3d")
        self.assertEqual(body["file"]["submitter"], "gitlab-ci")
        self.assertEqual(body["file"]["size"], len(b"kernel-image"))
        self.assertEqual(body["file"]["md5"], "6ec01f90b144039feb3a329039d90570")

        public_data = self.client.get("/api/public/data").json()
        folders = public_data["folders"]
        kernel = next(folder for folder in folders if folder["name"] == "内核")
        board = next(folder for folder in folders if folder["name"] == "ZC7015")
        branch = next(folder for folder in folders if folder["name"] == "test")
        self.assertEqual(board["parent_id"], kernel["id"])
        self.assertEqual(branch["parent_id"], board["id"])
        self.assertEqual(body["file"]["folder_id"], branch["id"])

        uploaded = next(item for item in public_data["files"] if item["name"] == "uImage")
        self.assertEqual(uploaded["folder_id"], branch["id"])
        self.assertEqual(uploaded["version"], "test-a1b2c3d")
        self.assertEqual(uploaded["changelog"], "Add kernel driver")
        self.assertEqual(uploaded["git_commit"], "a1b2c3d")
        self.assertEqual(uploaded["submitter"], "gitlab-ci")
        self.assertEqual(uploaded["size"], len(b"kernel-image"))
```

- [ ] **Step 2: Run the new test to verify it fails on missing persistence**

Run:

```bash
python -m unittest tests.test_file_management.FileManagementTest.test_ci_upload_creates_nested_folder_and_stores_metadata -v
```

Expected: FAIL because the placeholder route returns no `file` metadata and does not save a record.

- [ ] **Step 3: Implement folder path helpers and shared upload saver**

Edit `backend/app/main.py`.

After `get_folder_path`, add:

```python
def parse_folder_path(folder_path: Optional[str]) -> list:
    if not folder_path:
        return []
    if "\x00" in folder_path or "\\" in folder_path:
        raise HTTPException(status_code=400, detail="目录路径非法")
    parts = []
    for raw_part in folder_path.split("/"):
        part = raw_part.strip()
        if not part:
            continue
        if part in {".", ".."}:
            raise HTTPException(status_code=400, detail="目录路径非法")
        parts.append(part)
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
```

After `send_wechat_notify`, add:

```python
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
    real_filename = f"{int(time.time())}_{file.filename}"
    save_path = os.path.join(UPLOAD_DIR, real_filename)
    with open(save_path, "wb") as f:
        f.write(content)

    new_file = FileRecord(
        filename=file.filename,
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
            file.filename,
            version,
            changelog,
            git_commit,
            folder_id,
            submitter_name,
            db,
        )
        background_tasks.add_task(send_wechat_notify, msg_content)

    return new_file
```

- [ ] **Step 4: Replace placeholder CI route with real upload route**

Edit `backend/app/main.py`.

Replace the placeholder `ci_upload` function with:

```python
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
```

- [ ] **Step 5: Refactor browser upload to use shared saver**

Edit the existing `upload` route in `backend/app/main.py`.

Replace the body with:

```python
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
```

- [ ] **Step 6: Run the CI upload persistence test**

Run:

```bash
python -m unittest tests.test_file_management.FileManagementTest.test_ci_upload_creates_nested_folder_and_stores_metadata -v
```

Expected: PASS.

- [ ] **Step 7: Commit CI upload persistence**

Run:

```bash
git add backend/app/main.py tests/test_file_management.py
git commit -m "feat: store ci uploaded artifacts"
```

---

### Task 4: Add Folder Path Validation Tests

**Files:**
- Modify: `tests/test_file_management.py`
- Modify: `backend/app/main.py` only if tests reveal validation bugs

- [ ] **Step 1: Add tests for invalid and normalized folder paths**

Edit `tests/test_file_management.py`.

Add these tests after `test_ci_upload_creates_nested_folder_and_stores_metadata`:

```python
    def test_ci_upload_rejects_unsafe_folder_path(self):
        for folder_path in ["内核/../ZC7015", "内核\\ZC7015", "内核/\x00/ZC7015"]:
            with self.subTest(folder_path=folder_path):
                response = self.client.post(
                    "/api/ci/upload",
                    headers={"X-CI-Upload-Token": "ci-test-token"},
                    data={"folder_path": folder_path},
                    files={"file": ("uImage", b"kernel-image", "application/octet-stream")},
                )

                self.assertEqual(response.status_code, 400)

    def test_ci_upload_ignores_empty_folder_path_segments(self):
        response = self.client.post(
            "/api/ci/upload",
            headers={"X-CI-Upload-Token": "ci-test-token"},
            data={"folder_path": " /内核//ZC7015/ "},
            files={"file": ("system-top.dtb", b"device-tree", "application/octet-stream")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        public_data = self.client.get("/api/public/data").json()
        folders = public_data["folders"]
        kernel = next(folder for folder in folders if folder["name"] == "内核")
        board = next(folder for folder in folders if folder["name"] == "ZC7015" and folder["parent_id"] == kernel["id"])
        self.assertEqual(response.json()["file"]["folder_id"], board["id"])
```

- [ ] **Step 2: Run folder validation tests**

Run:

```bash
python -m unittest tests.test_file_management.FileManagementTest.test_ci_upload_rejects_unsafe_folder_path tests.test_file_management.FileManagementTest.test_ci_upload_ignores_empty_folder_path_segments -v
```

Expected: PASS if Task 3 implementation already matches the spec.

- [ ] **Step 3: Fix validation only if needed**

If the tests fail, edit only `parse_folder_path` in `backend/app/main.py` to match:

```python
def parse_folder_path(folder_path: Optional[str]) -> list:
    if not folder_path:
        return []
    if "\x00" in folder_path or "\\" in folder_path:
        raise HTTPException(status_code=400, detail="目录路径非法")
    parts = []
    for raw_part in folder_path.split("/"):
        part = raw_part.strip()
        if not part:
            continue
        if part in {".", ".."}:
            raise HTTPException(status_code=400, detail="目录路径非法")
        parts.append(part)
    return parts
```

- [ ] **Step 4: Commit folder path validation tests**

Run:

```bash
git add backend/app/main.py tests/test_file_management.py
git commit -m "test: cover ci folder path validation"
```

---

### Task 5: Add Deployment and GitLab CI Examples

**Files:**
- Modify: `docker-compose.yml`
- Create: `docs/gitlab-ci-upload-example.yml`

- [ ] **Step 1: Update docker compose token configuration**

Edit `docker-compose.yml`.

In `services.backend.environment`, add:

```yaml
      - CI_UPLOAD_TOKEN=${CI_UPLOAD_TOKEN:-}
```

The backend environment block should look like:

```yaml
    environment:
      - DATA_DIR=/data
      - SECRET_KEY=changethissecretkey123456
      - CI_UPLOAD_TOKEN=${CI_UPLOAD_TOKEN:-}
```

- [ ] **Step 2: Add GitLab CI upload example**

Create `docs/gitlab-ci-upload-example.yml` with:

```yaml
variables:
  ARCH_VAL: "arm"
  CROSS_COMPILE_VAL: "/opt/toolchains/gcc-linaro-6.4.1-arm-gnueabihf/bin/arm-linux-gnueabihf-"
  LOADADDR_VAL: "0x00008000"
  FILE_SERVER_URL: "https://files.example.internal"
  FILE_RELEASE_FOLDER: "内核/ZC7015"
  FILE_RELEASE_VERSION: "${CI_COMMIT_TAG:-${CI_COMMIT_REF_NAME}-${CI_COMMIT_SHORT_SHA}}"

stages:
  - build

build_zc7015:
  stage: build
  tags:
    - build-kernel
  only:
    - test
  script:
    - echo "--- 1. 显式导出环境变量 ---"
    - export ARCH=$ARCH_VAL
    - export CROSS_COMPILE=$CROSS_COMPILE_VAL
    - export LOADADDR=$LOADADDR_VAL

    - echo "--- 2. 检查编译器版本 ---"
    - ${CROSS_COMPILE}gcc --version

    - echo "--- 3. 环境清理与配置 ---"
    - make distclean
    - make som_71_defconfig

    - echo "--- 4. 开始并行编译 (双编译器协同) ---"
    - make HOSTCFLAGS="-fcommon" -j$(nproc) uImage LOADADDR=0x00008000

    - echo "--- 5. 编译设备树 ---"
    - make HOSTCFLAGS="-fcommon" system-top.dtb

    - echo "--- 6. 整理产物 ---"
    - mkdir -p output_release
    - cp arch/arm/boot/uImage output_release/
    - cp arch/arm/boot/dts/system-top.dtb output_release/

    - echo "--- 7. 上传产物到文件管理后台 ---"
    - test -n "$FILE_UPLOAD_TOKEN"
    - |
      RELEASE_CHANGELOG="${CI_COMMIT_TITLE:-${CI_COMMIT_MESSAGE}}"
      for artifact in output_release/uImage output_release/system-top.dtb; do
        curl -f -X POST "$FILE_SERVER_URL/api/ci/upload" \
          -H "X-CI-Upload-Token: $FILE_UPLOAD_TOKEN" \
          -F "file=@${artifact}" \
          -F "folder_path=${FILE_RELEASE_FOLDER}" \
          -F "version=${FILE_RELEASE_VERSION}" \
          -F "changelog=${RELEASE_CHANGELOG}" \
          -F "git_commit=${CI_COMMIT_SHORT_SHA}" \
          -F "submitter=gitlab-ci"
      done

  artifacts:
    name: "ZC7015_Build_${CI_COMMIT_SHORT_SHA}"
    paths:
      - output_release/
    expire_in: 1 week
```

- [ ] **Step 3: Commit deployment docs**

Run:

```bash
git add docker-compose.yml docs/gitlab-ci-upload-example.yml
git commit -m "docs: add gitlab ci upload example"
```

---

### Task 6: Full Verification

**Files:**
- Read-only verification across changed files

- [ ] **Step 1: Run the complete backend test suite**

Run:

```bash
python -m unittest tests/test_file_management.py -v
```

Expected: PASS for all tests.

- [ ] **Step 2: Check Python syntax**

Run:

```bash
python -m py_compile backend/app/main.py tests/test_file_management.py
```

Expected: no output and exit code 0.

- [ ] **Step 3: Inspect final diff**

Run:

```bash
git status --short
git diff --stat HEAD
```

Expected: clean working tree if all task commits were made. If there are uncommitted changes, inspect them and either commit intended changes or revert only your own accidental edits.

- [ ] **Step 4: Document runtime configuration for the user**

Prepare the final response with:

```text
Backend env:
CI_UPLOAD_TOKEN=<long random token>

GitLab CI/CD variable:
FILE_UPLOAD_TOKEN=<same token>

GitLab YAML example:
docs/gitlab-ci-upload-example.yml
```
