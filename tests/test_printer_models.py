import unittest
from types import SimpleNamespace

from printer_models import PROFILES, UNKNOWN, camera_type, profile
from printer_controls import limits, prepare
from thermal_controls import fans


class PrinterModelTests(unittest.TestCase):
    def core(self, name, model, data=None):
        return SimpleNamespace(names=lambda: [name], printer_config=lambda _: {'name': name, 'model': model},
                               state_data=lambda _: ('IDLE', 0, data or {}, True))

    def test_each_model_has_explicit_capabilities(self):
        for name, expected in PROFILES.items():
            with self.subTest(name=name):
                configured = {'name': 'Workshop printer', 'model': 'Bambu Lab ' + name}
                self.assertEqual(profile(configured), expected)
                core = self.core('Workshop printer', configured['model'])
                actual = limits(core, 'Workshop printer')
                self.assertEqual((actual['nozzle'], actual['bed'], actual['chamber']),
                                 (expected.nozzle, expected.bed, expected.chamber))
                self.assertEqual(camera_type({**configured, 'camera_type': 'auto'}), expected.camera)
                if expected.chamber:
                    self.assertEqual(prepare(core, 'Workshop printer', 'chamber', expected.chamber)[1][0]['ctt_val'], expected.chamber)
                else:
                    with self.assertRaises(ValueError):
                        prepare(core, 'Workshop printer', 'chamber', 40)

    def test_model_aliases_and_old_named_printers(self):
        self.assertEqual(profile({'name': 'BOB (H2D)'}).name, 'H2D')
        self.assertEqual(profile({'name': 'A1 mini Combo'}).name, 'A1 mini')
        self.assertEqual(profile({'name': 'X1 Carbon', 'model': 'X1 Carbon'}).name, 'X1C')
        self.assertEqual(profile({'name': 'H2D Pro Laser', 'model': 'H2D Pro Laser'}).name, 'H2D Pro')
        self.assertIs(profile({'name': 'H2D', 'model': 'Mystery'}), UNKNOWN)

    def test_unknown_model_keeps_conservative_limits_and_explicit_camera_override(self):
        core = self.core('BOB', 'Mystery')
        result = limits(core, 'BOB')
        self.assertEqual((result['nozzle'], result['bed'], result['chamber']), (300, 80, 0))
        self.assertEqual([fan['key'] for fan in fans(core, 'BOB')], ['part'])
        self.assertEqual(camera_type({'name': 'BOB', 'model': 'Mystery', 'camera_type': 'auto'}), '')
        self.assertEqual(camera_type({'name': 'A1', 'camera_type': 'rtsp'}), 'rtsp')
        self.assertEqual(camera_type({'name': 'A1'}), '')

    def test_modern_fans_wait_for_capability_report(self):
        core = self.core('BOB', 'H2C')
        self.assertEqual(fans(core, 'BOB'), [])
        core.state_data = lambda _: ('IDLE', 0, {'support_aux_fan': True}, True)
        self.assertEqual(fans(core, 'BOB'), [])
        core.state_data = lambda _: ('IDLE', 0, {'device': {'airduct': {'modeCur': 1,
            'modeList': [{'modeId': 1, 'ctrl': [16], 'off': []}],
            'parts': [{'id': 16, 'func': 1, 'range': 100 << 16, 'state': 20}]}}}, True)
        self.assertEqual([f['key'] for f in fans(core, 'BOB')], ['part'])
