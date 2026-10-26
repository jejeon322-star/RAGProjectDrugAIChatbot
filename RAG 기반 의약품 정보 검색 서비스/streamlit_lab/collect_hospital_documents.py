"""본문이 없는 API 품목은 식약처 공식 PDF 원문과 추출 결과를 별도 보존."""
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path

import pdfplumber
import requests

from collect_hospital import TARGET, save


def collect(seq, code):
    folder = TARGET / 'data/raw/official_pdf'
    sidecar = folder / f'{seq}_{code}.json'
    if sidecar.exists():
        return json.loads(sidecar.read_text(encoding='utf-8'))
    url = f'https://nedrug.mfds.go.kr/pbp/cmn/pdfDownload/{seq}/{code}'
    try:
        response = requests.get(url, timeout=(5, 30))
        if response.status_code != 200 or not response.content.startswith(b'%PDF'):
            return {'item_seq': seq, 'code': code, 'error': 'official_pdf_unavailable'}
        folder.mkdir(parents=True, exist_ok=True)
        pdf_path = folder / f'{seq}_{code}.pdf'
        pdf_path.write_bytes(response.content)
        pages = []
        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            for index, page in enumerate(pdf.pages, 1):
                # 페이지 배치를 보존한 추출. 감지된 표는 검색에서 제외한다.
                pages.append({'page': index, 'text': page.extract_text(layout=True) or '',
                              'contains_table': bool(page.find_tables())})
        value = {'item_seq': seq, 'code': code, 'url': url,
                 'collected_at': datetime.now(timezone.utc).isoformat(),
                 'source_file': str(pdf_path), 'pages': pages,
                 'sha256': hashlib.sha256(response.content).hexdigest()}
        save(sidecar, value)
        return value
    except (requests.RequestException, ValueError, OSError):
        return {'item_seq': seq, 'code': code, 'error': 'download_or_parse_failed'}


def run():
    jobs = []
    for file in (TARGET / 'data/raw/detail').glob('*.json'):
        raw = json.loads(file.read_text(encoding='utf-8'))['body']['items'][0]
        for code in ('EE', 'UD', 'NB'):
            if not raw.get(code + '_DOC_DATA') and raw.get(code + '_DOC_ID'):
                jobs.append((str(raw['ITEM_SEQ']), code))
    errors = []
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(collect, *job) for job in jobs]
        for index, future in enumerate(as_completed(futures), 1):
            value = future.result()
            if value.get('error'):
                errors.append(value)
            if index % 20 == 0 or index == len(jobs):
                print('공식 PDF', index, '/', len(jobs), '미확보', len(errors), flush=True)
    save(TARGET / 'pdf_collection_report.json', {'requested': len(jobs), 'errors': errors})


if __name__ == '__main__':
    run()
