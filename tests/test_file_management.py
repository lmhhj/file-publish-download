import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


class FileManagementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["SECRET_KEY"] = "test-secret"
        os.environ["CI_UPLOAD_TOKEN"] = "ci-test-token"
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        cls.main = importlib.import_module("backend.app.main")
        cls.client = TestClient(cls.main.app)
        response = cls.client.post(
            "/token",
            data={"username": "admin", "password": "admin123"},
        )
        assert response.status_code == 200, response.text
        cls.headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    def auth_headers_for(self, username: str, password: str = "pw") -> dict:
        response = self.client.post("/token", data={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def create_user(self, username: str, password: str = "pw", full_name: str = "") -> dict:
        response = self.client.post(
            "/api/admin/users",
            headers=self.headers,
            json={"username": username, "password": password, "full_name": full_name},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return self.auth_headers_for(username, password)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_version_sort_key_orders_semantic_versions(self):
        versions = ["v0.1", "V1.0", "V1.10"]

        ordered = sorted(versions, key=self.main.version_sort_key, reverse=True)

        self.assertEqual(ordered, ["V1.10", "V1.0", "v0.1"])

    def test_build_download_name_uses_folder_and_git(self):
        name = self.main.build_download_name("uboot.img", "新 ENCU3000", "d3dd22a533")

        self.assertEqual(name, "新 ENCU3000 -gd3dd22a533-uboot.img")

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

    def test_ci_upload_rejects_unsafe_folder_name_segments(self):
        for folder_path in ["内核/发布/.", "内核/name/with/slash"]:
            with self.subTest(folder_path=folder_path):
                response = self.client.post(
                    "/api/ci/upload",
                    headers={"X-CI-Upload-Token": "ci-test-token"},
                    data={"folder_path": folder_path},
                    files={"file": ("uImage", b"kernel-image", "application/octet-stream")},
                )

                if folder_path == "内核/name/with/slash":
                    self.assertEqual(response.status_code, 200)
                else:
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

    def test_ci_upload_sanitizes_path_like_filename(self):
        response = self.client.post(
            "/api/ci/upload",
            headers={"X-CI-Upload-Token": "ci-test-token"},
            files={"file": ("../release/uImage", b"kernel-image", "application/octet-stream")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["file"]["name"], "uImage")
        uploaded = next(item for item in self.client.get("/api/public/data").json()["files"] if item["id"] == response.json()["file"]["id"])
        physical_name = uploaded["url"].removeprefix("/files/")
        self.assertNotIn("/", physical_name)
        self.assertNotIn("\\", physical_name)

    def test_ci_upload_same_second_same_filename_keeps_distinct_files(self):
        main = self.main
        original_time = main.time.time
        main.time.time = lambda: 1782454007
        try:
            first = self.client.post(
                "/api/ci/upload",
                headers={"X-CI-Upload-Token": "ci-test-token"},
                files={"file": ("uImage", b"first-kernel", "application/octet-stream")},
            )
            second = self.client.post(
                "/api/ci/upload",
                headers={"X-CI-Upload-Token": "ci-test-token"},
                files={"file": ("uImage", b"second-kernel", "application/octet-stream")},
            )
        finally:
            main.time.time = original_time

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        public_files = self.client.get("/api/public/data").json()["files"]
        first_url = next(item["url"] for item in public_files if item["id"] == first.json()["file"]["id"])
        second_url = next(item["url"] for item in public_files if item["id"] == second.json()["file"]["id"])
        self.assertNotEqual(first_url, second_url)

    def test_public_file_url_escapes_reserved_url_characters(self):
        response = self.client.post(
            "/api/ci/upload",
            headers={"X-CI-Upload-Token": "ci-test-token"},
            files={"file": ("发布记录 #1?.pdf", b"pdf-content", "application/pdf")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        uploaded = next(item for item in self.client.get("/api/public/data").json()["files"] if item["id"] == response.json()["file"]["id"])
        physical_name = uploaded["url"].removeprefix("/files/")
        self.assertIn("%20", physical_name)
        self.assertIn("%23", physical_name)
        self.assertIn("%3F", physical_name)
        self.assertNotIn("#", physical_name)
        self.assertNotIn("?", physical_name)

    def test_office_preview_extension_detection(self):
        for filename in ["report.doc", "report.docx", "sheet.xls", "sheet.xlsx", "slides.ppt", "slides.pptx"]:
            with self.subTest(filename=filename):
                self.assertTrue(self.main.is_office_preview_file(filename))

        for filename in ["readme.md", "manual.pdf", "archive.zip", "uImage"]:
            with self.subTest(filename=filename):
                self.assertFalse(self.main.is_office_preview_file(filename))

    def test_office_preview_converts_file_to_pdf(self):
        main = self.main
        source_path = Path(main.UPLOAD_DIR) / "office-preview.docx"
        source_path.write_bytes(b"fake office document")
        original_converter = main.convert_office_to_pdf

        def fake_converter(source_file, pdf_file):
            self.assertEqual(source_file, str(source_path))
            Path(pdf_file).write_bytes(b"%PDF-1.4\nfake pdf")

        db = main.SessionLocal()
        try:
            record = main.FileRecord(
                filename="office-preview.docx",
                filepath=str(source_path),
                md5="office-md5",
                version="V1.0",
                submitter="admin",
                filesize=source_path.stat().st_size,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            record_id = record.id
        finally:
            db.close()

        main.convert_office_to_pdf = fake_converter
        try:
            response = self.client.get(f"/api/preview/office/{record_id}")
        finally:
            main.convert_office_to_pdf = original_converter

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertTrue(response.content.startswith(b"%PDF-1.4"))

    def test_spreadsheet_preview_uses_single_page_sheet_pdf_export(self):
        main = self.main
        source_path = Path(main.UPLOAD_DIR) / "wide-table.xlsx"
        pdf_path = Path(main.PREVIEW_DIR) / "wide-table.pdf"
        source_path.write_bytes(b"fake spreadsheet")
        recorded_command = []
        original_find_converter = main.find_office_converter
        original_run = main.subprocess.run

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_find_converter():
            return "/usr/bin/soffice"

        def fake_run(args, capture_output, text, timeout):
            recorded_command.extend(args)
            output_dir = Path(args[args.index("--outdir") + 1])
            (output_dir / "wide-table.pdf").write_bytes(b"%PDF-1.4\nfake pdf")
            return FakeResult()

        main.find_office_converter = fake_find_converter
        main.subprocess.run = fake_run
        try:
            main.convert_office_to_pdf(str(source_path), str(pdf_path))
        finally:
            main.find_office_converter = original_find_converter
            main.subprocess.run = original_run

        convert_filter = recorded_command[recorded_command.index("--convert-to") + 1]
        self.assertIn("pdf:calc_pdf_Export", convert_filter)
        self.assertIn("SinglePageSheets", convert_filter)
        self.assertTrue(pdf_path.exists())

    def test_office_preview_conversion_uses_isolated_user_profile(self):
        main = self.main
        source_path = Path(main.UPLOAD_DIR) / "office-preview.docx"
        pdf_path = Path(main.PREVIEW_DIR) / "office-preview.pdf"
        source_path.write_bytes(b"fake office document")
        recorded_command = []
        original_find_converter = main.find_office_converter
        original_run = main.subprocess.run

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_find_converter():
            return "/usr/bin/soffice"

        def fake_run(args, capture_output, text, timeout):
            recorded_command.extend(args)
            output_dir = Path(args[args.index("--outdir") + 1])
            (output_dir / "office-preview.pdf").write_bytes(b"%PDF-1.4\nfake pdf")
            return FakeResult()

        main.find_office_converter = fake_find_converter
        main.subprocess.run = fake_run
        try:
            main.convert_office_to_pdf(str(source_path), str(pdf_path))
        finally:
            main.find_office_converter = original_find_converter
            main.subprocess.run = original_run

        self.assertTrue(
            any(arg.startswith("-env:UserInstallation=file://") for arg in recorded_command),
            recorded_command,
        )

    def test_office_preview_user_profile_uri_escapes_spaces(self):
        main = self.main
        original_preview_dir = main.PREVIEW_DIR
        spaced_preview_dir = Path(original_preview_dir) / "preview cache"
        spaced_preview_dir.mkdir(parents=True, exist_ok=True)
        main.PREVIEW_DIR = str(spaced_preview_dir)
        source_path = Path(main.UPLOAD_DIR) / "office-preview.docx"
        pdf_path = spaced_preview_dir / "office-preview.pdf"
        source_path.write_bytes(b"fake office document")
        recorded_command = []
        original_find_converter = main.find_office_converter
        original_run = main.subprocess.run

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_find_converter():
            return "/usr/bin/soffice"

        def fake_run(args, capture_output, text, timeout):
            recorded_command.extend(args)
            output_dir = Path(args[args.index("--outdir") + 1])
            (output_dir / "office-preview.pdf").write_bytes(b"%PDF-1.4\nfake pdf")
            return FakeResult()

        main.find_office_converter = fake_find_converter
        main.subprocess.run = fake_run
        try:
            main.convert_office_to_pdf(str(source_path), str(pdf_path))
        finally:
            main.find_office_converter = original_find_converter
            main.subprocess.run = original_run
            main.PREVIEW_DIR = original_preview_dir

        profile_arg = next(arg for arg in recorded_command if arg.startswith("-env:UserInstallation="))
        self.assertIn("%20", profile_arg)

    def test_office_preview_cache_path_includes_converter_version(self):
        main = self.main
        record = main.FileRecord(id=42, filename="wide-table.xlsx", md5="spreadsheet-md5")

        pdf_path = Path(main.get_office_preview_pdf_path(record))

        self.assertIn(main.OFFICE_PREVIEW_CACHE_VERSION, pdf_path.name)

    def test_delete_file_removes_office_preview_cache(self):
        main = self.main
        source_path = Path(main.UPLOAD_DIR) / "delete-preview.docx"
        source_path.write_bytes(b"fake office document")
        db = main.SessionLocal()
        try:
            record = main.FileRecord(
                filename="delete-preview.docx",
                filepath=str(source_path),
                md5="delete-preview-md5",
                submitter="admin",
                filesize=source_path.stat().st_size,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            record_id = record.id
            preview_path = Path(main.get_office_preview_pdf_path(record))
            preview_path.write_bytes(b"%PDF-1.4\nfake cached pdf")
            old_preview_path = Path(main.PREVIEW_DIR) / f"{record.id}_v1_delete-preview-md5.pdf"
            old_preview_path.write_bytes(b"%PDF-1.4\nold cached pdf")
        finally:
            db.close()

        response = self.client.delete(f"/api/admin/files/{record_id}", headers=self.headers)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(preview_path.exists())
        self.assertFalse(old_preview_path.exists())
        self.assertFalse(source_path.exists())

    def test_office_preview_rejects_unsupported_file(self):
        main = self.main
        source_path = Path(main.UPLOAD_DIR) / "readme.md"
        source_path.write_text("# demo", encoding="utf-8")
        db = main.SessionLocal()
        try:
            record = main.FileRecord(
                filename="readme.md",
                filepath=str(source_path),
                md5="markdown-md5",
                submitter="admin",
                filesize=source_path.stat().st_size,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            record_id = record.id
        finally:
            db.close()

        response = self.client.get(f"/api/preview/office/{record_id}")

        self.assertEqual(response.status_code, 400)

    def test_office_preview_rejects_missing_source_file(self):
        main = self.main
        missing_path = Path(main.UPLOAD_DIR) / "missing.docx"
        db = main.SessionLocal()
        try:
            record = main.FileRecord(
                filename="missing.docx",
                filepath=str(missing_path),
                md5="missing-md5",
                submitter="admin",
                filesize=0,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            record_id = record.id
        finally:
            db.close()

        response = self.client.get(f"/api/preview/office/{record_id}")

        self.assertEqual(response.status_code, 404)

    def test_admin_can_rename_folder(self):
        response = self.client.post(
            "/api/admin/folders",
            headers=self.headers,
            json={"name": "旧目录", "parent_id": 0},
        )
        self.assertEqual(response.status_code, 200, response.text)
        folder_id = next(
            folder["id"]
            for folder in self.client.get("/api/public/data").json()["folders"]
            if folder["name"] == "旧目录"
        )

        response = self.client.put(
            f"/api/admin/folders/{folder_id}",
            headers=self.headers,
            json={"name": "新目录"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        folders = self.client.get("/api/public/data").json()["folders"]
        self.assertTrue(any(folder["id"] == folder_id and folder["name"] == "新目录" for folder in folders))

    def test_admin_rejects_invalid_folder_names(self):
        for name in ["", ".", "..", "bad/name", "bad\\name", "bad\x00name"]:
            with self.subTest(name=name):
                response = self.client.post(
                    "/api/admin/folders",
                    headers=self.headers,
                    json={"name": name, "parent_id": 0},
                )

                self.assertEqual(response.status_code, 400)

    def test_admin_rejects_invalid_file_group_filename(self):
        main = self.main
        source_path = Path(main.UPLOAD_DIR) / "invalid-rename.bin"
        source_path.write_bytes(b"demo")
        db = main.SessionLocal()
        try:
            record = main.FileRecord(filename="valid.bin", filepath=str(source_path), version="v1.0", submitter="admin")
            db.add(record)
            db.commit()
            db.refresh(record)
            record_id = record.id
        finally:
            db.close()

        for filename in ["", ".", "..", "bad/name.bin", "bad\\name.bin", "bad\x00name.bin"]:
            with self.subTest(filename=filename):
                response = self.client.put(
                    f"/api/admin/files/{record_id}/group",
                    headers=self.headers,
                    json={"filename": filename},
                )

                self.assertEqual(response.status_code, 400)

    def test_delete_folder_rejects_non_empty_folder(self):
        response = self.client.post(
            "/api/admin/folders",
            headers=self.headers,
            json={"name": "非空目录", "parent_id": 0},
        )
        self.assertEqual(response.status_code, 200, response.text)
        folder_id = next(
            folder["id"]
            for folder in self.client.get("/api/public/data").json()["folders"]
            if folder["name"] == "非空目录"
        )
        upload = self.client.post(
            "/api/upload",
            headers=self.headers,
            data={"folder_id": folder_id},
            files={"file": ("keep.bin", b"demo", "application/octet-stream")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)

        response = self.client.delete(f"/api/admin/folders/{folder_id}", headers=self.headers)

        self.assertEqual(response.status_code, 400)
        folders = self.client.get("/api/public/data").json()["folders"]
        self.assertTrue(any(folder["id"] == folder_id for folder in folders))

    def test_delete_folder_rejects_folder_with_child_folder(self):
        parent = self.client.post(
            "/api/admin/folders",
            headers=self.headers,
            json={"name": "父目录", "parent_id": 0},
        )
        self.assertEqual(parent.status_code, 200, parent.text)
        folders = self.client.get("/api/public/data").json()["folders"]
        parent_id = next(folder["id"] for folder in folders if folder["name"] == "父目录")
        child = self.client.post(
            "/api/admin/folders",
            headers=self.headers,
            json={"name": "子目录", "parent_id": parent_id},
        )
        self.assertEqual(child.status_code, 200, child.text)

        response = self.client.delete(f"/api/admin/folders/{parent_id}", headers=self.headers)

        self.assertEqual(response.status_code, 400)

    def test_non_admin_cannot_manage_admin_resources(self):
        user_headers = self.create_user("normal-user", "normal-pw")

        requests = [
            self.client.post("/api/admin/folders", headers=user_headers, json={"name": "nope", "parent_id": 0}),
            self.client.post("/api/admin/users", headers=user_headers, json={"username": "nope", "password": "pw"}),
            self.client.post("/api/admin/settings", headers=user_headers, json={"site_title": "nope"}),
            self.client.post("/api/admin/wechat/test", headers=user_headers),
        ]

        self.assertEqual([response.status_code for response in requests], [403, 403, 403, 403])

    def test_admin_rejects_invalid_user_create_payload(self):
        for payload in [
            {"username": "", "password": "pw"},
            {"username": "bad/name", "password": "pw"},
            {"username": "valid-no-password", "password": ""},
        ]:
            with self.subTest(payload=payload):
                response = self.client.post("/api/admin/users", headers=self.headers, json=payload)

                self.assertEqual(response.status_code, 400)

    def test_file_submitter_can_edit_and_delete_own_file(self):
        owner_headers = self.create_user("file-owner", "owner-pw")
        upload = self.client.post(
            "/api/upload",
            headers=owner_headers,
            data={"version": "v1.0"},
            files={"file": ("owner.bin", b"demo", "application/octet-stream")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        file_id = upload.json()["id"]

        edit = self.client.put(
            f"/api/admin/files/{file_id}",
            headers=owner_headers,
            json={"version": "v1.1"},
        )
        delete = self.client.delete(f"/api/admin/files/{file_id}", headers=owner_headers)

        self.assertEqual(edit.status_code, 200, edit.text)
        self.assertEqual(delete.status_code, 200, delete.text)

    def test_non_admin_cannot_edit_or_delete_other_users_file(self):
        owner_headers = self.create_user("file-owner-2", "owner-pw")
        other_headers = self.create_user("file-other", "other-pw")
        upload = self.client.post(
            "/api/upload",
            headers=owner_headers,
            files={"file": ("private.bin", b"demo", "application/octet-stream")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        file_id = upload.json()["id"]

        edit = self.client.put(
            f"/api/admin/files/{file_id}",
            headers=other_headers,
            json={"version": "v9.9"},
        )
        delete = self.client.delete(f"/api/admin/files/{file_id}", headers=other_headers)

        self.assertEqual(edit.status_code, 403)
        self.assertEqual(delete.status_code, 403)

    def test_admin_can_rename_and_move_file_group_without_touching_other_folders(self):
        main = self.main
        db = main.SessionLocal()
        try:
            source = main.Folder(name="源目录", parent_id=0, creator="admin")
            target = main.Folder(name="目标目录", parent_id=0, creator="admin")
            other = main.Folder(name="其他目录", parent_id=0, creator="admin")
            db.add_all([source, target, other])
            db.commit()
            for item in [source, target, other]:
                db.refresh(item)

            paths = []
            for idx in range(3):
                path = Path(main.UPLOAD_DIR) / f"group-test-{idx}.bin"
                path.write_bytes(b"demo")
                paths.append(str(path))

            group_v1 = main.FileRecord(filename="uboot.img", filepath=paths[0], version="v1.0", folder_id=source.id, submitter="admin")
            group_v2 = main.FileRecord(filename="uboot.img", filepath=paths[1], version="v1.10", folder_id=source.id, submitter="admin")
            other_folder_file = main.FileRecord(filename="uboot.img", filepath=paths[2], version="v9.0", folder_id=other.id, submitter="admin")
            db.add_all([group_v1, group_v2, other_folder_file])
            db.commit()
            db.refresh(group_v1)
            group_v1_id = group_v1.id
            target_id = target.id
            other_id = other.id
        finally:
            db.close()

        response = self.client.put(
            f"/api/admin/files/{group_v1_id}/group",
            headers=self.headers,
            json={"filename": "boot-renamed.img", "folder_id": target_id},
        )

        self.assertEqual(response.status_code, 200, response.text)
        db = main.SessionLocal()
        try:
            moved = db.query(main.FileRecord).filter(main.FileRecord.filename == "boot-renamed.img").all()
            untouched = db.query(main.FileRecord).filter(
                main.FileRecord.filename == "uboot.img",
                main.FileRecord.folder_id == other_id,
            ).all()
            self.assertEqual({record.folder_id for record in moved}, {target_id})
            self.assertEqual(len(moved), 2)
            self.assertEqual(len(untouched), 1)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
