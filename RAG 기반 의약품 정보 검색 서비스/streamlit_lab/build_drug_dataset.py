"""prepare_drugs.py 옆에 저장 후 python build_drug_dataset.py 실행.

API/LLM/임베딩 호출 없이 기본정보 보완과 청킹까지 수행합니다.
"""
import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

import prepare_drugs as base


DEFAULT_CONFIG = {
    'chunking': {'size_chars': 1000, 'overlap_chars': 200, 'version': 'article-v1'},
    'retrieval': {'method': 'similarity', 'candidate_k': 10,
                  'external_top_k': 5, 'include_selected_product': True},
    'collections': {'metadata': 'drug_meta', 'documents': 'drug_docs'},
}


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def parse_material(text):
    """제공된 구분자와 명시적 필드만 파싱. 불명확한 값은 원문 보존."""
    ingredients, warnings = [], []
    for record in re.split(r';(?=\s*총량\s*:)', text or ''):
        if not record.strip():
            continue
        fields = {}
        for token in record.split('|'):
            key, separator, value = token.partition(':')
            if separator:
                fields[key.strip()] = value.strip()
        if not fields.get('성분명'):
            warnings.append('성분명 파싱 불가: 원문 확인 필요')
            continue
        amount_raw = fields.get('분량', '')
        amount = None
        try:
            parsed = Decimal(amount_raw)
            if parsed.is_finite() and parsed >= 0:
                amount = str(parsed)
        except InvalidOperation:
            pass
        if amount is None:
            warnings.append('수치로 확인할 수 없는 분량: ' + fields['성분명'])
        ingredients.append({
            'name': fields['성분명'], 'amount': amount,
            'amount_raw': amount_raw, 'unit': fields.get('단위') or None,
            'basis': fields.get('총량') or None,
            'specification': fields.get('규격') or None,
            'ingredient_note': fields.get('성분정보') or None,
            'note': fields.get('비고') or None,
            'source_field': 'MATERIAL_NAME', 'raw_record': record,
        })
    return ingredients, warnings


