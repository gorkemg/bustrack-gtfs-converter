import sqlite3
from io import BytesIO
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import convert
from scripts.validate_release import copy_bounded, validate_release


class ReleaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'pvta.sqlite'
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT)')
            db.executemany('INSERT INTO app_metadata VALUES (?, ?)', [('agency_id', 'pvta'), ('schema_version', '1.0')])
            for table in ('stops', 'trips', 'stop_times'):
                db.execute(f'CREATE TABLE {table} (id TEXT)')
                db.execute(f"INSERT INTO {table} VALUES ('1')")
        self.archive = self.root / 'pvta.sqlite.zip'

    def make_archive(self, name='pvta.sqlite', content=None):
        with zipfile.ZipFile(self.archive, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(name, self.database.read_bytes() if content is None else content)

    def test_bounded_copy_rejects_excess_and_short_data(self):
        for data, expected in [(b'1234', 3), (b'12', 3)]:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    copy_bounded(BytesIO(data), BytesIO(), expected)
        with patch('scripts.validate_release.MAX_DATABASE_SIZE', 3):
            with self.assertRaises(ValueError):
                copy_bounded(BytesIO(b'1234'), BytesIO(), 4)
        destination = BytesIO()
        copy_bounded(BytesIO(b'123'), destination, 3)
        self.assertEqual(destination.getvalue(), b'123')

    def test_valid_archive(self):
        self.make_archive()
        validate_release(self.archive, 'pvta')

    def test_rejects_invalid_contents_and_wrong_agency(self):
        for name, content, agency in [('pvta.sqlite', b'not sqlite', 'pvta'), ('../pvta.sqlite', None, 'pvta'), ('pvta.sqlite', None, 'uta')]:
            with self.subTest(name=name, agency=agency):
                self.make_archive(name, content)
                with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                    validate_release(self.archive, agency)

    def test_rejects_duplicate_and_symlink_entries(self):
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr('pvta.sqlite', self.database.read_bytes())
            archive.writestr('extra', b'extra')
        with self.assertRaises(ValueError):
            validate_release(self.archive, 'pvta')
        info = zipfile.ZipInfo('pvta.sqlite')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr(info, 'target')
        with self.assertRaises(ValueError):
            validate_release(self.archive, 'pvta')

    def test_rejects_wrong_schema_and_empty_core_table(self):
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE app_metadata SET value='2.0' WHERE key='schema_version'")
        self.make_archive()
        with self.assertRaises(ValueError):
            validate_release(self.archive, 'pvta')
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE app_metadata SET value='1.0' WHERE key='schema_version'")
            db.execute('DELETE FROM stops')
        self.make_archive()
        with self.assertRaises(ValueError):
            validate_release(self.archive, 'pvta')

    def test_noop_and_failure_have_distinct_status(self):
        status = self.root / 'status'
        config = [{'id': 'pvta', 'url': 'https://example.test/feed.zip'}]
        args = ['convert.py', '--agency', 'pvta', '--status-file', str(status)]
        with patch('sys.argv', args), patch.object(convert, 'load_agencies_config', return_value=config), patch.object(convert, 'needs_update', return_value=(False, {}, {}, 'unchanged')):
            self.assertEqual(convert.main(), 0)
        self.assertEqual(status.read_text(), 'unchanged\n')
        with patch('sys.argv', args), patch.object(convert, 'load_agencies_config', side_effect=ValueError('bad config')):
            self.assertEqual(convert.main(), 1)
        self.assertFalse(status.exists())


if __name__ == '__main__':
    unittest.main()
