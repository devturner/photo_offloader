import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import google_photos_sync as sync

class TestGooglePhotosSync(unittest.TestCase):
    def test_filters_jpg_and_video_but_not_raw(self):
        for ext in ("*.jpg","*.jpeg","*.mov","*.mp4"): self.assertIn(ext,sync.INCLUDE_EXTENSIONS)
        for ext in ("*.nef","*.arw","*.cr2","*.dng"): self.assertNotIn(ext,sync.INCLUDE_EXTENSIONS)
    def test_status_preserves_last_success_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); status=root/"status.json"
            with patch.object(sync,"SUPPORT_DIR",root),patch.object(sync,"STATUS_PATH",status):
                sync.publish_status("complete","ok"); success=json.loads(status.read_text())["last_success"]
                sync.publish_status("failed","error"); payload=json.loads(status.read_text())
            self.assertEqual(payload["last_success"],success); self.assertEqual(payload["state"],"failed")
    def test_missing_rclone_reports_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); status=root/"status.json"
            with patch.object(sync,"SUPPORT_DIR",root),patch.object(sync,"STATUS_PATH",status),patch.object(sync,"LOCK_PATH",root/".lock"),patch.object(sync,"find_rclone",return_value=None):
                result=sync.run_sync(); payload=json.loads(status.read_text())
            self.assertEqual(result,2); self.assertEqual(payload["state"],"not_configured")
if __name__=="__main__": unittest.main()
