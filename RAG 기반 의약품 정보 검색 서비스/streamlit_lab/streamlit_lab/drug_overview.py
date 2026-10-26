"""제품 확정 전 기본 정보: 직접 일치한 후보만, 제품별 출처를 유지한다."""
import re
from drug_core import ROOT, read_json


def general_question(question, interpretation):
    specific = r'용량|용법|몇\s*(?:알|정|번|회|미리|mg|ml)|얼마나|먹어|먹는\s*법|복용\s*(?:법|량|간격)|투여|투약|주사\s*(?:방법|어떻게)|희석|혼합|배합|AST|피부반응|임신|수유|소아|신장|신부전|간기능|병용|같이|함께|추천|바꿔|대신|요약'
    if re.search(specific, question, re.I):
        return False
    kind = interpretation.get('question_type')
    if interpretation.get('requires_product_selection') is True:
        return False
    return kind in ('overview', 'precautions') or bool(re.search(r'뭐|무슨|설명|효능|효과|주의사항|부작용|어떤\s*약|알려', question))


def direct_matches(mentions, candidates):
    def norm(value):
        return re.sub(r'[\s()（）·\-]', '', value).casefold()
    return all(any(norm(term) in norm(p.get(field) or '')
                   for term in mentions for field in ('product_name', 'ingredient_names', 'product_name_en'))
               for p in candidates)


def overview_evidence(products, question, metadata_only=False):
    evidence, skipped = [], []
    budget = 40000
    codes = ['EE', 'NB'] if re.search(r'주의|부작용|이상반응', question) else ['EE']
    for product in products:
        # 상세 용법과 수치는 기본 설명 경로에 전달하지 않는다.
        text = '\n'.join(f'{label}: {product.get(field) or "확인되지 않음"}'
                         for field, label in [('product_name', '제품명'), ('company_name', '제조사'),
                                              ('ingredient_names', '주성분')])
        evidence.append({'evidence_id': f'E{len(evidence)+1}', 'drug_id': product['drug_id'],
                         'title': product['product_name'] + ' 기본정보', 'source_type': 'metadata',
                         'source_name': product['source_name'], 'url': product.get('source_dataset_url'),
                         'text': text, 'locator': '제품 기본정보', 'collected_at': product.get('collected_at')})
        if metadata_only:
            continue
        for code in codes:
            doc_id = product['drug_id'] + ':' + code
            if doc_id not in product['document_ids']:
                skipped.append(doc_id)
                continue
            path = ROOT / 'data/prepared/documents' / f"{product['source_record_id']}_{code}.json"
            doc = read_json(path)
            # 표 또는 긴 문서를 일부만 떼어 공통 주의사항이라고 단정하지 않는다.
            table = any(p['kind'] == 'table' for s in doc['sections'] for p in s['paragraphs'])
            body = doc['text']
            if table or len(body) > 16000 or len(body) > budget:
                skipped.append(doc_id)
                continue
            budget -= len(body)
            evidence.append({'evidence_id': f'E{len(evidence)+1}', 'drug_id': product['drug_id'],
                             'title': doc['title'], 'source_type': 'official_label',
                             'source_name': doc['source_name'], 'url': doc.get('source_url'),
                             'text': body, 'locator': '문서 전체 · 원문 쪽수 표시 참고',
                             'collected_at': doc.get('collected_at'), 'source_updated_at': doc.get('source_updated_at')})
    return {'evidence': evidence, 'status': 'retrieved', 'overview_coverage': {
        'candidate_count': len(products), 'skipped_documents': skipped,
        'expected_documents': [p['drug_id'] + ':' + code for p in products for code in codes],
        'common_claims_allowed': False,
        'note': '서로 다른 제품 후보의 기본 설명이며 병원 채택 및 제형·용량은 확정되지 않았다.'}}
