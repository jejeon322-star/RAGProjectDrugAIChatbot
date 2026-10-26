"""명칭 확인, 두 컬렉션 검색 및 retrieve/generate LangGraph."""
import re
from difflib import SequenceMatcher
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

from drug_core import (ROOT, load_products, read_json, collections, embed,
                       settings, chat_json, log_event, ServiceError)
from drug_sources import select_documents
from drug_overview import general_question, direct_matches, overview_evidence


def normalize(value):
    return re.sub(r'[\s()（）·\-]', '', value).casefold()


def product_card(product):
    return {k: product.get(k) for k in ('drug_id', 'product_name', 'company_name',
            'strength_text', 'dosage_form', 'routes', 'classification_raw',
            'material_name_raw', 'appearance_raw', 'indications_text', 'permit_status',
            'prescription_type', 'ingredient_names', 'main_ingredient_raw')}


def match_mentions(mentions, products):
    groups = []
    for mention in mentions:
        term = normalize(mention)
        if not term:
            continue
        exact = [p for p in products if term in (normalize(p['product_name']), normalize(p.get('product_name_en') or ''))]
        matches = exact or [p for p in products if term in normalize(p['product_name'])]
        if not matches:
            matches = [p for p in products if term in normalize(p.get('ingredient_names') or '')]
        if not matches:
            ranked = sorted(products, key=lambda p: SequenceMatcher(None, term, normalize(p['product_name'])).ratio(), reverse=True)
            matches = [p for p in ranked[:4] if SequenceMatcher(None, term, normalize(p['product_name'])).ratio() >= .45]
        groups.append(matches)
    return groups


def interpret(question, active_ids, previous_questions=None, active_overview=False):
    # 명시적인 기본 설명만 규칙으로 처리. 조건·용량·비교·후속 질문은 기존 해석 유지.
    basic = re.fullmatch(r'\s*([가-힣A-Za-z0-9·]+?)(?:은|는|이|가)?\s*(?:뭐야|뭔가요|어떤\s*약이야|어떤\s*약인가요|설명해줘|설명해\s*줘)[?？.!\s]*', question)
    term = basic.group(1) if basic else ''
    literal = [p for p in load_products() if term and normalize(term) in normalize(p['product_name'])]
    if basic and len(term) >= 2 and literal and general_question(question, {}):
        result = {'intent': 'question', 'question_type': 'overview', 'mentions': [term],
                  'requires_product_selection': False, 'comparison': False, 'ingredient_scope': False}
        log_event('interpret_local', {'candidate_count': len(literal)})
    else:
        result = chat_json('interpret', {'question': question, 'active_drug_ids': active_ids,
                                        'previous_user_questions': (previous_questions or [])[-3:]})
    products = load_products()
    by_id = {p['drug_id']: p for p in products}
    if result.get('intent') == 'exploration':
        return {'status': 'exploration', 'interpretation': result}
    mentions = result.get('mentions', [])
    if not isinstance(mentions, list):
        mentions = []
    mentions = [x for x in mentions if isinstance(x, str)][:3]
    groups = match_mentions(mentions, products)
    candidates = list({p['drug_id']: p for group in groups for p in group}.values())
    if (groups and all(groups) and candidates and len(candidates) <= 50
            and len(mentions) == 1 and not result.get('comparison')
            and direct_matches(mentions, candidates) and general_question(question, result)):
        if len(candidates) > 1 or result.get('ingredient_scope'):
            return {'status': 'ready', 'drug_ids': [p['drug_id'] for p in candidates],
                    'overview': True, 'overview_name': mentions[0], 'interpretation': result}
    if result.get('ingredient_scope') and mentions:
        # 제품 허가사항을 성분 일반 자료로 바꿔 부르지 않는다.
        candidates = list({p['drug_id']: p for group in groups for p in group}.values())
        return {'status': 'clarify', 'candidates': candidates[:10],
                'message': '현재는 제품별 허가자료가 준비되어 있습니다. 제품별 정보로 확인할 대상을 선택해 주세요. 성분 일반 자료는 아직 확보하지 않았습니다.',
                'interpretation': result}
    if groups and any(not group for group in groups):
        return {'status': 'not_found', 'message': '현재 확보한 제품 자료에서 질문 대상을 확인하지 못했습니다. 제품명을 다시 확인하거나 목록에서 선택해 주세요.', 'candidates': []}
    if groups and all(len(group) == 1 for group in groups):
        ids = list(dict.fromkeys(group[0]['drug_id'] for group in groups))
        return {'status': 'ready', 'drug_ids': ids, 'interpretation': result}
    if groups:
        candidates = list({p['drug_id']: p for group in groups for p in group}.values())
        message = '이 질문은 함량·제형에 따라 답이 달라질 수 있어요. 정확한 제품을 선택해 주세요.' if direct_matches(mentions, candidates) else '제품 이름이 불확실합니다. 아래 후보에서 정확한 제품을 확인해 주세요.'
        return {'status': 'clarify', 'message': message,
                'candidates': candidates[:10], 'interpretation': result}
    if result.get('use_active') and len(active_ids) == 1 and active_ids[0] in by_id:
        return {'status': 'ready', 'drug_ids': active_ids, 'interpretation': result}
    if result.get('use_active') and len(active_ids) > 1:
        if active_overview and general_question(question, result):
            return {'status': 'ready', 'drug_ids': [i for i in active_ids if i in by_id],
                    'overview': True, 'interpretation': result}
        message = '용량·투여방법 등 제품별 정보는 정확한 함량·제형 확인이 필요해요. 아래에서 제품을 선택해 주세요.' if active_overview else '현재 여러 제품을 비교 중입니다. 어느 제품에 대한 질문인가요?'
        return {'status': 'clarify', 'message': message,
                'candidates': [by_id[i] for i in active_ids if i in by_id], 'interpretation': result}
    return {'status': 'clarify', 'message': '제품명이나 성분명을 알려 주세요. 확보된 제품 목록에서 선택할 수도 있습니다.',
            'candidates': [], 'interpretation': result}


