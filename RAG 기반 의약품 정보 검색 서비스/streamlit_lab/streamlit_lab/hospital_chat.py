"""실행: python -m streamlit run hospital_app.py"""
import time
import uuid

import streamlit as st

from drug_core import ROOT, ServiceError, load_products, read_json, collections
from drug_pipeline import interpret, explore, graph

NOTICE = '본 서비스는 의약품 정보 조회와 학습을 지원하며, 환자별 투약 판단이나 의사·약사의 전문적 판단을 대신하지 않습니다. 실제 투약 전에는 최신 허가사항, 처방 및 소속 기관의 지침을 확인하세요.'

st.set_page_config(page_title='의약품 근거 검색', page_icon='💊', layout='wide')
st.markdown('''<style>
.stApp {background:#f6f8fb} .block-container {max-width:1050px;padding-top:2rem}
[data-testid="stChatMessage"] {background:white;border:1px solid #e5eaf0;border-radius:14px}
h1 {letter-spacing:-1.2px} [data-testid="stSidebar"] {background:#edf2f7}
</style>''', unsafe_allow_html=True)

try:
    products = load_products()
except ServiceError as exc:
    st.info(str(exc))
    st.stop()
by_id = {p['drug_id']: p for p in products}

st.title('💊 의약품 정보 검색 챗봇 서비스')
with st.expander(f'검색 가능한 약 목록 · {len(products)}개', expanded=False):
    st.caption('공식 제품 후보 목록입니다. 정확한 함량·제형·제조사를 확인해 선택하세요.')
    with st.container(height=220):
        st.caption('\n\n'.join(p['product_name'] + ' · ' + str(p.get('company_name') or '')
                             for p in sorted(products, key=lambda p: p['product_name'])))

st.caption('제공한 약 목록에 연결된 식약처 공식 자료를 검색합니다.')
st.info('병원에서 쓰는 약 일부의 이름을 바탕으로 찾은 제품 후보이며, 병원 전체 채택약 목록은 아닙니다. 기본 정보는 약 이름만으로 질문하세요. 용량·투여방법 등 제품별 확인이 필요한 질문에서 정확한 제품을 확인합니다.')
scope = read_json(ROOT / 'scope_report.json')
with st.expander('자료 범위와 미확보 자료'):
    st.write(f"PDF 약 이름 항목 {scope['note_entries']}건 → 공식 제품 후보 {scope['official_candidate_products']}개")
    st.caption('이 수는 병원 채택이 확정된 제품 수가 아닙니다.')
    additional = read_json(ROOT / 'additional_sources_manifest.json') if (ROOT / 'additional_sources_manifest.json').exists() else {'count': 0, 'documents': []}
    st.write('현재 답변 근거: 식약처 제품 허가정보')
    st.write(f"추가 등록 자료: {additional['count']}건")
    if additional['documents']:
        st.caption(' · '.join(sorted({d['source_name'] for d in additional['documents']})))
    else:
        st.caption('약학정보원·간호학 서적·병원 지침은 아직 등록되지 않았습니다. 왼쪽 자료 추가에서 등록할 수 있습니다.')
    st.caption('PDF의 공부용 설명과 예시 약 목록은 임상 답변 근거에 포함하지 않았습니다.')
    if scope['unmatched_entries']:
        st.warning('이름 재확인 필요: ' + ', '.join(scope['unmatched_entries']))
    if scope['collection_errors']:
        st.warning(f"공식 자료 수집 오류 {len(scope['collection_errors'])}건 — 해당 자료는 미확보 상태입니다.")



if 'contexts' not in st.session_state:
    st.session_state.contexts = {'general': {'drug_ids': [], 'messages': []}}
    st.session_state.active_context = 'general'
    st.session_state.context_picker = 'general'
    st.session_state.job = None
    st.session_state.last_failed_job = None

if 'next_context' in st.session_state:
    selected_context = st.session_state.pop('next_context')
    st.session_state.active_context = selected_context
    st.session_state.context_picker = selected_context


def context_for(ids, overview=False):
    key = ('overview:' if overview else '') + '|'.join(sorted(set(ids))) if ids else 'general'
    st.session_state.contexts.setdefault(key, {'drug_ids': ids, 'messages': [], 'overview': overview})
    return key


