"""Offline regression tests: provided inputs never fetch; remote license gate remains."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('fetch_arxiv', Path(__file__).with_name('fetch_arxiv.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class InputTests(unittest.TestCase):
    def run_cli(self, root, *args):
        with patch.object(sys, 'argv', ['fetch_arxiv.py', '1801.03583v2', str(root / 'work'), *map(str, args)]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return m.main()

    def test_real_user_pdf_offline(self):
        pdf = Path('/Users/xiaolibird/Desktop/Lab/Reading/2026-10-13-缺失数据因果图理论/mohan2021Graphical_arxivv2.pdf')
        if not pdf.exists():
            self.skipTest('local integration fixture not present')
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'get', side_effect=AssertionError('must not fetch')):
            root = Path(tmp)
            self.assertEqual(self.run_cli(root, '--provided-file', pdf), 0)
            meta = json.loads((root / 'work/meta.json').read_text())
            self.assertEqual(meta['translation_basis'], 'user_provided_content')
            self.assertEqual(meta['mode'], 'pdf')
            self.assertEqual(Path(meta['input_file']).read_bytes(), pdf.read_bytes())

    def test_provided_tex(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'get', side_effect=AssertionError('must not fetch')):
            root = Path(tmp)
            tex = root / 'main.tex'
            tex.write_text(r'\documentclass{article}\begin{document}Hello\end{document}')
            self.assertEqual(self.run_cli(root, '--provided-file', tex), 0)
            self.assertEqual((root / 'work/eprint/main.tex').read_bytes(), tex.read_bytes())

    def test_invalid_inputs(self):
        for name, data in [('broken.pdf', b'not a pdf'), ('paper.txt', b'text'), ('missing.pdf', None)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp, patch.object(m, 'get', side_effect=AssertionError('must not fetch')):
                root = Path(tmp)
                p = root / name
                if data is not None:
                    p.write_bytes(data)
                self.assertEqual(self.run_cli(root, '--provided-file', p), 2)
                self.assertFalse((root / 'work/meta.json').exists())

    def test_remote_nonexclusive_nd_unknown_stop_before_download(self):
        for license_html in ['https://arxiv.org/licenses/nonexclusive-distrib/1.0/', 'https://creativecommons.org/licenses/by-nd/4.0/', 'no license']:
            with self.subTest(license=license_html), tempfile.TemporaryDirectory() as tmp, patch.object(m, 'get', return_value=license_html.encode()) as get:
                root = Path(tmp)
                self.assertEqual(self.run_cli(root), 3)
                self.assertEqual(get.call_count, 1)
                self.assertFalse((root / 'work/eprint').exists())

    def test_remote_cc_by_downloads_latex(self):
        data = io.BytesIO()
        tex = b'\\documentclass{article}\n\\begin{document}Hello\\end{document}'
        with tarfile.open(fileobj=data, mode='w:gz') as tf:
            info = tarfile.TarInfo('main.tex')
            info.size = len(tex)
            tf.addfile(info, io.BytesIO(tex))
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'get', side_effect=[b'https://creativecommons.org/licenses/by/4.0/', data.getvalue()]):
            root = Path(tmp)
            self.assertEqual(self.run_cli(root), 0)
            self.assertEqual((root / 'work/eprint/main.tex').read_bytes(), tex)


if __name__ == '__main__':
    unittest.main()
