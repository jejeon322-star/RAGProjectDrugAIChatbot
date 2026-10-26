"""API 원본을 제품 목록과 근거 문서로 변환. API/LLM 호출 없음.

streamlit_lab에 넣고 python prepare_drugs.py 로 실행하세요.
"""
import argparse
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET


class ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('p', 'div', 'br', 'tr', 'li'):
            self.parts.append('\n')
        if tag in ('td', 'th'):
            self.parts.append(' | ')

    def handle_endtag(self, tag):
        if tag in ('p', 'div', 'tr', 'li'):
            self.parts.append('\n')

    def handle_data(self, text):
        self.parts.append(text)


def readable(text):
    parser = ReadableHTML()
    parser.feed(text)
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def extract_sections(xml_text):
    root = ET.fromstring(xml_text)
    sections = []
    for number, article in enumerate(root.iter('ARTICLE'), 1):
        paragraphs = []
        for index, node in enumerate(article.iter('PARAGRAPH'), 1):
            original = ''.join(node.itertext())
            paragraphs.append({
                'paragraph_index': index,
                'kind': node.get('tagName', 'p'),
                'text': readable(original),
                'raw_content': original,
            })
        sections.append({
            'article_index': number,
            'heading': article.get('title', ''),
            'paragraphs': paragraphs,
        })
    if not sections:
        raise ValueError('ARTICLE 구조가 없습니다. 원본 확인 필요')
    return root.get('title', ''), sections