def select_product(drug_id, question=None):
    key = context_for([drug_id])
    st.session_state.active_context = key
    st.session_state.context_picker = key
    if question:
        st.session_state.job = {'id': uuid.uuid4().hex, 'question': question,
                                'forced_ids': [drug_id], 'append': True}


def select_context():
    st.session_state.active_context = st.session_state.context_picker


def new_chat():
    key = 'general:' + uuid.uuid4().hex[:8]
    st.session_state.contexts[key] = {'drug_ids': [], 'messages': []}
    st.session_state.active_context = key
    st.session_state.context_picker = key
    st.session_state.job = None


def label(key):
    if st.session_state.contexts[key].get('overview'):
        return st.session_state.contexts[key].get('overview_name', '관련 약') + ' · 기본정보'
    ids = st.session_state.contexts[key]['drug_ids']
    return ' / '.join(by_id[i]['product_name'] for i in ids if i in by_id) or ('대상 미확정 대화' if key == 'general' else '새 대화 ' + key[-4:])


def show_sources(message):
    evidence = {e['evidence_id']: e for e in message.get('evidence', [])}
    used = [evidence[i] for i in message.get('used_evidence_ids', []) if i in evidence]
    if not used:
        if message.get('is_answer'):
            st.caption('이 답변에 인용된 근거 문서가 없습니다.')
        return
    with st.expander(f'사용한 근거 {len(used)}건'):
        for source in used:
            kind = {'metadata': '제품 기본정보', 'official_label': '식약처 허가자료', 'external_document': '추가 근거 자료'}.get(source['source_type'], '근거 자료')
            st.markdown(f"**[근거 {source['evidence_id']}] {source['title']}**")
            st.caption(f"{kind} · {source['source_name']} · 위치: {source.get('locator', '')}")
            if source.get('url'):
                st.link_button('데이터 출처' if source['source_type'] == 'metadata' else '공식 원문', source['url'])
            elif source.get('source_file'):
                from pathlib import Path
                st.caption('원문 파일: ' + Path(source['source_file']).name)
            if source.get('edition'):
                st.caption('판본: ' + source['edition'])
            st.caption('수집일: ' + str(source.get('collected_at') or '기록 없음') +
                       ' · 개정일: ' + str(source.get('source_updated_at') or '확인되지 않음'))
            st.text(source['text'])


def candidate_cards(message, prefix):
    for index, row in enumerate(message.get('candidates', [])):
        product = by_id.get(row['drug_id'])
        if not product:
            continue
        with st.container(border=True):
            st.markdown('**' + product['product_name'] + '**')
            st.caption(product.get('company_name') or '업체명 미확인')
            st.write(product.get('strength_text') or product.get('material_name_raw') or '성분·함량 미확인')
            st.caption('제형·투여 경로: 구조화 확인 전 · 허가 상태: ' + str(product.get('permit_status')))
            if row.get('reason'):
                st.write(row['reason'])
            st.button('이 제품으로 질문하기', key=f'{prefix}_candidate_{index}',
                      on_click=select_product, args=(product['drug_id'], message.get('pending_question')))