def text_windows(text, size, overlap):
    """짧은 문단은 통째로 유지하고 긴 문단은 겹치는 문자 범위로 나눔."""
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            split = text.rfind('\n', start + size // 2, end)
            if split >= start + overlap:
                end = split + 1
        yield start, end, text[start:end]
        if end == len(text):
            break
        start = end - overlap


def generate_chunks(doc, product, cfg):
    """절 위치와 조건을 보존하고 표는 검토 대상으로 분리."""
    settings = cfg['chunking']
    size, overlap = settings['size_chars'], settings['overlap_chars']
    index = 0
    for section in doc['sections']:
        # 조건 확인을 위해 절 전체를 함께 보존한다.
        paragraphs = section['paragraphs']
        section_text = '\n\n'.join(p['text'] for p in paragraphs if p['text'])
        if not section_text.strip():
            continue
        has_table = any(p['kind'] == 'table' for p in paragraphs)
        # 표를 포함한 절은 구조 검증 전에 검색에 사용하지 않는다.
        windows = [(0, len(section_text), section_text)] if has_table else text_windows(section_text, size, overlap)
        for start, end, body in windows:
            index += 1
            prefix = '\n'.join([
                '자료 범위: 제품별 공식 자료' if doc.get('source_type', 'official_label') == 'official_label' else '자료 범위: 제품별 추가 자료 · ' + doc.get('source_name', ''),
                '대상 제품: ' + product['product_name'],
                '문서 제목: ' + doc['title'],
                '절 제목: ' + (section['heading'] or doc.get('xml_title', '')),
            ])
            version = doc['content_hash']
            chunk_id = f"doc:{doc['drug_id']}:{doc['doc_id']}:{version}:{settings['version']}:{size}:{overlap}:{index}"
            yield {
                'chunk_id': chunk_id,
                'text': prefix + '\n\n' + body,
                'drug_id': doc['drug_id'], 'document_id': doc['doc_id'],
                'document_version': version, 'chunking_version': settings['version'],
                'chunk_index': index, 'document_title': doc['title'],
                'section_type': doc['section_type'], 'section_title': section['heading'],
                'source_type': doc.get('source_type', 'official_label'), 'source_name': doc['source_name'],
                'source_url': doc.get('source_url'),
                'source_file': doc['source_file'], 'source_field': doc['source_field'],
                'article_index': section['article_index'],
                'char_start': start, 'char_end': end,
                'article_text': section_text,
                'collected_at': doc.get('collected_at'),
                'scope_type': 'product', 'is_current': True,
                'chunk_hash': digest(body),
                'contains_table': has_table,
                'retrieval_status': 'review_required' if has_table else 'candidate',
                'context_review_required': True,
            }


def run(root, output, config_path):
    if not config_path.exists():
        base.write(config_path, DEFAULT_CONFIG)
    cfg = json.loads(config_path.read_text(encoding='utf-8-sig'))
    size = cfg['chunking']['size_chars']
    overlap = cfg['chunking']['overlap_chars']
    if not isinstance(size, int) or not isinstance(overlap, int) or not 0 <= overlap < size:
        raise ValueError('청크 크기와 중복 길이는 0 <= overlap < size인 정수여야 합니다.')

    source = root / 'data' / 'raw' / 'detail'
    if output.resolve() == source.resolve():
        raise ValueError('원본과 출력 폴더는 달라야 합니다.')
    if base.run(source, output):
        raise ValueError('원본 변환 오류를 먼저 해결하세요. quality_report.json을 확인하세요.')
    products = json.loads((output / 'drugs.json').read_text(encoding='utf-8'))
    classification = {}
    list_files = sorted((root / 'data' / 'raw' / 'list').glob('*.json'))
    list_files += sorted((root / 'data' / 'raw' / 'list_full').glob('*.json'))
    for file in list_files:
        data = json.loads(file.read_text(encoding='utf-8-sig'))
        for row in data.get('body', {}).get('items', []):
            classification[str(row['ITEM_SEQ'])] = row.get('PRDUCT_TYPE')

    report = {'products': len(products), 'warnings': [], 'documents': 0,
              'chunks': 0, 'table_sections_held_for_review': 0,
              'embedding_called': False, 'vector_index_created': False,
              'notes': [
                  '성분·함량은 원문 구분자로 파싱했으며 단위 환산은 하지 않았습니다.',
                  '제형·방출 특성·투여 경로는 자동 확정하지 않았습니다.',
                  '긴 절의 조건이 청크 밖에 있을 수 있어 최종 근거는 article_text와 함께 검증해야 합니다.',
                  '표를 포함한 절은 구조 검증 전 자동 답변용 검색에서 제외해야 합니다.',
              ]}
    chunks = []
    for product in products:
        seq = product['source_record_id']
        ingredients, warnings = parse_material(product.get('material_name_raw'))
        product['ingredients'] = ingredients
        product['ingredient_names'] = ' / '.join(x['name'] for x in ingredients)
        product['strength_text'] = ' / '.join(
            f"{x['name']} {x['amount']} {x['unit']} (기준량: {x['basis']})"
            for x in ingredients if x['amount'] is not None and x['unit'] and x['basis']
        ) or None
        product['classification_raw'] = classification.get(seq)
        product['normalized_product_name'] = re.sub(r'\s+', '', product['product_name']).casefold()
        product['indications_text'] = None
        product['field_status'] = {
            'ingredients': 'parsed_from_source' if ingredients else 'not_parsed',
            'dosage_form': 'not_verified', 'routes': 'not_verified',
        }
        report['warnings'].extend({'drug_id': product['drug_id'], 'message': w} for w in warnings)
        # 현재 제품이 참조하는 문서만 읽는다.
        for doc_id in product['document_ids']:
            code = doc_id.rsplit(':', 1)[-1]
            doc = json.loads((output / 'documents' / f'{seq}_{code}.json').read_text(encoding='utf-8'))
            if code == 'EE':
                product['indications_text'] = doc['text']
            report['documents'] += 1
            generated = list(generate_chunks(doc, product, cfg))
            report['table_sections_held_for_review'] += sum(x['contains_table'] for x in generated)
            chunks.extend(generated)

        # 보관 정보를 원본 필드와 연결된 별도 근거로 저장한다.
        if product.get('storage_method'):
            storage_text = '저장방법: ' + product['storage_method']
            if product.get('valid_term'):
                storage_text += '\n사용기간: ' + product['valid_term']
            doc = {
                'drug_id': product['drug_id'], 'doc_id': product['drug_id'] + ':STORAGE',
                'title': product['product_name'] + ' — 저장방법·사용기간',
                'section_type': 'STORAGE', 'xml_title': '저장방법·사용기간',
                'content_hash': digest(storage_text),
                'source_name': product['source_name'], 'source_url': None,
                'source_file': product['source_file'],
                'source_field': 'STORAGE_METHOD,VALID_TERM',
                'collected_at': product.get('collected_at'),
                'sections': [{'article_index': 1, 'heading': '저장방법·사용기간',
                              'paragraphs': [{'kind': 'p', 'text': storage_text}]}],
                'text': storage_text,
            }
            base.write(output / 'documents' / f'{seq}_STORAGE.json', doc)
            product['document_ids'].append(doc['doc_id'])
            chunks.extend(generate_chunks(doc, product, cfg))
            report['documents'] += 1
        for label, value in [('공식 분류', product['classification_raw']),
                             ('효능·효과 원문', product['indications_text'])]:
            if value:
                product['search_text'] += f'\n{label}: {value}'
        product['content_hash'] = digest(product['search_text'])

    ids = [x['chunk_id'] for x in chunks]
    if len(ids) != len(set(ids)):
        raise ValueError('중복 청크 ID가 있습니다.')
    if any(not c['text'].strip() for c in chunks):
        raise ValueError('빈 청크가 있습니다.')
    report['chunks'] = len(chunks)
    report['products_with_classification'] = sum(bool(x['classification_raw']) for x in products)
    report['products_with_parsed_ingredients'] = sum(bool(x['ingredients']) for x in products)
    report['chunks_pending_context_validation'] = len(chunks)
    base.write(output / 'drugs.json', products)
    base.write(output / 'chunks_preview.json', chunks)
    base.write(output / 'dataset_report.json', report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('warnings', 'notes')}, ensure_ascii=False, indent=2))
    print('성분 확인 경고:', len(report['warnings']))
    print('완료. 임베딩과 벡터 DB 생성은 아직 실행하지 않았습니다.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    try:
        run(args.project, args.output or args.project / 'data' / 'prepared',
            args.config or args.project / 'rag_settings.json')
    except (OSError, ValueError, KeyError) as exc:
        print('처리 실패:', exc)
        raise SystemExit(1)
