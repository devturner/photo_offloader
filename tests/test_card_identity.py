"""Telling camera cards apart: every card from one camera has the same volume name."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import card_identity

# What macOS 26 reported for the user's two cards on 2026-11-11 (serial number changed).
SD_VOLUME = {
    "BusProtocol": "Secure Digital",
    "ParentWholeDisk": "disk4",
    "TotalSize": 63831146496,
    "MediaName": "",
}
SD_WHOLE_DISK = {
    "BusProtocol": "Secure Digital",
    "MediaName": "Built In SDXC Reader",
    "TotalSize": 63864569856,
}
SD_READER = {
    "SPCardReaderDataType": [
        {
            "_items": [
                {
                    "_name": "SDXC Card (Class 10)",
                    "bsd_name": "disk4",
                    "size_in_bytes": 63864569856,
                    "spcardreader_card_manufacturer-id": "0x03",
                    "spcardreader_card_productname": "SDSP64G",
                    "spcardreader_card_serialnumber": "0x12345678",
                }
            ],
            "_name": "spcardreader",
        }
    ]
}
CFEXPRESS_VOLUME = {
    "BusProtocol": "USB",
    "ParentWholeDisk": "disk5",
    "TotalSize": 255993577472,
    "MediaName": "",
}
CFEXPRESS_WHOLE_DISK = {
    "BusProtocol": "USB",
    "MediaName": "ASM236X NVME",
    "TotalSize": 256060514304,
}


class TestDescribe(unittest.TestCase):
    def test_an_sd_card_in_the_built_in_reader_gives_its_maker_and_serial(self):
        identity = card_identity.describe(SD_VOLUME, SD_WHOLE_DISK, SD_READER)
        self.assertEqual(identity["kind"], "SD")
        self.assertEqual(identity["size_bytes"], 63864569856)
        self.assertEqual(identity["maker"], "SanDisk")
        self.assertEqual(identity["model"], "SDSP64G")
        self.assertEqual(identity["serial"], "0x12345678")
        self.assertEqual(card_identity.label(identity), "64 GB SanDisk SD")

    def test_a_card_behind_an_nvme_bridge_is_cfexpress_without_a_serial(self):
        identity = card_identity.describe(CFEXPRESS_VOLUME, CFEXPRESS_WHOLE_DISK, SD_READER)
        self.assertEqual(identity["kind"], "CFexpress")
        self.assertNotIn("serial", identity)  # the bridge's serial is the reader's, not the card's
        self.assertEqual(card_identity.label(identity), "256 GB CFexpress")

    def test_a_usb_sd_reader_still_says_sd(self):
        identity = card_identity.describe(
            {"BusProtocol": "USB", "TotalSize": 31_000_000_000},
            {"MediaName": "SD/MMC Card Reader", "TotalSize": 31_914_983_424},
            {},
        )
        self.assertEqual(card_identity.label(identity), "32 GB SD")

    def test_an_unknown_reader_gives_the_size_alone(self):
        identity = card_identity.describe(
            {"TotalSize": 127_000_000_000}, {"MediaName": "Mass Storage"}, {}
        )
        self.assertEqual(card_identity.label(identity), "128 GB")

    def test_nothing_known_gives_no_label(self):
        self.assertEqual(card_identity.label({}), "")
        self.assertEqual(card_identity.label(None), "")

    def test_an_unknown_maker_id_is_left_out(self):
        reader = json.loads(json.dumps(SD_READER))
        reader["SPCardReaderDataType"][0]["_items"][0]["spcardreader_card_manufacturer-id"] = "0xee"
        identity = card_identity.describe(SD_VOLUME, SD_WHOLE_DISK, reader)
        self.assertNotIn("maker", identity)
        self.assertEqual(card_identity.label(identity), "64 GB SD")


class TestCapacity(unittest.TestCase):
    def test_sizes_read_as_printed_on_the_card(self):
        for size, text in [
            (63_864_569_856, "64 GB"),
            (256_060_514_304, "256 GB"),
            (127_865_454_592, "128 GB"),
            (31_914_983_424, "32 GB"),
            (512_110_190_592, "512 GB"),
            (1_000_204_886_016, "1 TB"),
            (2_000_398_934_016, "2 TB"),
            (165_000_000_000, "160 GB"),
            (650_000_000_000, "640 GB"),
            (90_000_000_000, "90 GB"),  # no usual size near it
        ]:
            self.assertEqual(card_identity.capacity_text(size), text, size)


class TestRead(unittest.TestCase):
    """read() asks diskutil and system_profiler, and never fails an import."""

    def fake_run(self, outputs):
        def run(args, **kwargs):
            key = args[-1] if args[0].endswith("diskutil") else "reader"
            out = outputs.get(key)
            if isinstance(out, Exception):
                raise out
            return subprocess.CompletedProcess(args, 0 if out is not None else 1, out or b"", b"")

        return run

    def test_reads_the_volume_the_whole_disk_and_the_reader(self):
        import plistlib

        outputs = {
            "/Volumes/NIKON Z 8": plistlib.dumps(SD_VOLUME),
            "disk4": plistlib.dumps(SD_WHOLE_DISK),
            "reader": json.dumps(SD_READER).encode(),
        }
        with patch.object(card_identity.subprocess, "run", side_effect=self.fake_run(outputs)):
            identity = card_identity.read(Path("/Volumes/NIKON Z 8"))
        self.assertEqual(card_identity.label(identity), "64 GB SanDisk SD")

    def test_a_tool_that_fails_or_hangs_gives_what_is_known(self):
        import plistlib

        outputs = {
            "/Volumes/NIKON Z 8": plistlib.dumps(SD_VOLUME),
            "disk4": subprocess.TimeoutExpired("diskutil", 5),
            "reader": b"not json",
        }
        with patch.object(card_identity.subprocess, "run", side_effect=self.fake_run(outputs)):
            identity = card_identity.read(Path("/Volumes/NIKON Z 8"))
        self.assertEqual(identity, {"kind": "SD", "size_bytes": 63831146496})

    def test_nothing_readable_gives_an_empty_identity(self):
        with patch.object(card_identity.subprocess, "run", side_effect=OSError("no diskutil")):
            self.assertEqual(card_identity.read(Path("/Volumes/X")), {})


if __name__ == "__main__":
    unittest.main()