with st.sidebar:
    st.header('대화')
    collection_progress = ROOT / 'data' / 'collection_progress.json'
    if collection_progress.exists():
        with st.expander('전체 의약품 수집 진행', expanded=True):
            try:
                collection_state = read_json(collection_progress)
                total = collection_state.get('total_reported', 0)
                catalog_count = collection_state.get('catalog_products', 0)
                saved_count = collection_state.get('details_saved', 0)
                st.write(f'전체 목록: {catalog_count:,} / {total:,}개')
                st.write(f'상세 응답: {saved_count:,}개')
                st.caption(collection_state.get('message', ''))
                if total:
                    st.progress(min(saved_count / total, 1.0))
                st.caption(f'현재 챗봇에 반영된 제품: {len(products):,}개. 원본 수집 후 검색 DB 반영이 별도로 필요합니다.')
                st.caption('갱신 시각: ' + collection_state.get('updated_at', ''))
                st.button('수집 상태 새로고침', key='refresh_collection')
            except (OSError, ValueError):
                st.caption('수집 상태를 읽는 중입니다. 잠시 후 새로고침하세요.')
    st.subheader('PDF에서 약 찾기')
    hospital_matches = read_json(ROOT / 'hospital_matches.json')['entries']
    entry_labels = {e['candidate_id']: f"{e['name_as_written']} · PDF {e['page']}쪽" for e in hospital_matches}
    chosen_entry = st.selectbox('자료에 적힌 약 이름', list(entry_labels), format_func=entry_labels.get)
    entry = next(e for e in hospital_matches if e['candidate_id'] == chosen_entry)
    options = [by_id['mfds:' + c['item_seq']] for c in entry['official_candidates'] if 'mfds:' + c['item_seq'] in by_id]
    if options:
        product_labels = {p['drug_id']: p['product_name'] + ' / ' + str(p.get('company_name') or '') for p in options}
        chosen_id = st.selectbox('공식 제품 후보 — 함량·제형 확인', list(product_labels), format_func=product_labels.get)
        chosen = by_id[chosen_id]
        st.caption('성분·함량 원문: ' + str(chosen.get('material_name_raw') or '정보 없음'))
        st.button('이 제품으로 질문하기', key='hospital_choose', on_click=select_product, args=(chosen_id,))
        st.caption('선택은 이번 질문 대상을 지정하며, 병원 채택 여부를 확정하지 않습니다.')
    else:
        st.warning('연결된 공식 제품이 없습니다. 약 봉투·제품 사진의 정확한 이름과 제조사 확인이 필요합니다.')
    st.button('＋ 새 대화', on_click=new_chat, width='stretch')
    context_labels = {key: label(key) for key in st.session_state.contexts}
    st.selectbox('대화 대상별 기록', list(context_labels), key='context_picker',
                 format_func=context_labels.get, on_change=select_context)
    with st.expander('제품 목록에서 직접 찾기'):
        keyword = st.text_input('제품명·성분명 검색', key='catalog_search')
        matches = [p for p in products if not keyword or keyword.casefold() in
                   (p['product_name']+' '+str(p.get('ingredient_names', ''))).casefold()]
        st.caption(f'확보된 데이터 내 {len(matches)}개 결과 · 처음 10개 표시')
        for p in matches[:10]:
            st.button(p['product_name'], key='catalog_'+p['drug_id'],
                      on_click=select_product, args=(p['drug_id'],))
    with st.expander('자료 준비 상태'):
        st.write(f'제품 원본: {len(products)}개')
        try:
            meta, docs = collections()
            st.write(f'기본정보 색인: {meta.count()}개')
            st.write(f'상세 근거 청크: {docs.count()}개')
        except ServiceError as exc:
            st.info(str(exc))
        st.caption('AST·기관별 지침·배합적합성 자료는 별도 확보 여부 확인이 필요합니다.')
        st.caption('표를 포함한 절은 구조 검증 전 자동 검색에서 제외됩니다.')

    from source_ui import render_source_import
    render_source_import(products)

active_key = st.session_state.active_context
context = st.session_state.contexts[active_key]
st.markdown('**현재 대상:** ' + label(active_key))
if context['drug_ids']:
    if st.button('투약 전·후 전체 요약'):
        st.session_state.job = {'id': uuid.uuid4().hex, 'question': '현재 제품의 투약 전·후 확인사항을 전체 요약해 주세요.',
                                'forced_ids': None if context.get('overview') else context['drug_ids'], 'summary': True, 'append': True}
elif not context['messages']:
    st.info('제품을 먼저 선택하지 않아도 질문할 수 있어요. 이름이 불확실하면 후보를 함께 확인합니다.')
    st.caption('예: 가스터가 뭐야? · 덱사메타손은 어떤 약이야? · 제품별 상세 정보가 필요하면 후보를 펼쳐 선택할 수 있어요.')

for index, message in enumerate(context['messages']):
    with st.chat_message(message['role']):
        st.markdown(message['content'])
        show_sources(message)
        if message.get('overview'):
            with st.expander('제품별로 자세히 보기', expanded=False):
                candidate_cards(message, f'{active_key}_{index}')
        else:
            candidate_cards(message, f'{active_key}_{index}')

