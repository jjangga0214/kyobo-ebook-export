"""Boundary tests that do not use a live account or run the original reader."""
import json
import contextlib
import io
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import kyobo
from formats import local_reference


def book(code, title, fmt='pdf', downloaded=True):
    return {'barcode': code, 'id': code + ':' + fmt, 'title': title, 'author': 'Test',
            'format': fmt, 'downloaded': downloaded, 'owned': True}


class ExportBoundaries(unittest.TestCase):
    def test_ambiguous_query_requires_selection(self):
        with self.assertRaises(kyobo.Blocked) as error:
            kyobo.select_books([book('1', 'React Basics'), book('2', 'React Advanced')], ['React'])
        self.assertEqual(error.exception.code, 'book_selection_required')
        self.assertEqual(len(error.exception.details['candidates']), 2)

    def test_unavailable_duplicate_prefers_actual_download(self):
        rows = [book('1', '선형대수', downloaded=False), book('1', '선형대수', fmt='epub')]
        self.assertEqual(kyobo.select_books(rows, ['선형대수'])[0]['format'], 'epub')
        self.assertEqual(len(kyobo.select_books(rows, [], all_books=True)), 1)

    def test_two_available_formats_remain_ambiguous(self):
        rows = [book('1', 'Book'), book('1', 'Book', fmt='epub')]
        with self.assertRaises(kyobo.Blocked):
            kyobo.select_books(rows, ['Book'])
        self.assertEqual(kyobo.select_books(rows, ['1:epub'])[0]['format'], 'epub')

    def test_unicode_search_and_safe_filename(self):
        self.assertTrue(kyobo.matches(book('1', '샘플 전자책 Deep Study'), '전자책 deep'))
        name = kyobo.filename('../a/b: ' + '책' * 200, 'pdf')
        self.assertNotIn('/', name)
        self.assertLess(len(name.encode()), 256)

    def test_epub_reference_cannot_escape_or_fetch_remote(self):
        for ref in ('../../private', '%2e%2e/%2e%2e/private', '/private', 'https://example.org/book'):
            with self.assertRaises(ValueError):
                local_reference('OEBPS/content.opf', ref)
        self.assertEqual(local_reference('OEBPS/content.opf', 'Text/a%20b.xhtml#p1'), 'OEBPS/Text/a b.xhtml')

    def test_cleanup_rejects_arbitrary_directory_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(kyobo.Blocked):
                kyobo.check_run(root)
            fake = root / ('run-' + 'a' * 32)
            fake.mkdir()
            (fake / 'run.json').write_text(json.dumps({'schema': 1, 'run_id': fake.name}))
            (fake / 'scratch').symlink_to(root, target_is_directory=True)
            with self.assertRaises(kyobo.Blocked):
                kyobo.check_run(fake)

    def test_changed_executable_cannot_get_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            (app / 'iPadB2C').write_bytes(b'unrecognized build')
            with self.assertRaises(kyobo.Blocked) as error:
                kyobo.adapter_for(app)
            self.assertEqual(error.exception.code, 'unsupported_app_build')

    def test_fat_slice_bounds_are_checked(self):
        broken = struct.pack('>II', 0xcafebabe, 1) + struct.pack('>IIIII', 0x100000c, 0, 1000, 100, 0)
        with self.assertRaises(kyobo.Blocked):
            kyobo.arm64_slice(broken)

    def test_source_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (root / 'private').write_text('not to be included')
            (source / 'link').symlink_to(root / 'private')
            with self.assertRaises(kyobo.Blocked):
                list(kyobo.source_files(source))

    def test_known_os_preferences_are_omitted_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'com.apple.security_common.plist').symlink_to('/does/not/exist')
            (root / 'reader.plist').write_bytes(b'local')
            self.assertEqual([p.name for p in kyobo.source_files(root, os_preferences=True)], ['reader.plist'])

    def test_publish_never_overwrites_and_survives_scratch_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / 'source', root / 'book.pdf'
            source.write_bytes(b'%PDF-test')
            digest = kyobo.publish(source, destination)
            source.unlink()
            self.assertEqual(kyobo.sha(destination), digest)
            source.write_bytes(b'different')
            with self.assertRaises(kyobo.Blocked):
                kyobo.publish(source, destination)
            self.assertEqual(destination.read_bytes(), b'%PDF-test')

    def test_all_does_not_include_rental_records(self):
        purchase, rental = book('1', 'Owned'), book('2', 'Rented')
        rental['owned'] = False
        self.assertEqual(kyobo.select_books([purchase, rental], [], all_books=True), [purchase])

    def test_output_lock_prevents_concurrent_manifest_writers(self):
        with tempfile.TemporaryDirectory() as directory:
            with kyobo.output_lock(Path(directory)):
                with self.assertRaises(kyobo.Blocked) as error:
                    with kyobo.output_lock(Path(directory)):
                        self.fail('Concurrent export was admitted')
                self.assertEqual(error.exception.code, 'output_busy')
            with kyobo.output_lock(Path(directory)):
                pass

    def test_only_exact_analytics_timestamp_change_is_noncritical(self):
        before = {'APMExperimentFetchSuccessTimestamp': 1.0, 'unchanged': 'value'}
        after = {**before, 'APMExperimentFetchSuccessTimestamp': 2.0}
        self.assertTrue(kyobo.telemetry_timestamp_only(before, after))
        self.assertFalse(kyobo.telemetry_timestamp_only(before, {**after, 'readingPosition': 20}))
        self.assertFalse(kyobo.telemetry_timestamp_only(before, {**after, 'unchanged': 'different'}))
        self.assertFalse(kyobo.telemetry_timestamp_only(before, {**after, 'APMExperimentFetchSuccessTimestamp': 'not numeric'}))

    def test_large_pdf_warning_output_is_bounded_but_counted(self):
        validation = {'format': 'pdf', 'pages': 594, 'encrypted': False, 'text_characters': 1000,
                      'replacement_characters': 683, 'poppler_warnings': ['warning'] * 20000,
                      'pdfkit': {'rendered_pages': 594, 'replacement_characters': 6}}
        summary = kyobo.console_validation(validation)
        self.assertEqual(summary['poppler_warning_count'], 20000)
        self.assertEqual(summary['pdfkit_replacement_characters'], 6)
        self.assertLess(len(json.dumps(summary)), 1000)

    def test_export_requires_a_chosen_output_directory(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            kyobo.parser().parse_args(['export', '--book', 'sample:pdf'])
        self.assertEqual(error.exception.code, 2)

    def test_export_accepts_contextual_relative_and_absolute_outputs(self):
        for destination in ('./exports', '/tmp/custom-ebook-library', '~/Documents/reading'):
            parsed = kyobo.parser().parse_args(['export', '--book', 'sample:pdf', '--out', destination])
            self.assertEqual(parsed.out, destination)


if __name__ == '__main__':
    unittest.main()
