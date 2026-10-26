"""공식 외부 검색, 질의 캐시 및 확보된 문서 후보 선별."""
import os
import time
import requests
from drug_core import ROOT, read_json, write_json, hash_text, log_event, chat_json, ServiceError, settings


def external_search(query):
    path = ROOT / 'storage' / 'source_queries' / (hash_text(query.strip().casefold()) + '.json')
    if path.exists():
        cached = read_json(path)
        if cached.get('failed') and time.time() - cached['time'] < 300:
            raise ServiceError('공식 검색의 최근 연결 실패로 기존 자료를 사용합니다.')
        if not cached.get('failed') and time.time() - cached['time'] < 86400:
            return cached['items'], True
    key = os.getenv('MFDS_API_KEY')
    if not key:
        return [], False
    url = 'https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService08/getDrugPrdtPrmsnInq08'
    result = None
    for attempt in range(3):
        try:
            response = requests.get(url, params={'serviceKey': key, 'type': 'json',
                                    'pageNo': 1, 'numOfRows': 10, 'item_name': query}, timeout=(10, 30))
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 200:
                result = response.json()
            break
        except (requests.RequestException, ValueError):
            if attempt < 2:
                time.sleep(2 ** attempt)
    if not result or result.get('header', {}).get('resultCode') != '00':
        write_json(path, {'time': time.time(), 'failed': True, 'items': []})
        raise ServiceError('공식 자료 검색을 완료하지 못했습니다. 기존 자료로만 확인합니다.')
    items = result.get('body', {}).get('items', []) or []
    if not isinstance(items, list):
        items = []
    write_json(path, {'time': time.time(), 'items': items})
    return items, False


def select_documents(products, question, plan):
    """기존 공식 문서를 재사용하고 미지원 외부 자료는 자동 수집하지 않는다."""
    folder = ROOT / 'data' / 'prepared' / 'documents'
    candidates = []
    for product in products:
        seq = product['source_record_id']
        for doc_id in product['document_ids']:
            code = doc_id.rsplit(':', 1)[-1]
            doc = read_json(folder / f'{seq}_{code}.json')
            candidates.append({'candidate_id': doc_id, 'title': doc['title'],
                               'summary': doc.get('text', '')[:500],
                               'required': doc.get('source_type', 'official_label') == 'official_label',
                               'source_scope': doc.get('scope_type', 'product'),
                               'already_collected': True,
                               'source_name': doc['source_name'], 'source_url': doc.get('source_url')})
    search_status = []
    external_candidates = []
    suggested = plan.get('source_queries', [])
    suggested = [q.strip() for q in suggested if isinstance(q, str) and 0 < len(q.strip()) <= 100] if isinstance(suggested, list) else []
    queries = list(dict.fromkeys([p['product_name'] for p in products][:2] + suggested[:3]))
    # 질문 시에는 확보된 근거를 우선 사용. 최신 목록 조회는 명시적 설정으로 분리.
    if not settings().get('retrieval', {}).get('refresh_sources_on_query', False):
        queries = []
        search_status.append({'status': 'local_sources', 'note': '저장된 자료 기준; 실시간 갱신 아님'})
    for query in queries:
        try:
            rows, cached = external_search(query)
            search_status.append({'query_hash': hash_text(query), 'cache_hit': cached, 'results': len(rows)})
            for row in rows:
                if not any(str(row.get('ITEM_SEQ')) == p['source_record_id'] for p in products):
                    external_candidates.append({
                        'candidate_id': 'external:' + str(row.get('ITEM_SEQ')),
                        'title': row.get('ITEM_NAME', ''),
                        'summary': str(row.get('PRDUCT_TYPE') or '') + ' / ' + str(row.get('ITEM_INGR_NAME') or ''),
                        'required': False, 'already_collected': False,
                        'source_name': '식품의약품안전처 목록 API',
                    })
        except ServiceError:
            search_status.append({'query_hash': hash_text(query), 'status': 'search_unavailable'})
    # 선택 제품과 다른 품목의 자료를 그 제품의 근거로 색인하지 않는다.
    optional = [c for c in candidates + external_candidates if not c['required']]
    # 모든 후보가 강제 포함 대상이면 LLM 선별로 달라질 결과가 없다.
    selection = chat_json('select', {'question': question, 'plan': plan,
                                     'products': [{'drug_id': p['drug_id'], 'name': p['product_name']} for p in products],
                                     'candidates': candidates + external_candidates}) if optional else {'selected_ids': [], 'rejected_ids': [], 'deferred_ids': []}
    available = {c['candidate_id'] for c in candidates}
    mandatory = {c['candidate_id'] for c in candidates if c['required']}
    selected = set(selection.get('selected_ids', [])) & available
    selected |= mandatory
    log_event('source_selection', {
        'selected_ids': sorted(selected),
        'rejected_ids': [c['candidate_id'] for c in external_candidates],
        'model_rejected_ids': selection.get('rejected_ids', []),
        'deferred_ids': selection.get('deferred_ids', []),
        'reused_ids': sorted(mandatory), 'searches': search_status,
        'selection_mode': 'llm' if optional else 'mandatory_reuse',
    })
    return sorted(selected), search_status
