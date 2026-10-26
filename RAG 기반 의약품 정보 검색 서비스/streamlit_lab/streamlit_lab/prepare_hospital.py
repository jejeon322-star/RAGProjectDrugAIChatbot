"""병원 후보 원본 변환·출처 연결. API와 LLM은 호출하지 않는다."""
import json
from pathlib import Path
import shutil
import re

from build_drug_dataset import run, generate_chunks, digest

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / 'data/hospital/project'


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def prepare():
    cfg = json.loads((ROOT / 'rag_settings.json').read_text(encoding='utf-8-sig'))
    cfg['collections'] = {'metadata': 'hospital_meta', 'documents': 'hospital_docs'}
    cfg['embedding_batch_size'] = 64
    save(TARGET / 'rag_settings.json', cfg)
    # 실행 시 프로젝트 루트 prompts/를 직접 읽으므로 별도 프롬프트 사본을 만들지 않는다.
    run(TARGET, TARGET / 'data/prepared', TARGET / 'rag_settings.json')
    mapping = json.loads((ROOT / 'data/hospital/matches.json').read_text(encoding='utf-8'))
    save(TARGET / 'hospital_matches.json', mapping)
    products_file = TARGET / 'data/prepared/drugs.json'
    products = json.loads(products_file.read_text(encoding='utf-8'))
    # 정상 허가 상태만 앱에 포함한다. 병원 채택 확정과는 다른 상태다.
    excluded = [p for p in products if p.get('permit_status') not in ('정상', None, '')]
    products = [p for p in products if p not in excluded]
    for product in products:
        seq = product['source_record_id']
        product['hospital_adoption_status'] = 'unconfirmed_candidate'
        product['hospital_note_entries'] = [
            {'candidate_id': e['candidate_id'], 'name_as_written': e['name_as_written'], 'page': e['page']}
            for e in mapping['entries'] if any(c['item_seq'] == seq for c in e['official_candidates'])]
        for doc_id in product['document_ids']:
            code = doc_id.rsplit(':', 1)[-1]
            doc_path = TARGET / 'data/prepared/documents' / f'{seq}_{code}.json'
            doc = json.loads(doc_path.read_text(encoding='utf-8'))
            if '/PBP/CMN/PDFDOWNLOAD/' in (doc.get('source_url') or '').upper():
                doc['source_url'] = f'https://nedrug.mfds.go.kr/pbp/cmn/pdfDownload/{seq}/{code}'
                save(doc_path, doc)
    chunks_file = TARGET / 'data/prepared/chunks_preview.json'
    chunks = json.loads(chunks_file.read_text(encoding='utf-8'))
    for chunk in chunks:
        if '/PBP/CMN/PDFDOWNLOAD/' in (chunk.get('source_url') or '').upper():
            seq = chunk['drug_id'].split(':')[-1]
            code = chunk['document_id'].rsplit(':', 1)[-1]
            chunk['source_url'] = f'https://nedrug.mfds.go.kr/pbp/cmn/pdfDownload/{seq}/{code}'
    by_seq = {p['source_record_id']: p for p in products}
    labels = {'EE': '효능·효과', 'UD': '용법·용량', 'NB': '사용상의 주의사항'}
    supplemental = 0
    for file in (TARGET / 'data/raw/official_pdf').glob('*.json'):
        pdf = json.loads(file.read_text(encoding='utf-8'))
        product = by_seq.get(pdf['item_seq'])
        if not product:
            continue
        code = pdf['code']
        doc_id = product['drug_id'] + ':' + code
        if doc_id in product['document_ids']:
            continue
        text = '\n\n'.join(f"[원문 {p['page']}쪽]\n" + '\n'.join(line.replace('\x00', ' ').strip() for line in p['text'].splitlines() if line.strip()) for p in pdf['pages'])
        if not text.strip() or not any(p['text'].strip() for p in pdf['pages']):
            continue
        has_table = any(p['contains_table'] for p in pdf['pages'])
        sections = [{'article_index': 1, 'heading': f"공식 PDF 전체 · 1~{len(pdf['pages'])}쪽",
                     'paragraphs': [{'kind': 'table' if has_table else 'p', 'text': text}]}]
        if code == 'NB' and not has_table:
            # 식약처의 번호 있는 주의사항 절을 유지한다. 임의 길이로 절을 자르지 않는다.
            parts = re.split(r'(?m)(?=^\d{1,2}\.\s*(?:경고|다음|이상|부작용|일반|상호|임부|임산부|수유|소아|고령|과량|적용|기타|저장|보관|운전|신중|투여|금기|주의))', text)
            page = 1
            sections = []
            for part in parts:
                if not part.strip():
                    continue
                heading = next((line for line in part.splitlines() if not line.startswith('[원문 ')), labels[code])
                sections.append({'article_index': len(sections) + 1,
                    'heading': f'PDF {page}쪽부터 · ' + heading[:120],
                    'paragraphs': [{'kind': 'p', 'text': part.strip()}]})
                markers = re.findall(r'\[원문 (\d+)쪽\]', part)
                if markers:
                    page = int(markers[-1])
        doc = {'doc_id': doc_id, 'drug_id': product['drug_id'], 'scope_type': 'product',
               'title': product['product_name'] + ' — ' + labels[code] + ' (공식 PDF)',
               'section_type': code, 'xml_title': labels[code],
               'source_name': product['source_name'], 'source_url': pdf['url'],
               'source_file': pdf['source_file'], 'source_field': code + '_DOC_ID',
               'source_updated_at': None, 'collected_at': pdf['collected_at'],
               'text': text, 'content_hash': digest(text),
               'extraction_method': 'pdfplumber_layout_text', 'pdf_sha256': pdf['sha256'],
               'sections': sections}
        save(TARGET / 'data/prepared/documents' / f"{pdf['item_seq']}_{code}.json", doc)
        product['document_ids'].append(doc_id)
        chunks.extend(generate_chunks(doc, product, cfg))
        supplemental += 1
        if code == 'EE' and not has_table:
            product['indications_text'] = text
            product['search_text'] += '\n효능·효과 원문: ' + text
            product['content_hash'] = digest(product['search_text'])
    save(chunks_file, chunks)
    save(products_file, products)
    report_path = TARGET / 'data/prepared/dataset_report.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    report.update(products=len(products), documents=sum(len(p['document_ids']) for p in products),
                  chunks=len(chunks), official_pdf_documents=supplemental,
                  table_sections_held_for_review=sum(c['contains_table'] for c in chunks))
    save(report_path, report)
    quality_path = TARGET / 'data/prepared/quality_report.json'
    quality = json.loads(quality_path.read_text(encoding='utf-8'))
    available_docs = {doc_id for p in products for doc_id in p['document_ids']}
    quality['missing_fields'] = [m for m in quality['missing_fields'] if m['drug_id'] + ':' + m['field'].split('_')[0] not in available_docs]
    quality['document_count'] = report['documents']
    quality['official_pdf_documents'] = supplemental
    quality['notes'].append('API 누락 본문 중 공식 PDF로 보완한 문서는 미확보 목록에서 제거했습니다.')
    save(quality_path, quality)
    save(TARGET / 'scope_report.json', {
        'note_entries': len(mapping['entries']), 'official_candidate_products': len(products),
        'unmatched_entries': [e['name_as_written'] for e in mapping['entries'] if not e['official_candidates']],
        'excluded_inactive_products': [p['product_name'] for p in excluded],
        'collection_errors': mapping['errors'], 'hospital_confirmed_products': 0,
        'evidence_sources': ['식품의약품안전처 제품 허가정보'],
        'not_loaded': ['약학정보원', '성인간호학 서적', '병원 투약 지침'],
        'note_text_used_as_clinical_evidence': False,
        'official_pdf_documents': supplemental,
        'held_table_chunks': sum(c['contains_table'] for c in chunks),
        'clinical_documents_missing': [
            {'product_name': p['product_name'], 'missing': [code for code in labels if p['drug_id'] + ':' + code not in p['document_ids']]}
            for p in products if any(p['drug_id'] + ':' + code not in p['document_ids'] for code in labels)],
    })
    print('병원 자료 후보 준비:', len(products), '제품. 병원 채택은 미확정.', flush=True)
    from external_sources import apply_imports
    loaded = apply_imports(TARGET, cfg)
    print('추가 근거 복원:', len(loaded), '문서', flush=True)


if __name__ == '__main__':
    prepare()