def run(input_dir, output_dir):
    files = sorted(input_dir.glob('*.json'))
    if not files:
        raise ValueError('입력 폴더에 JSON 파일이 없습니다.')
    products = {}
    report = {'input_files': len(files), 'document_count': 0, 'table_count': 0,
              'errors': [], 'missing_fields': [], 'notes': [
                  '표 검색용 텍스트는 간략 표현입니다. 병합 셀 의미 확인에는 raw_content 원문을 사용하세요.',
                  'XML의 제형·함량·적용 조건을 삭제하지 않았습니다. 제품별 적용 범위는 검색·답변 단계에서 확인해야 합니다.',
                  'CHANGE_DATE는 API 레코드 변경일로 보존했으며 개별 문서 개정일로 간주하지 않습니다.',
              ]}
    log_path = input_dir.parent / 'collection_log.json'
    try:
        collection_log = json.loads(log_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        collection_log = {}

    fields = [('EE', '효능·효과'), ('UD', '용법·용량'),
              ('NB', '사용상의 주의사항'), ('PN', '전문가용 정보')]
    for file in files:
        try:
            response = json.loads(file.read_text(encoding='utf-8-sig'))
            if response.get('header', {}).get('resultCode') != '00':
                raise ValueError('정상 API 응답이 아닙니다.')
            rows = response.get('body', {}).get('items', [])
            if not isinstance(rows, list) or not rows:
                raise ValueError('제품 배열이 비어 있거나 지원하지 않는 구조입니다.')
            for raw in rows:
                seq = str(raw['ITEM_SEQ'])
                if not re.fullmatch(r'[0-9]+', seq):
                    raise ValueError('품목번호 형식 오류')
                if seq in products:
                    raise ValueError('중복 품목번호: ' + seq)
                drug_id = 'mfds:' + seq
                collected_at = collection_log.get(seq, {}).get('collected_at')
                product = {
                    'drug_id': drug_id, 'source_record_id': seq,
                    'product_name': raw['ITEM_NAME'],
                    'product_name_en': raw.get('ITEM_ENG_NAME'),
                    'company_name': raw.get('ENTP_NAME'),
                    'contract_manufacturer_raw': raw.get('CNSGN_MANUF'),
                    'prescription_type': raw.get('ETC_OTC_CODE'),
                    'material_name_raw': raw.get('MATERIAL_NAME'),
                    'main_ingredient_raw': raw.get('MAIN_ITEM_INGR'),
                    'main_ingredient_en': raw.get('MAIN_INGR_ENG'),
                    'total_content_raw': raw.get('TOTAL_CONTENT'),
                    'appearance_raw': raw.get('CHART'),
                    'ingredients': [], 'strength_text': None, 'dosage_form': None,
                    'routes': [], 'aliases': [],
                    'storage_method': raw.get('STORAGE_METHOD'),
                    'valid_term': raw.get('VALID_TERM'),
                    'atc_code': raw.get('ATC_CODE'),
                    'permit_status': raw.get('CANCEL_NAME'),
                    'source_record_change_date_raw': raw.get('CHANGE_DATE'),
                    'collected_at': collected_at,
                    'source_name': '식품의약품안전처_의약품 제품 허가정보',
                    'source_dataset_url': 'https://www.data.go.kr/data/15095677/openapi.do',
                    'source_file': str(file.resolve()), 'document_ids': [],
                }
                for code, label in fields:
                    xml_text = raw.get(code + '_DOC_DATA')
                    if not xml_text:
                        report['missing_fields'].append({'drug_id': drug_id, 'field': code + '_DOC_DATA'})
                        continue
                    try:
                        title, sections = extract_sections(xml_text)
                    except (ET.ParseError, ValueError) as exc:
                        report['errors'].append({'file': file.name, 'field': code + '_DOC_DATA', 'error': str(exc)})
                        continue
                    doc_id = drug_id + ':' + code
                    text_parts = []
                    for section in sections:
                        text_parts.append(section['heading'])
                        for paragraph in section['paragraphs']:
                            text_parts.append(paragraph['text'])
                            if paragraph['kind'] == 'table':
                                report['table_count'] += 1
                    text = '\n\n'.join(x for x in text_parts if x)
                    doc = {
                        'doc_id': doc_id, 'drug_id': drug_id, 'scope_type': 'product',
                        'title': raw['ITEM_NAME'] + ' — ' + label,
                        'section_type': code, 'xml_title': title,
                        'source_name': product['source_name'],
                        'source_url': raw.get(code + '_DOC_ID') or None,
                        'source_file': product['source_file'],
                        'source_field': code + '_DOC_DATA',
                        'source_updated_at': None,
                        'source_record_change_date_raw': raw.get('CHANGE_DATE'),
                        'collected_at': collected_at, 'sections': sections,
                        'text': text, 'raw_xml': xml_text,
                        'content_hash': hashlib.sha256(xml_text.encode('utf-8')).hexdigest(),
                    }
                    write(output_dir / 'documents' / f'{seq}_{code}.json', doc)
                    product['document_ids'].append(doc_id)
                    report['document_count'] += 1
                product['search_text'] = '\n'.join(
                    f'{label}: {value}' for label, value in [
                        ('제품명', product['product_name']), ('영문명', product['product_name_en']),
                        ('업체명', product['company_name']), ('전문/일반', product['prescription_type']),
                        ('성분 및 함량 원문', product['material_name_raw']),
                        ('주성분', product['main_ingredient_raw']), ('성상', product['appearance_raw']),
                        ('ATC 코드', product['atc_code']), ('허가 상태', product['permit_status']),
                    ] if value
                )
                products[seq] = product
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report['errors'].append({'file': file.name, 'error': str(exc)})
    report['product_count'] = len(products)
    write(output_dir / 'drugs.json', list(products.values()))
    write(output_dir / 'quality_report.json', report)
    print(f"제품 {len(products)}개 / 근거 문서 {report['document_count']}개 / 표 {report['table_count']}개")
    print(f"확인 필요 오류 {len(report['errors'])}건 / 미확보 본문 {len(report['missing_fields'])}건")
    print('저장 위치:', output_dir.resolve())
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, default=base / 'data' / 'raw' / 'detail')
    parser.add_argument('--output', type=Path, default=base / 'data' / 'prepared')
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error('원본 폴더와 출력 폴더를 다르게 지정하세요.')
    try:
        raise SystemExit(run(args.input, args.output))
    except (OSError, ValueError) as exc:
        print('변환 실패:', exc)
        raise SystemExit(1)
