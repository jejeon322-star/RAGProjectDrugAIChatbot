"""외부 API 호출 없이 수행하는 핵심 회귀 점검."""
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv
BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
os.environ['DRUG_PROJECT_ROOT'] = str(BASE / 'data/hospital/project')

from build_drug_dataset import parse_material, text_windows
from drug_core import ROOT, client, ServiceError
from drug_pipeline import match_mentions, validate_answer, interpret, product_card


class DataTests(unittest.TestCase):
    def test_ingredient_amount_and_unknown(self):
        rows, warnings = parse_material('총량 : 1정|성분명 : A|분량 : 0.2|단위 : mg;총량 : 1정|성분명 : B|분량 : .|단위 : mg')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['amount'], '0.2')
        self.assertIsNone(rows[1]['amount'])
        self.assertEqual(len(warnings), 1)

    def test_chunk_coverage(self):
        text = ('조건과 본문\n' * 500)
        windows = list(text_windows(text, 1000, 200))
        previous_end = 0
        for start, end, part in windows:
            self.assertLessEqual(start, previous_end)
            self.assertEqual(part, text[start:end])
            previous_end = end
        self.assertEqual(previous_end, len(text))

    def test_ambiguous_products_not_collapsed(self):
        products = [{'drug_id': '1', 'product_name': '예시정1mg'}, {'drug_id':'2','product_name':'예시정2mg'}]
        self.assertEqual(len(match_mentions(['예시정'], products)[0]), 2)
        self.assertEqual(match_mentions(['예시정1mg'], products)[0][0]['drug_id'], '1')

    def test_citation_validation(self):
        evidence = [{'evidence_id':'E1'}]
        result = validate_answer({'status':'answered','answer_markdown':'원문 내용 [근거 E1]',
                                  'used_evidence_ids':['E999']}, evidence)
        self.assertEqual(result['used_evidence_ids'], ['E1'])
        with self.assertRaises(Exception):
            validate_answer({'answer_markdown':'설명 [근거 E999]'}, evidence)
        result = validate_answer({'status':'answered','answer_markdown':'인용 없는 설명'}, evidence)
        self.assertEqual(result['status'], 'insufficient_evidence')

    def test_missing_entity_does_not_use_previous(self):
        with patch('drug_pipeline.chat_json', return_value={'mentions':['없는제품XYZ'], 'use_active':False}):
            result = interpret('없는제품XYZ는?', ['mfds:200410896'])
        self.assertNotEqual(result['status'], 'ready')

    def test_multiple_active_requires_clarification(self):
        with patch('drug_pipeline.chat_json', return_value={'mentions':[], 'use_active':True}):
            result = interpret('그 약은?', ['mfds:195500005','mfds:195500006'])
        self.assertEqual(result['status'], 'clarify')

    def test_exploration_has_prescription_field(self):
        product = {'drug_id':'x', 'product_name':'검증 제품', 'prescription_type':'전문의약품'}
        self.assertEqual(product_card(product)['prescription_type'], '전문의약품')

    def test_missing_key_is_handled_without_request(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ServiceError):
                client()


class InterfaceTests(unittest.TestCase):
    def test_initial_page_and_context_history(self):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(str(BASE/'hospital_app.py'))
        # 사전 제품 선택 없이 질문 가능하고, 제품별 대화 상태가 분리된다.
        at.session_state.contexts = {
            'general': {'drug_ids': [], 'messages': []},
            'mfds:200410896': {'drug_ids':['mfds:200410896'],
                              'messages':[{'role':'user','content':'기존 질문 보존'}]},
        }
        at.session_state.active_context = 'general'
        at.session_state.context_picker = 'general'
        at.session_state.job = None
        at.session_state.last_failed_job = None
        at.run(timeout=30)
        self.assertFalse(at.exception)
        self.assertEqual(len(at.chat_input), 1)
        at.selectbox(key='context_picker').select('mfds:200410896').run(timeout=30)
        self.assertFalse(at.exception)
        self.assertTrue(any(m.value=='기존 질문 보존' for m in at.markdown))
        at.selectbox(key='context_picker').select('general').run(timeout=30)
        self.assertFalse(any(m.value=='기존 질문 보존' for m in at.markdown))
        self.assertEqual(len(at.session_state.contexts['mfds:200410896']['messages']), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
