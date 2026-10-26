"""사용 권한이 확인된 추가 근거 등록 화면."""
import streamlit as st
from drug_core import ROOT, ServiceError, read_json


def render_source_import(products):
    with st.expander('근거 자료 추가 · 약학정보원 / 병원 지침'):
        st.caption('사용 권한이 있는 자료를 정확한 제품에 연결합니다. 원문이 없으면 주소만 저장해 근거처럼 사용하지 않습니다.')
        st.link_button('약학정보원에서 원문 확인', 'https://www.health.kr/')
        st.caption('약학정보원은 자료의 무단 복제·제공을 제한합니다. 허락받은 자료나 제공받은 데이터를 등록하세요.')
        st.caption('환자 개인정보가 포함된 자료는 등록하지 마세요. 자동 선별은 임상 검토나 이용 허락을 대신하지 않습니다.')
        labels = {p['drug_id']: p['product_name'] + ' / ' + str(p.get('company_name') or '') for p in products}
        by_id = {p['drug_id']: p for p in products}
        with st.form('additional_source_form', clear_on_submit=False):
            drug_id = st.selectbox('자료를 연결할 정확한 제품', list(labels), format_func=labels.get)
            provider = st.selectbox('자료 출처', ['약학정보원', '병원 지침', '전문서적', '기타 공식기관'])
            title = st.text_input('문서 제목')
            url = st.text_input('원문 상세 주소', help='약학정보원은 health.kr 상세 주소가 필수입니다. 내부 문서는 파일·쪽수를 입력할 수 있습니다.')
            edition = st.text_input('판본·기관·발행연도', help='전문서적은 출판사·판본·발행연도를 입력하세요.')
            locator = st.text_input('장·절·쪽수', help='예: 3장 42~44쪽. PDF는 파일 기준 쪽수도 자동 기록합니다.')
            updated = st.text_input('원문에 명시된 개정일 (없으면 비워두기)')
            file = st.file_uploader('PDF 또는 UTF-8 TXT', type=['pdf', 'txt'])
            text = st.text_area('또는 검토한 본문 붙여넣기', help='파일과 본문 중 하나만 사용하세요. 적용 조건이 온전히 포함된 독립적인 절을 준비하세요.')
            rights = st.text_input('이용 권한 근거', help='예: 기관에서 제공한 사용 허락 문서 번호, 데이터 이용 계약 범위. 출처 표기만으로 이용 허락이 생기지는 않습니다.')
            rights_ok = st.checkbox('이 자료를 챗봇 검색과 AI 처리에 사용할 권한을 확인했습니다.')
            scope_ok = st.checkbox('자료의 제품·함량·제형과 위에서 선택한 제품의 적용 범위를 확인했습니다.')
            submit = st.form_submit_button('자료 검토 후 검색에 반영')
        if submit:
            try:
                from external_sources import register_source
                from index_drugs import index
                with st.status('자료와 출처를 검토하고 있습니다.', expanded=True) as status:
                    doc = register_source(by_id[drug_id], provider, title, url, rights, rights_ok, scope_ok,
                        text=text, file_content=file.getvalue() if file else None,
                        filename=file.name if file else '', locator=locator, edition=edition, updated_at=updated)
                    status.update(label='추가 자료의 검색 색인을 만들고 있습니다.')
                    index()
                    status.update(label='등록 완료: ' + doc['title'], state='complete')
                st.success('자료를 검색에 반영했습니다. 해당 제품을 선택하고 질문하세요.')
            except (ValueError, ServiceError) as exc:
                st.warning(str(exc))
            except Exception:
                st.error('등록을 완료하지 못했습니다. 저장된 자료는 유지됩니다. 자료 상태를 확인하고 다시 시도하세요.')
        manifest = ROOT / 'storage/index_manifest.json'
        if manifest.exists() and not read_json(manifest).get('ready'):
            if st.button('저장한 자료의 색인 다시 반영', key='retry_source_index'):
                try:
                    from index_drugs import index
                    with st.spinner('검색 색인을 복구하고 있습니다.'):
                        index()
                    st.success('검색 색인이 준비되었습니다.')
                except Exception:
                    st.error('색인을 완료하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도하세요.')
