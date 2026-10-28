import io
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from scripts import convert


def archive_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        for name, data in entries:
            archive.writestr(name, data)
    buf.seek(0)
    return buf


class IngestionSecurityTests(unittest.TestCase):
    def test_bad_archives_and_network_failure_preserve_existing_input(self):
        symlink = zipfile.ZipInfo('link.txt')
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        archives = [io.BytesIO(b'not a zip'), archive_bytes([('../evil.txt', 'x')]),
                    archive_bytes([('/evil.txt', 'x')]), archive_bytes([(symlink, 'target')]),
                    archive_bytes([('one/stops.txt', 'x'), ('two/stops.txt', 'y')])]
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'feed'
            target.mkdir()
            (target / 'old.txt').write_text('old')
            for feed in archives:
                with self.subTest(feed=feed), patch.object(convert, 'secure_open', return_value=feed):
                    with self.assertRaises(ValueError):
                        convert.download_and_extract_zip('https://example.test/feed', target)
                    self.assertEqual((target / 'old.txt').read_text(), 'old')
            with patch.object(convert, 'secure_open', side_effect=URLError('offline')):
                with self.assertRaises(ValueError):
                    convert.download_and_extract_zip('https://example.test/feed', target)
            self.assertEqual((target / 'old.txt').read_text(), 'old')

    def test_limits_reject_download_entry_count_and_expanded_size(self):
        cases = [('MAX_DOWNLOAD_BYTES', 10, [('stops.txt', 'data')]),
                 ('MAX_ZIP_ENTRIES', 1, [('stops.txt', 'x'), ('trips.txt', 'x')]),
                 ('MAX_MEMBER_BYTES', 2, [('stops.txt', 'long')]),
                 ('MAX_EXPANDED_BYTES', 2, [('stops.txt', 'long')])]
        with tempfile.TemporaryDirectory() as temp:
            for constant, limit, entries in cases:
                with self.subTest(constant=constant), patch.object(convert, constant, limit), patch.object(convert, 'secure_open', return_value=archive_bytes(entries)):
                    with self.assertRaises(ValueError):
                        convert.download_and_extract_zip('https://example.test/feed', Path(temp) / 'feed')

    def test_rejects_unsupported_compression(self):
        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as temp:
                payload = io.BytesIO()
                with zipfile.ZipFile(payload, 'w', compression=method) as archive:
                    archive.writestr('stops.txt', 'data')
                payload.seek(0)
                with patch.object(convert, 'secure_open', return_value=payload), self.assertRaises(ValueError):
                    convert.download_and_extract_zip('https://example.test/feed', Path(temp) / 'feed')

    def test_https_only_redirect_and_token_scope(self):
        with self.assertRaises(ValueError):
            convert.build_http_request('http://example.test/feed')
        with patch.dict('os.environ', {'GITHUB_TOKEN': 'test-token'}):
            self.assertFalse(convert.build_http_request('https://example.test/feed').has_header('Authorization'))
            self.assertTrue(convert.build_http_request('https://api.github.com/repos/a/b', github_api=True).has_header('Authorization'))
            for url in ('https://evil.test/', 'https://api.github.com.evil.test/', 'https://api.github.com:444/'):
                with self.assertRaises(ValueError):
                    convert.build_http_request(url, github_api=True)
        def redirect(url):
            return HTTPError('https://api.github.com/resource', 302, 'redirect', {'Location': url}, None)
        for location, token in [('http://evil.test/', False), ('https://evil.test/', True)]:
            request = Request('https://api.github.com/resource', headers={'Authorization': 'Bearer test'} if token else {})
            with self.subTest(location=location), patch.object(convert, 'build_opener') as opener:
                opener.return_value.open.side_effect = redirect(location)
                with self.assertRaises(ValueError):
                    convert.secure_open(request, time.monotonic() + 10)
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_redirect_limit_and_deadline(self):
        error = lambda: HTTPError('https://example.test/', 302, 'redirect', {'Location': '/next'}, None)
        with patch.object(convert, 'build_opener') as opener:
            opener.return_value.open.side_effect = lambda *args, **kwargs: (_ for _ in ()).throw(error())
            with self.assertRaises(ValueError):
                convert.secure_open(Request('https://example.test/'), time.monotonic() + 10)
            self.assertEqual(opener.return_value.open.call_count, convert.MAX_REDIRECTS + 1)
        with self.assertRaises(TimeoutError):
            convert.read_bounded(io.BytesIO(b'data'), 10, time.monotonic() - 1)

    def test_xml_declarations_and_invalid_cache_rejected(self):
        evil = b'<!DOCTYPE x [<!ENTITY e "bad">]><x>&e;</x>'
        with self.assertRaises(ValueError):
            convert.parse_pvta_route_details_records(evil)
        for encoding in ('utf-16', 'utf-32', 'utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be'):
            with self.subTest(encoding=encoding), self.assertRaises((ValueError, UnicodeError)):
                convert.parse_pvta_route_details_records(evil.decode().encode(encoding))
        with self.assertRaises(ValueError):
            convert.parse_pvta_route_details_records(b'<?xml version="1.0" encoding="ISO-8859-1"?><x/>')
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / 'cache.xml'
            cache.write_bytes(evil)
            with patch.object(convert, 'secure_open', side_effect=URLError('offline')):
                self.assertIsNone(convert.load_pvta_route_details_xml(cache))
            with patch.object(convert, 'MAX_XML_BYTES', 4), patch.object(convert, 'secure_open', side_effect=URLError('offline')):
                self.assertIsNone(convert.load_pvta_route_details_xml(cache))
            cache.write_bytes(b'[]')
            with patch.object(convert, 'secure_open', side_effect=URLError('offline')):
                self.assertIsNone(convert.load_pvta_route_details_xml(cache))
            valid = b'<ArrayOfRoute><Route><RouteId>1</RouteId><RouteAbbreviation>A</RouteAbbreviation></Route></ArrayOfRoute>'
            cache.write_bytes(valid)
            with patch.object(convert, 'secure_open', return_value=io.BytesIO(evil)):
                self.assertEqual(convert.load_pvta_route_details_xml(cache), valid)
            self.assertEqual(cache.read_bytes(), valid)


if __name__ == '__main__':
    unittest.main()
