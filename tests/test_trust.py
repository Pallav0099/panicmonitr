import tempfile
import threading
import unittest
from pathlib import Path

import nacl.encoding
import nacl.signing

from src.log import OP_SET_TAGS, TrustLog
from src.trust import PeerTrustManager


def node_id_for(key: nacl.signing.SigningKey) -> str:
    return key.verify_key.encode(encoder=nacl.encoding.HexEncoder).decode()


class TrustTagMutationTests(unittest.TestCase):
    def make_manager(self, tmp: str) -> tuple[PeerTrustManager, TrustLog, str]:
        signing_key = nacl.signing.SigningKey.generate()
        own_node_id = node_id_for(signing_key)
        log = TrustLog(Path(tmp) / "log.jsonl", signing_key=signing_key, own_node_id=own_node_id)
        manager = PeerTrustManager(log, Path(tmp) / "peers.json", own_node_id=own_node_id)
        peer_id = "a" * 64
        self.assertTrue(manager.add_peer(peer_id, alias="peer-a"))
        return manager, log, peer_id

    def test_concurrent_add_tag_emits_one_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            manager, log, peer_id = self.make_manager(d)
            start = threading.Barrier(8)
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    start.wait()
                    self.assertTrue(manager.add_tag(peer_id, "prod"))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            mutations = [e for e in log.entries() if e.type == OP_SET_TAGS]
            self.assertEqual(len(mutations), 1)
            self.assertEqual(mutations[0].data["tags"], ["prod"])

    def test_concurrent_remove_tag_emits_one_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            manager, log, peer_id = self.make_manager(d)
            self.assertTrue(manager.add_tag(peer_id, "prod"))
            baseline = len([e for e in log.entries() if e.type == OP_SET_TAGS])
            start = threading.Barrier(8)
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    start.wait()
                    self.assertTrue(manager.remove_tag(peer_id, "prod"))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            mutations = [e for e in log.entries() if e.type == OP_SET_TAGS]
            self.assertEqual(len(mutations), baseline + 1)
            self.assertEqual(mutations[-1].data["tags"], [])
