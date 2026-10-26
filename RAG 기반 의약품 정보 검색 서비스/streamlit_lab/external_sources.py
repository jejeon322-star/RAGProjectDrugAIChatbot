"""사용 권한과 제품 적용 범위가 확인된 외부 자료를 출처와 함께 등록한다."""
import hashlib
import io
import json
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    from drug_core import write_json
    write_json(path, value)


def extract_content(content, filename):
    if len(content) > 5 * 1024 * 1024:
        raise ValueError('파일은 5MB 이하로 준비하세요.')
    if filename.lower().endswith('.pdf'):
        import pdfplumber
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                if len(pdf.pages) > 30:
                    raise ValueError('제품과 관련된 독립적인 절을 30쪽 이하로 준비하세요.')
                pages = []
                for i, page in enumerate(pdf.pages, 1):
                    if page.find_tables():
                        raise ValueError('표가 감지되었습니다. 표의 행·열과 적용 조건을 검토한 텍스트를 등록하세요.')
                    text = (page.extract_text() or '').replace('\x00', ' ').strip()
                    if not text:
                        raise ValueError('추출되지 않는 페이지가 있습니다. OCR 결과를 원문과 대조한 후 텍스트로 등록하세요.')
                    pages.append(f'[파일 {i}쪽]\n{text}')
                return '\n\n'.join(pages), f'PDF 1~{len(pdf.pages)}쪽'
        except ValueError:
            raise
        except Exception:
            raise ValueError('PDF를 읽지 못했습니다. 검토한 텍스트 파일로 등록하세요.') from None
    if filename.lower().endswith('.txt'):
        try:
            return content.decode('utf-8-sig'), '등록한 텍스트 전체'
        except UnicodeDecodeError:
            raise ValueError('텍스트 파일은 UTF-8로 저장하세요.') from None
    raise ValueError('PDF 또는 UTF-8 TXT 파일을 사용하세요.')


def validate_url(url, provider):
    parts = urlsplit(url.strip())
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('정확한 원문 주소를 입력하세요. 인증정보가 담긴 주소는 사용하지 마세요.')
    if provider == '약학정보원':
        host = parts.hostname.lower()
        if host != 'health.kr' and not host.endswith('.health.kr'):
            raise ValueError('약학정보원 자료는 health.kr의 원문 주소를 입력하세요.')
        if parts.path in ('', '/'):
            raise ValueError('홈페이지 주소 대신 해당 제품·문서의 상세 주소를 입력하세요.')
    return url.strip()


def make_document(product, provider, title, url, text, locator, rights_note, edition='', updated_at=None):
    url = validate_url(url, provider) if url.strip() or provider == '약학정보원' else None
    if not url and not locator.strip():
        raise ValueError('원문 주소가 없는 내부 문서는 파일·장·쪽수 등 원문 위치를 입력하세요.')
    if not title.strip() or len(rights_note.strip()) < 5:
        raise ValueError('문서 제목과 구체적인 이용 권한 근거를 입력하세요.')
    if provider == '전문서적' and not edition.strip():
        raise ValueError('서적의 판본·출판사·발행연도를 입력하세요.')
    if provider in ('병원 지침', '기타 공식기관') and not edition.strip():
        raise ValueError('자료를 발행한 기관명과 버전을 입력하세요.')
    text = text.replace('\x00', ' ').strip()
    if not 40 <= len(text) <= 16000:
        raise ValueError('본문은 40~16,000자로, 적용 조건이 온전히 포함된 독립적인 절을 준비하세요.')
    if '\ufffd' in text or '(cid:' in text:
        raise ValueError('본문에 깨진 문자가 있습니다. 원문과 대조해 수정하세요.')
    identity = '\n'.join([product['drug_id'], provider, url or locator, title.strip(), edition])
    code = 'EXT' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    return {'doc_id': product['drug_id'] + ':' + code, 'drug_id': product['drug_id'],
            'title': title.strip(), 'source_name': provider, 'source_type': 'external_document',
            'source_url': url, 'scope_type': 'product', 'section_type': code,
            'source_field': 'user_supplied_authorized_content', 'xml_title': title.strip(),
            'source_file': '', 'collected_at': datetime.now(timezone.utc).isoformat(),
            'source_updated_at': updated_at or None, 'edition': edition.strip(),
            'rights_note': rights_note.strip(), 'scope_confirmation': 'user_confirmed_exact_product',
            'text': text, 'content_hash': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'sections': [{'article_index': 1, 'heading': locator.strip() or '등록 본문 전체',
                          'paragraphs': [{'kind': 'p', 'text': text}]}]}


