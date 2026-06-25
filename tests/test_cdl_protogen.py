"""test_cdl_protogen.py -- golden + drift tests for the C-header emitter.

Pins the generated cdl_proto.h (golden file) and -- the point of P1 -- proves the
shared constant block is genuinely shared: the exact lines render_proto_defines()
emits also appear verbatim inside a generated target-stub header, so cdl_proto.h
and the stub cannot drift (05 SS 4.2). Regenerate the golden with:

    python -c "from cc5x_setcc_native_lib import cdl_protogen as g; \\
        open('tests/golden/cdl_proto.h','w').write(g.render_c_header())"
"""
from __future__ import annotations

import unittest
from pathlib import Path

from cc5x_setcc_native_lib import cdl_protogen as g

GOLDEN = Path(__file__).resolve().parent / "golden" / "cdl_proto.h"


class CHeaderGolden(unittest.TestCase):
    def test_matches_golden_file(self):
        self.assertEqual(g.render_c_header(), GOLDEN.read_text(),
                         "cdl_proto.h changed; review and regenerate the golden (see module docstring)")

    def test_constants_present_with_correct_values(self):
        h = g.render_c_header()
        for line in ("#define CDL_FLAG          0x7E",
                     "#define CDL_CRC_POLY      0x07",
                     "#define CDL_T_HELLO     0x01",
                     "#define CDL_T_SET_TRACE 0x87",
                     "#define CDL_P_RELAY    0xF0",
                     "#define CDL_P_STATUS   0xF1"):
            self.assertIn(line, h)

    def test_has_include_guard(self):
        h = g.render_c_header()
        self.assertIn("#ifndef CDL_PROTO_H", h)
        self.assertIn("#endif // CDL_PROTO_H", h)


class SharedBlockDoesNotDrift(unittest.TestCase):
    """The device-independent block is one renderer; assert the stub uses it."""

    def test_stub_header_embeds_render_proto_defines(self):
        # Generate a synthetic stub and confirm the shared block appears verbatim.
        from cc5x_setcc_native_lib import debuggen
        from test_debuggen import make_metadata  # synthetic device, no packs

        payload = {"tier": "full", "transport": {"tx_pin": "RB7", "brg": 25}}
        header = debuggen.generate_debug_stub(make_metadata(), payload).monitor_h

        block = "\n".join(g.render_proto_defines())
        self.assertIn(block, header)
        # ...and the probe-only types must NOT leak into the target stub.
        self.assertNotIn("CDL_P_RELAY", header)


if __name__ == "__main__":
    unittest.main()