st.caption(NOTICE)
st.caption('저장된 근거 자료를 검색합니다. 실시간 최신 허가사항 조회는 기본적으로 꺼져 있습니다.')
st.caption('환자 이름·등록번호 등 개인을 식별할 수 있는 정보는 입력하지 마세요.')
question = st.chat_input('제품명과 궁금한 내용을 입력하세요')
if question:
    st.session_state.job = {'id': uuid.uuid4().hex, 'question': question, 'append': True}

failed = st.session_state.last_failed_job
if failed and failed.get('context_key') == active_key:
    if st.button('실패한 질문 다시 시도'):
        st.session_state.job = dict(failed)
        st.session_state.last_failed_job = None

job = st.session_state.job
if job:
    st.session_state.job = None
    question = job['question']
    start_time = time.monotonic()
    try:
        with st.status('질문의 대상과 주제를 확인하고 있습니다.', expanded=False) as progress:
            forced = job.get('forced_ids')
            result = {'status': 'ready', 'drug_ids': forced, 'overview': job.get('overview', False)} if forced else interpret(
                question, context['drug_ids'], [m['content'] for m in context['messages'] if m['role']=='user'],
                active_overview=context.get('overview', False))
            if result['status'] == 'ready':
                ids = result['drug_ids']
                overview = result.get('overview', False)
                key = context_for(ids, overview)
                target_context = st.session_state.contexts[key]
                if overview:
                    target_context['overview_name'] = result.get('overview_name') or context.get('overview_name', '관련 약')
                if job.get('append'):
                    target_context['messages'].append({'role': 'user', 'content': question})
                    job['append'] = False
                job['forced_ids'] = ids
                job['overview'] = overview
                progress.update(label='관련 근거를 검색하고 확보된 문서를 확인하고 있습니다.')
                state = {'question': question, 'drug_ids': ids, 'overview': overview,
                         'summary': job.get('summary', result.get('interpretation', {}).get('intent')=='summary')}
                for event in graph().stream(state):
                    if 'retrieve' in event:
                        state.update(event['retrieve'])
                        progress.update(label='검색된 근거로 답변을 작성하고 있습니다.')
                    if 'generate' in event:
                        state.update(event['generate'])
                answer = state['answer']
                target_context['messages'].append({
                    'role': 'assistant', 'content': answer['answer_markdown'],
                    'evidence': state['evidence'], 'used_evidence_ids': answer['used_evidence_ids'],
                    'is_answer': True, 'elapsed_seconds': round(time.monotonic()-start_time, 2),
                    'overview': overview,
                    'candidates': [{'drug_id': i} for i in ids] if overview else [],
                })
                # 다음 실행 전에 콜백으로 선택 위젯 상태도 함께 전환.
                st.session_state.active_context = key
                st.session_state.next_context = key
            else:
                if job.get('append'):
                    context['messages'].append({'role': 'user', 'content': question})
                    job['append'] = False
                if result['status'] == 'exploration':
                    progress.update(label='조건에 맞는 제품 후보를 확인하고 있습니다.')
                    rows = explore(question, result['interpretation'])
                    message = {'role': 'assistant', 'content': '확보된 자료에서 조건을 확인한 제품입니다. 환자별 약물 추천을 뜻하지 않습니다.' if rows else '확보된 자료에서 조건 일치를 확인할 수 있는 제품을 찾지 못했습니다.',
                               'candidates': [{'drug_id': row['product']['drug_id'], 'reason': row['reason']} for row in rows]}
                else:
                    message = {'role': 'assistant', 'content': result['message'],
                               'pending_question': question,
                               'candidates': [{'drug_id': p['drug_id']} for p in result.get('candidates', [])]}
                context['messages'].append(message)
            progress.update(label='완료', state='complete')
        st.session_state.last_failed_job = None
        st.rerun()
    except ServiceError as exc:
        st.error(str(exc))
        st.session_state.last_failed_job = {**job, 'context_key': st.session_state.active_context}
    except Exception as exc:
        import traceback
        from pathlib import Path
        from drug_core import log_event
        log_event('ui_error', {'error_type': type(exc).__name__,
                  'frames': [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                             for f in traceback.extract_tb(exc.__traceback__)]})
        st.error('요청 처리 중 오류가 발생했습니다. 새로고침한 뒤 다시 시도하세요.')
        st.session_state.last_failed_job = {**job, 'context_key': st.session_state.active_context}
