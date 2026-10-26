import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from external_sources import make_document, apply_imports, register_source
from drug_sources import select_documents

PRODUCT = {'drug_id': 'mfds:1', 'source_record_id': '1', 'product_name': '검증용제품정',
           'document_ids': ['mfds:1:EE']}
CONTENT = '이 문서는 프로그램 검증을 위한 가상 문서입니다. 실제 의약품 설명이나 투약 지침이 아닙니다. ' * 3
CFG = {'chunking': {'size_chars': 1000, 'overlap_chars': 200, 'version': 'article-v1'}}


class AdditionalSourceTests(unittest.TestCase):
    def make(self):
        return make_document(PRODUCT, '약학정보원', '검증 문서', 'https://www.health.kr/test/document',
                             CONTENT, '원문 1쪽', '테스트 실행 전용 허락 기록')

    def test_pharmacy_url_requires_real_domain_and_detail(self):
        for url in ['https://health.kr.evil.example/test', 'https://www.health.kr/', 'javascript:test']:
            with self.assertRaises(ValueError):
                make_document(PRODUCT, '약학정보원', '제목', url, CONTENT, '', '사용 허락 기록')

    def test_identity_stable_for_updated_body(self):
        a = self.make()
        b = make_document(PRODUCT, '약학정보원', '검증 문서', a['source_url'], CONTENT + '수정', '원문 1쪽', '사용 허락 기록')
        self.assertEqual(a['doc_id'], b['doc_id'])
        self.assertNotEqual(a['content_hash'], b['content_hash'])
        self.assertEqual(a['source_type'], 'external_document')

    def test_permission_and_scope_required_before_model_call(self):
        with patch('drug_core.chat_json') as chat:
            with self.assertRaises(ValueError):
                register_source(PRODUCT, '약학정보원', '문서', 'https://www.health.kr/test',
                                '허락 기록', False, True, text=CONTENT)
            chat.assert_not_called()

    def test_rebuild_preserves_source_and_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            prepared = target / 'data/prepared'
            raw = target / 'data/raw/additional_documents'
            prepared.mkdir(parents=True)
            raw.mkdir(parents=True)
            (prepared / 'drugs.json').write_text(json.dumps([PRODUCT]), encoding='utf-8')
            (prepared / 'chunks_preview.json').write_text('[]', encoding='utf-8')
            doc = self.make()
            doc.update(review_status='selected', source_file=str(raw / 'source.txt'))
            (raw / 'source.json').write_text(json.dumps(doc), encoding='utf-8')
            apply_imports(target, CFG)
            apply_imports(target, CFG)
            chunks = json.loads((prepared / 'chunks_preview.json').read_text(encoding='utf-8'))
            self.assertEqual(len(chunks), 1)
            self.assertEqual(chunks[0]['source_type'], 'external_document')
            self.assertEqual(chunks[0]['source_url'], doc['source_url'])
            products = json.loads((prepared / 'drugs.json').read_text(encoding='utf-8'))
            self.assertEqual(len(products[0]['document_ids']), 2)

    def test_books_require_edition(self):
        with self.assertRaises(ValueError):
            make_document(PRODUCT, '전문서적', '책', '', CONTENT, '1장 12쪽', '기관 제공 자료')

    def test_search_plan_is_used_and_own_document_is_mandatory(self):
        doc = {'title': '자체 허가 문서', 'text': '본문', 'source_name': '식약처', 'source_url': None}
        with patch('drug_sources.read_json', return_value=doc), \
             patch('drug_sources.settings', return_value={'retrieval': {'refresh_sources_on_query': True}}), \
             patch('drug_sources.external_search', return_value=([], True)) as search, \
             patch('drug_sources.chat_json', return_value={'selected_ids': [], 'rejected_ids': ['mfds:1:EE']}) as selector, \
             patch('drug_sources.log_event'):
            selected, _ = select_documents([PRODUCT], '질문', {'source_queries': ['계획 검색어']})
        self.assertIn('mfds:1:EE', selected)
        self.assertIn('계획 검색어', [call.args[0] for call in search.call_args_list])
        selector.assert_not_called()

    def test_authorized_registration_goes_through_candidate_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            prepared = target / 'data/prepared'
            prepared.mkdir(parents=True)
            (prepared / 'drugs.json').write_text(json.dumps([PRODUCT]), encoding='utf-8')
            (prepared / 'chunks_preview.json').write_text('[]', encoding='utf-8')
            doc = self.make()
            with patch('drug_core.ROOT', target), patch('drug_core.settings', return_value=CFG), \
                 patch('drug_core.chat_json', return_value={'selected_ids': [doc['doc_id']]}) as selector:
                registered = register_source(PRODUCT, '약학정보원', '검증 문서', 'https://www.health.kr/test/document',
                    '테스트 실행 전용 허락 기록', True, True, text=CONTENT, locator='원문 1쪽')
            self.assertEqual(registered['review_status'], 'selected')
            self.assertEqual(selector.call_args.args[0], 'select')
            manifest = json.loads((target / 'additional_sources_manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['count'], 1)

    def test_additional_source_is_indexed_queryable_and_idempotent(self):
        import chromadb
        from chromadb.config import Settings
        import index_drugs
        import uuid
        db = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
        suffix = uuid.uuid4().hex[:10]
        meta = db.create_collection('test_meta_' + suffix, embedding_function=None)
        docs = db.create_collection('test_docs_' + suffix, embedding_function=None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                prepared = target / 'data/prepared'
                raw = target / 'data/raw/additional_documents'
                prepared.mkdir(parents=True)
                raw.mkdir(parents=True)
                product = {**PRODUCT, 'search_text': '프로그램 검증용 가상 항목'}
                (prepared / 'drugs.json').write_text(json.dumps([product]), encoding='utf-8')
                (prepared / 'chunks_preview.json').write_text('[]', encoding='utf-8')
                doc = self.make()
                doc.update(review_status='selected', source_file=str(raw / 'source.txt'))
                (raw / 'source.json').write_text(json.dumps(doc), encoding='utf-8')
                apply_imports(target, CFG)
                with patch.object(index_drugs, 'ROOT', target), \
                     patch.object(index_drugs, 'load_products', side_effect=lambda: json.loads((prepared / 'drugs.json').read_text(encoding='utf-8'))), \
                     patch.object(index_drugs, 'collections', return_value=(meta, docs)), \
                     patch.object(index_drugs, 'settings', return_value={'models': {'embedding': 'test-embedding'}}), \
                     patch.object(index_drugs, 'vector_directory', return_value=target / 'vector'), \
                     patch.object(index_drugs, 'embed', side_effect=lambda texts: [[1.0, 0.0, 0.0] for _ in texts]):
                    index_drugs.index()
                    index_drugs.index()
                self.assertEqual(meta.count(), 1)
                self.assertEqual(docs.count(), 1)
                found = docs.query(query_embeddings=[[1.0, 0.0, 0.0]], where={'drug_id': 'mfds:1'}, n_results=1)
                self.assertEqual(found['metadatas'][0][0]['source_type'], 'external_document')
                self.assertEqual(found['metadatas'][0][0]['document_id'], doc['doc_id'])
        finally:
            db.delete_collection(meta.name)
            db.delete_collection(docs.name)


class AdditionalSourceInterfaceTests(unittest.TestCase):
    def app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_file(str(Path(__file__).resolve().parent / 'hospital_app.py'))

    def test_upload_form_requires_permission_before_api_call(self):
        app = self.app().run(timeout=30)
        with patch('drug_core.chat_json') as chat:
            button = next(b for b in app.button if b.label == '자료 검토 후 검색에 반영')
            button.click().run(timeout=30)
        self.assertFalse(app.exception)
        self.assertTrue(any('사용 권한' in w.value for w in app.warning))
        chat.assert_not_called()

    def test_additional_source_has_distinct_source_label(self):
        app = self.app()
        app.session_state.contexts = {'general': {'drug_ids': [], 'messages': [{
            'role': 'assistant', 'content': '화면 검증용 문구', 'is_answer': True,
            'used_evidence_ids': ['E1'], 'evidence': [{
                'evidence_id': 'E1', 'title': '검증 자료', 'source_type': 'external_document',
                'source_name': '약학정보원', 'url': 'https://www.health.kr/test',
                'text': '검증 문구', 'locator': '1쪽'}]}]}}
        app.session_state.active_context = 'general'
        app.session_state.context_picker = 'general'
        app.session_state.job = None
        app.session_state.last_failed_job = None
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertTrue(any('추가 근거 자료 · 약학정보원' in c.value for c in app.caption))


if __name__ == '__main__':
    unittest.main()
