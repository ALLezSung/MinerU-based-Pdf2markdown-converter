"""Hosted path tests: fixtures only, no requests or actual PDF conversion."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from hosted_api import chunk_plan, safe_extract, acquire_chunk
from hosted_worker import load_bundle, render_v1, continuation, is_misclassified_caption


class HostedChecks(unittest.TestCase):
    def test_formula_label_print_leaders_removed(self):
        self.assertIn(r'\tag{B.12}',render_v1({'type':'equation','text':r'x=1\tag{……( B. 12}'}))

    def test_table_math_entities_are_valid_tex(self):
        rendered=render_v1({'type':'table','table_body':r'<table><tr><td>$x&lt;2$</td><td>\( y&gt;6$</td></tr></table>'})
        self.assertIn(r'$x\lt 2$',rendered)
        self.assertIn(r'$ y\gt 6$',rendered)
        self.assertNotIn(r'\(',rendered)

    def test_misclassified_page_footnote_is_preserved(self):
        self.assertEqual(render_v1({'type':'page_footnote','text':'①1亩约666.7平方米。'}),'①1亩约666.7平方米。')

    def test_original_page_tables_prevent_overlap_loss(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            folder=Path(temp)
            native=[]
            for index,label in enumerate(['A','B']):
                native.append({'page_idx':index,'page_size':[100,100],
                    'preproc_blocks':[{'type':'table','bbox':[10,10,80,40],
                        'blocks':[{'type':'table_body','lines':[{'spans':[{'type':'table','html':f'<table><tr><td>{label}</td></tr></table>'}]}]}]}]})
            (folder/'layout.json').write_text(json.dumps({'pdf_info':native}),encoding='utf-8')
            items=[{'type':'table','page_idx':0,'bbox':[100,100,800,400],
                    'table_body':'<table><tr><td>A</td></tr><tr><td>B</td></tr></table>'},
                   {'type':'table','page_idx':1,'bbox':[100,100,800,400],'img_path':''}]
            (folder/'a_content_list.json').write_text(json.dumps(items),encoding='utf-8')
            actual,_=load_bundle(folder,[40,41],unmerge_tables=True)
            self.assertEqual(actual[0]['table_body'],'<table><tr><td>A</td></tr></table>')
            self.assertEqual(actual[1]['table_body'],'<table><tr><td>B</td></tr></table>')

    def test_chinese_page_continuation(self):
        left=('a',8,'水上管道的使用受到浮箱吃水的限制或受到管道和处理管道的辅助设备吃水的限',{'type':'text','bbox':[110,20,500,90]})
        right=('b',9,'制。因此这项技术不适用。',{'type':'text','bbox':[108,20,500,90]})
        self.assertTrue(continuation(left,right))
        self.assertFalse(continuation(left,('b',9,'第三章 其他条件',right[3])))

    def test_caption_requires_image_geometry(self):
        b={'type':'text','text':'图 6.13 扩散器','bbox':[685,663,813,678]}
        self.assertTrue(is_misclassified_caption(b,[{'type':'image','bbox':[618,453,873,654]}]))
        self.assertFalse(is_misclassified_caption(b,[]))
        self.assertFalse(is_misclassified_caption(dict(b,text='图 6.13 显示了施工方法。'),[{'type':'image','bbox':[618,453,873,654]}]))

    def test_chunk_ownership_no_gaps_or_duplicates(self):
        chosen = list(range(1, 522))
        chunks = chunk_plan(chosen)
        self.assertEqual([p for c in chunks for p in c["owned_pages"]], chosen)
        self.assertTrue(all(len(c["upload_pages"]) <= 200 for c in chunks))
        self.assertEqual(chunks[0]["upload_pages"][-2:], [181, 182])
        self.assertEqual(chunks[1]["upload_pages"][:2], [179, 180])

    def test_discontinuous_pilot_not_glued(self):
        chunks = chunk_plan([5, 6, 7, 30, 31])
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["upload_pages"], [5, 6, 7])

    def test_chunk_limits_strict(self):
        for chosen, size, overlap in [([1], 200, 2), ([], 180, 2), ([2, 1], 180, 2)]:
            with self.subTest(chosen=chosen), self.assertRaises(ValueError):
                chunk_plan(chosen, size, overlap)

    def test_table_structure_and_notes_preserved(self):
        html = '<table><tr><td rowspan="2">A</td><td>$x_1$</td></tr></table>'
        rendered = render_v1({"type": "table", "table_body": html,
                              "table_caption": ["表1"], "table_footnote": ["注：条件X"]})
        self.assertIn(html, rendered)
        self.assertIn("注：条件X", rendered)

    def test_formula_is_latex(self):
        self.assertEqual(render_v1({"type": "equation", "text": "Q=A\\cdot v"}),
                         "$$\nQ=A\\cdot v\n$$")

    def test_missing_formula_or_table_not_silently_ignored(self):
        for item in [{"type": "equation", "img_path": "f.png"},
                     {"type": "table", "img_path": "t.png"}, {"type": "unknown"}]:
            with self.subTest(item=item), self.assertRaises(ValueError):
                render_v1(item)

    def test_zip_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            file = Path(temp) / "input.zip"
            with zipfile.ZipFile(file, "w") as archive:
                archive.writestr("../escaped.txt", "bad")
            with self.assertRaises(ValueError):
                safe_extract(file, Path(temp) / "raw")

    def test_blank_page_coverage_from_layout(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            folder = Path(temp)
            (folder / "layout.json").write_text(json.dumps({"pdf_info": [{"page_idx": 0}, {"page_idx": 1}]}))
            (folder / "a_content_list.json").write_text(json.dumps([{"type": "text", "text": "A", "page_idx": 0}]))
            blocks, _ = load_bundle(folder, [8, 9])
            self.assertEqual(len(blocks), 1)

    def test_missing_layout_page_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            folder = Path(temp)
            (folder / "layout.json").write_text(json.dumps({"pdf_info": [{"page_idx": 0}]}))
            with self.assertRaises(ValueError):
                load_bundle(folder, [8, 9])

    def test_offline_clean_cannot_upload(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, self.assertRaises(ValueError):
            acquire_chunk("unused.pdf", {"source_sha256": "abc", "upload_pages": [1]},
                          temp, offline=True)


if __name__ == "__main__":
    unittest.main()
