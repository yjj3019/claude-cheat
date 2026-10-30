"""Independent live-session pointers; all paths and credentials are synthetic."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import openrouterAdvisor as route


class UsageInstanceTest(unittest.TestCase):
    def test_two_instances_have_distinct_handoffs_and_cleanup_is_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = str(uuid.uuid4()), str(uuid.uuid4())
            a, b = route.instance_directory(root, first), route.instance_directory(root, second)
            with patch.object(route, 'HANDOFF_FILE', root / '.session-handoff.json'):
                for name, folder, port, pid in ((first, a, 1111, 101), (second, b, 2222, 202)):
                    route.write_handoff(port=port, nonce='synthetic', proxy_pid=pid, provider='local',
                                        cli_pid=1, mode='ordinary', instance_id=name, instance_dir=folder)
                self.assertEqual(json.loads((a / 'handoff.json').read_text())['instance_id'], first)
                self.assertEqual(json.loads((b / 'handoff.json').read_text())['instance_id'], second)
                route.clear_instance_handoff(a, 202)
                self.assertTrue((a / 'handoff.json').is_file())
                route.clear_instance_handoff(a, 101)
                self.assertFalse((a / 'handoff.json').exists())
                self.assertTrue((b / 'handoff.json').is_file())
                self.assertEqual(json.loads(route.HANDOFF_FILE.read_text())['instance_id'], second)

    def test_bad_instance_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises((ValueError, AttributeError)):
                route.instance_directory(Path(directory), '../escape')


if __name__ == '__main__':
    unittest.main()