def explore(question, interpretation):
    meta, _ = collections()
    count = meta.count()
    if not count:
        raise ServiceError('제품 색인이 없습니다.')
    matches = meta.query(query_embeddings=embed([question]), n_results=min(15, count))
    ids = {m['drug_id'] for m in matches['metadatas'][0]}
    candidates = [p for p in load_products() if p['drug_id'] in ids]
    selected = chat_json('explore', {'question': question, 'conditions': interpretation.get('conditions', []),
                                     'products': [product_card(p) for p in candidates]})
    valid = {p['drug_id']: p for p in candidates}
    results = []
    for row in selected.get('selected', [])[:5]:
        product = valid.get(row.get('drug_id'))
        if not product:
            continue
        searchable = product['search_text'] + '\n' + '\n'.join(str(v or '') for v in product_card(product).values())
        quotes = row.get('supporting_quotes', [])
        if not quotes or not all(isinstance(q, str) and q in searchable for q in quotes):
            continue
        results.append({'product': product, 'reason': row.get('reason', ''), 'quotes': quotes})
    return results


class RagState(TypedDict, total=False):
    question: str
    drug_ids: list[str]
    summary: bool
    evidence: list[dict]
    answer: dict
    status: str
    overview: bool
    overview_coverage: dict


def retrieve(state):
    all_products = load_products()
    products = [p for p in all_products if p['drug_id'] in state['drug_ids']]
    if len(products) != len(set(state['drug_ids'])) or not products:
        raise ServiceError('제품 식별정보를 확인할 수 없습니다.')
    if state.get('overview') and re.fullmatch(r'\s*[가-힣A-Za-z0-9·]+?(?:은|는|이|가)?\s*(?:뭐야|뭔가요|어떤\s*약이야|어떤\s*약인가요|설명해줘|설명해\s*줘)[?？.!\s]*', state['question']):
        plan = {'retrieval_questions': [state['question'] + ' 성분 효능 효과'],
                'source_queries': [], 'drug_ids': state['drug_ids']}
        log_event('search_plan_local', {'query_count': 1})
    else:
        plan = chat_json('search', {'question': state['question'], 'products': [product_card(p) for p in products]})
    # LLM의 ID 출력으로 대상이 바뀌지 않도록 확정된 제품 ID만 사용.
    chosen_docs, search_status = select_documents(products, state['question'], plan)
    meta, docs = collections()
    evidence = []
    overview_base = overview_evidence(products, state['question'], metadata_only=True) if state.get('overview') else None
    for product in ([] if overview_base else products):
        evidence.append({'evidence_id': 'E' + str(len(evidence)+1),
                         'drug_id': product['drug_id'], 'title': product['product_name'] + ' 기본정보',
                         'source_type': 'metadata', 'source_name': product['source_name'],
                         'url': product.get('source_dataset_url'),
                         'text': product['search_text'], 'locator': '제품 기본정보',
                         'collected_at': product.get('collected_at')})
    if overview_base:
        evidence = overview_base['evidence']
    mapping = {c['chunk_id']: c for c in read_json(ROOT / 'data' / 'prepared' / 'chunks_preview.json')}
    queries = plan.get('retrieval_questions', [])
    queries = [q for q in queries if isinstance(q, str) and q.strip()][:3] or [state['question']]
    # 특화 요약은 주제별 질의를 수행해 용법·금기·관찰 근거가 편중되지 않게 한다.
    if state.get('summary'):
        queries = ['효능 효과 사용 목적', '용법 용량 투여 방법', '금기 신중 투여', '상호작용', '이상반응 관찰']
    query_vectors = embed(queries)
    found = []
    for product_index, product in enumerate(products):
        where = {'drug_id': product['drug_id']}
        if state.get('overview'):
            allowed_codes = {'EE', 'NB'} if re.search(r'주의|부작용|이상반응', state['question']) else {'EE'}
            permitted = [doc_id for doc_id in product['document_ids'] if doc_id in chosen_docs and
                         (doc_id.rsplit(':', 1)[-1] in allowed_codes or doc_id.rsplit(':', 1)[-1].startswith('EXT'))]
            if not permitted:
                continue
            where = {'$and': [where, {'document_id': {'$in': permitted}}]}
        available = docs.get(where=where, include=[])
        n = min(settings()['retrieval']['candidate_k'], len(available['ids']))
        if not n:
            continue
        result = docs.query(query_embeddings=query_vectors, where=where, n_results=n)
        for rank in range(n):
            for qindex in range(len(queries)):
                found.append((rank, product_index, qindex, result['ids'][qindex][rank], result['distances'][qindex][rank]))
    seen = set()
    max_sources = settings()['retrieval']['external_top_k']
    used = 0
    for _, _, _, chunk_id, distance in sorted(found):
        chunk = mapping.get(chunk_id)
        if not chunk or chunk['document_id'] not in chosen_docs:
            continue
        if state.get('overview'):
            code = chunk['document_id'].rsplit(':', 1)[-1]
            allowed = {'EE', 'NB'} if re.search(r'주의|부작용|이상반응', state['question']) else {'EE'}
            if code not in allowed and chunk.get('source_type') != 'external_document':
                continue
        key = (chunk['drug_id'], chunk['document_id'], chunk['article_index'])
        if key in seen:
            continue
        seen.add(key)
        # 정량 조건이 다른 청크로 분리되지 않도록 해당 ARTICLE 전체를 함께 제공.
        text = chunk['article_text']
        if len(text) > 16000:
            log_event('source_held', {'chunk_id': chunk_id, 'reason': 'article_too_large'})
            continue
        seq = chunk['drug_id'].split(':')[-1]
        code = chunk['document_id'].rsplit(':', 1)[-1]
        doc = read_json(ROOT / 'data' / 'prepared' / 'documents' / f'{seq}_{code}.json')
        evidence.append({'evidence_id': 'E' + str(len(evidence)+1), 'drug_id': chunk['drug_id'],
                         'title': chunk['document_title'], 'source_type': chunk.get('source_type', 'official_label'),
                         'source_name': chunk['source_name'], 'url': chunk.get('source_url'),
                         'text': text, 'locator': chunk['section_title'] or doc.get('xml_title', code),
                         'collected_at': chunk.get('collected_at'), 'chunk_id': chunk_id,
                         'source_field': chunk['source_field'], 'article_index': chunk['article_index'],
                         'source_updated_at': doc.get('source_updated_at')})
        evidence[-1]['source_file'] = doc.get('source_file')
        evidence[-1]['edition'] = doc.get('edition')
        used += 1
        if used >= max_sources:
            break
    log_event('retrieve', {'drug_ids': state['drug_ids'], 'evidence_ids': [e.get('chunk_id',e['evidence_id']) for e in evidence],
                           'search_queries': plan.get('queries', []), 'search_status': search_status})
    output = {'evidence': evidence, 'status': 'retrieved'}
    if overview_base:
        used_docs = {mapping[e['chunk_id']]['document_id'] for e in evidence if e.get('chunk_id') in mapping}
        expected = set(overview_base['overview_coverage']['expected_documents'])
        output['overview_coverage'] = {**overview_base['overview_coverage'],
                                       'skipped_documents': sorted(expected - used_docs)}
    return output


