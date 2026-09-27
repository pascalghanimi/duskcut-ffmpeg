import unittest
from capture_environment import capture

class CaptureTests(unittest.TestCase):
    def test_known_build_values_kept(self):
        self.assertEqual(capture({'CC':'x86_64-w64-mingw32-gcc','FF_CFLAGS':'-O2 -I/opt/ffbuild/include','SOURCE_DATE_EPOCH':'123'}),
            {'CC':'x86_64-w64-mingw32-gcc','FF_CFLAGS':'-O2 -I/opt/ffbuild/include','SOURCE_DATE_EPOCH':'123'})
    def test_arbitrary_secret_environment_never_exported(self):
        captured=capture({'CC':'gcc','OPENAI_API_KEY':'my-secret','CLIENT_SECRET':'private','CUSTOMER_EMAIL':'private@example.com','HOME':'/private/user'})
        self.assertEqual(captured,{'CC':'gcc'})
    def test_secrets_inside_allowed_flags_fail_without_printing_value(self):
        values=['-DAPI_KEY=private','-DPASSWORD=private','Bearer private-token',
                'https://user:password@example.com','https://example.com?token=private',
                'sk-abcdefghijklmnopqrstuvwxyz0123456789','-----BEGIN PRIVATE KEY-----', 'good\nprivate']
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as error: capture({'FF_CFLAGS':value})
                self.assertEqual(str(error.exception),'Potential secret in build variable: FF_CFLAGS')

if __name__=='__main__': unittest.main()
