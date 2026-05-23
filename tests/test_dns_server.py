import json
import socket
import struct
import tempfile
import unittest
from pathlib import Path

from dns_server import DNSServer


def encode_qname(name):
    payload = b""
    for part in name.split("."):
        payload += bytes([len(part)]) + part.encode("ascii")
    return payload + b"\x00"


def make_query(name, query_id=0x3054, qtype=1):
    header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    question = encode_qname(name) + struct.pack("!HH", qtype, 1)
    return header + question


class DNSServerTest(unittest.TestCase):
    def make_server(self):
        tmpdir = tempfile.TemporaryDirectory()
        record_file = Path(tmpdir.name) / "dns_records.json"
        record_file.write_text(
            json.dumps(
                {
                    "server_ip": "192.168.1.1",
                    "server_mac": "7e:49:b3:f0:f9:99",
                    "ttl": 60,
                    "records": {
                        "web.cs305.local": "192.168.1.3",
                        "h1.cs305.local": "192.168.1.2",
                    },
                }
            ),
            encoding="utf-8",
        )
        self.addCleanup(tmpdir.cleanup)
        return DNSServer(str(record_file))

    def test_answer_known_a_record(self):
        server = self.make_server()

        response = server.build_response(make_query("web.cs305.local"))

        self.assertEqual(response[:2], struct.pack("!H", 0x3054))
        flags = struct.unpack("!H", response[2:4])[0]
        self.assertTrue(flags & 0x8000)
        self.assertEqual(flags & 0x000F, 0)
        self.assertEqual(struct.unpack("!H", response[6:8])[0], 1)
        self.assertIn(socket.inet_aton("192.168.1.3"), response)

    def test_unknown_name_returns_nxdomain(self):
        server = self.make_server()

        response = server.build_response(make_query("missing.cs305.local"))

        flags = struct.unpack("!H", response[2:4])[0]
        self.assertEqual(flags & 0x000F, 3)
        self.assertEqual(struct.unpack("!H", response[6:8])[0], 0)

    def test_unsupported_query_type_has_no_answer(self):
        server = self.make_server()

        response = server.build_response(make_query("web.cs305.local", qtype=28))

        flags = struct.unpack("!H", response[2:4])[0]
        self.assertEqual(flags & 0x000F, 0)
        self.assertEqual(struct.unpack("!H", response[6:8])[0], 0)


if __name__ == "__main__":
    unittest.main()