def validate_answer(answer, evidence):
    allowed = {e['evidence_id'] for e in evidence}
    markdown = answer.get('answer_markdown', '')
    if not isinstance(markdown, str):
        raise ServiceError('답변 형식을 확인할 수 없습니다.')
    cited = set(re.findall(r'\[근거\s+(E\d+)\]', markdown))
    if not cited.issubset(allowed):
        raise ServiceError('답변에 확인되지 않은 근거 번호가 있어 표시를 보류했습니다.')
    answer['used_evidence_ids'] = sorted(cited, key=lambda x: int(x[1:]))
    if not cited and answer.get('status') == 'answered':
        return {'status': 'insufficient_evidence',
                'answer_markdown': '제공된 근거 문서에서 인용을 확인하지 못해 답변을 보류했습니다.',
                'used_evidence_ids': [], 'unconfirmed_items': ['인용 누락']}
    return answer


def generate(state):
    result = chat_json('answer', {'question': state['question'], 'summary': state.get('summary', False),
                                 'overview': state.get('overview', False),
                                 'overview_coverage': state.get('overview_coverage'),
                                 'evidence': state['evidence']})
    return {'answer': validate_answer(result, state['evidence']), 'status': 'complete'}


def graph():
    workflow = StateGraph(RagState)
    workflow.add_node('retrieve', retrieve)
    workflow.add_node('generate', generate)
    workflow.add_edge(START, 'retrieve')
    workflow.add_edge('retrieve', 'generate')
    workflow.add_edge('generate', END)
    return workflow.compile()


def answer_question(question, drug_ids, summary=False):
    return graph().invoke({'question': question, 'drug_ids': drug_ids, 'summary': summary})