def apply_imports(target, cfg):
    """원본을 유지하고 데이터 재준비 후에도 추가 문서를 복원한다. API 호출 없음."""
    from build_drug_dataset import generate_chunks
    prepared = target / 'data/prepared'
    products = read(prepared / 'drugs.json')
    by_id = {p['drug_id']: p for p in products}
    chunks = read(prepared / 'chunks_preview.json')
    chunks = [c for c in chunks if c.get('source_type') != 'external_document']
    for product in products:
        product['document_ids'] = [i for i in product['document_ids'] if not i.rsplit(':', 1)[-1].startswith('EXT')]
    loaded = []
    for file in sorted((target / 'data/raw/additional_documents').glob('*.json')):
        doc = read(file)
        if doc.get('review_status') != 'selected' or doc['drug_id'] not in by_id:
            continue
        product = by_id[doc['drug_id']]
        code = doc['doc_id'].rsplit(':', 1)[-1]
        save(prepared / 'documents' / f"{product['source_record_id']}_{code}.json", doc)
        product['document_ids'].append(doc['doc_id'])
        chunks.extend(generate_chunks(doc, product, cfg))
        loaded.append({'doc_id': doc['doc_id'], 'title': doc['title'], 'source_name': doc['source_name'],
                       'source_url': doc['source_url'], 'drug_id': doc['drug_id']})
    save(prepared / 'drugs.json', products)
    save(prepared / 'chunks_preview.json', chunks)
    save(target / 'additional_sources_manifest.json', {'count': len(loaded), 'documents': loaded})
    report_path = prepared / 'dataset_report.json'
    if report_path.exists():
        report = read(report_path)
        report.update(documents=sum(len(p['document_ids']) for p in products), chunks=len(chunks),
                      additional_documents=len(loaded))
        save(report_path, report)
    return loaded


def register_source(product, provider, title, url, rights_note, rights_confirmed, scope_confirmed,
                    text='', file_content=None, filename='', locator='', edition='', updated_at=None):
    from drug_core import ROOT, settings, chat_json
    if not rights_confirmed or not scope_confirmed:
        raise ValueError('사용 권한과 선택 제품에 대한 자료 적용 범위를 확인하세요.')
    if file_content is not None:
        if text.strip():
            raise ValueError('본문 붙여넣기와 파일 중 하나만 사용하세요.')
        text, extracted_locator = extract_content(file_content, filename)
        locator = locator or extracted_locator
    doc = make_document(product, provider, title, url, text, locator, rights_note, edition, updated_at)
    selection = chat_json('select', {'question': product['product_name'] + '의 추가 근거 등록',
        'products': [{'drug_id': product['drug_id'], 'name': product['product_name']}],
        'candidates': [{'candidate_id': doc['doc_id'], 'title': doc['title'],
                        'summary': doc['text'][:4000], 'source_name': provider,
                        'source_url': url, 'source_scope': 'product',
                        'rights_status': 'user_confirmed', 'required': False, 'already_collected': True}]})
    doc['review_status'] = 'selected' if doc['doc_id'] in selection.get('selected_ids', []) else 'deferred'
    code = doc['doc_id'].rsplit(':', 1)[-1]
    raw = ROOT / 'data/raw/additional_documents' / f"{product['source_record_id']}_{code}.json"
    if file_content is not None:
        suffix = '.pdf' if filename.lower().endswith('.pdf') else '.txt'
        source_file = raw.with_suffix(suffix)
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_bytes(file_content)
        doc['source_file'] = str(source_file)
    else:
        doc['source_file'] = str(raw)
    save(raw, doc)
    if doc['review_status'] != 'selected':
        raise ValueError('자료를 보류 상태로 저장했습니다. 선택 제품과의 관련성·원문의 적용 범위를 확인한 뒤 재등록하세요.')
    apply_imports(ROOT, settings())
    return doc
