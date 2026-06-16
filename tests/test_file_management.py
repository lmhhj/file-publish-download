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
