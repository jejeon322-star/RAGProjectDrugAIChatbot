import unittest
from unittest.mock import patch

from drug_pipeline import interpret
from drug_overview import overview_evidence

PRODUCTS = [{'drug_id': str(i), 'product_name': f'테스트약정{i}mg',
             'ingredient_names': '테스트성분'} for i in (1, 2)]


class OverviewTests(unittest.TestCase):
    def route(self, question, result, active=None, overview=False):
        with patch('drug_pipeline.chat_json', return_value=result), patch('drug_pipeline.load_products', return_value=PRODUCTS):
            return interpret(question, active or [], active_overview=overview)

    def test_name_question_does_not_require_strength(self):
        r = self.route('테스트약이 뭐야?', {'mentions': ['테스트약'], 'question_type': 'overview'})
        self.assertEqual(r['status'], 'ready')
        self.assertTrue(r['overview'])
        self.assertEqual(set(r['drug_ids']), {'1', '2'})

    def test_dose_still_requires_product_even_if_model_misclassifies(self):
        r = self.route('테스트약 얼마나 먹어?', {'mentions': ['테스트약'], 'question_type': 'overview'})
        self.assertEqual(r['status'], 'clarify')

    def test_fuzzy_name_does_not_become_overview(self):
        r = self.route('테스약 뭐야?', {'mentions': ['테스약'], 'question_type': 'overview'})
        self.assertNotEqual(r['status'], 'ready')

    def test_family_followup_and_comparison_are_distinct(self):
        result = {'mentions': [], 'use_active': True, 'question_type': 'precautions'}
        self.assertTrue(self.route('주의사항은?', result, ['1', '2'], True)['overview'])
        self.assertEqual(self.route('얼마나 먹어?', result, ['1', '2'], True)['status'], 'clarify')
        self.assertEqual(self.route('주의사항은?', result, ['1', '2'], False)['status'], 'clarify')

    def test_table_source_is_held_and_dose_document_is_not_requested(self):
        p = {**PRODUCTS[0], 'source_record_id': '1', 'company_name': '검증업체',
             'source_name': '공식자료', 'document_ids': ['1:EE', '1:UD']}
        doc = {'sections': [{'paragraphs': [{'kind': 'table'}]}], 'text': '표 내용'}
        with patch('drug_overview.read_json', return_value=doc) as read:
            result = overview_evidence([p], '테스트약이 뭐야?')
        self.assertEqual(read.call_count, 1)
        self.assertEqual(len(result['evidence']), 1)
        self.assertFalse(result['overview_coverage']['common_claims_allowed'])
        self.assertIn('1:EE', result['overview_coverage']['skipped_documents'])


if __name__ == '__main__':
    unittest.main()
