"""Offline contract checks only; never import OCR models or convert real PDFs."""
import unittest
from workflow import pages
from mineru_worker import check_pages, issues_for_markdown, DROP_TYPES


class WorkflowChecks(unittest.TestCase):
    def test_ranges_are_one_based_inclusive(self):
        self.assertEqual(pages("2-4,3,7", 8), [2, 3, 4, 7])

    def test_never_clip_ranges(self):
        for value in ["0", "1-9", "4-2", "", "1;2"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                pages(value, 8)

    def test_full_document(self):
        self.assertEqual(pages("all", 3), [1, 2, 3])

    def test_missing_count(self):
        with self.assertRaises(ValueError):
            pages("all", None)

    def test_missing_and_unexpected(self):
        self.assertEqual(check_pages([2, 3, 4], [2, 4, 5]), ([3], [5]))

    def test_duplicate_page_rejected(self):
        with self.assertRaises(ValueError):
            check_pages([1, 2], [1, 1, 2])

    def test_formula_fallback_is_an_issue(self):
        self.assertEqual(issues_for_markdown("![公式](images/f.png)")[0]["code"], "image_fallback")

    def test_html_images_detected(self):
        self.assertTrue(issues_for_markdown('<table><tr><td><img src="x"></td></tr></table>'))

    def test_latex_not_deleted(self):
        self.assertEqual(issues_for_markdown("$$Q = A \\cdot v$$\n\n$p \\leq 3$"), [])

    def test_numeric_body_not_layout_removed(self):
        self.assertNotIn("text", DROP_TYPES)
        self.assertNotIn("table", DROP_TYPES)
        self.assertNotIn("equation", DROP_TYPES)
        self.assertNotIn("table_footnote", DROP_TYPES)


if __name__ == "__main__":
    unittest.main()
