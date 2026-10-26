"""PDF 이름 후보만 식약처에서 검색. 후보 연결은 병원 채택 확정이 아님."""
import json
import os
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / 'data/hospital/project'
BASE = 'https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService08/'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(20):
        try:
            temp.replace(path)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.2)


def fetch(operation, **params):
    for attempt in range(3):
        try:
            response = requests.get(BASE + operation, params={
                'serviceKey': os.environ['MFDS_API_KEY'], 'type': 'json',
                'pageNo': 1, 'numOfRows': 100, **params}, timeout=(5, 20))
            response.raise_for_status()
            data = response.json()
            if data.get('header', {}).get('resultCode') != '00':
                raise ValueError('API result code')
            return data
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise RuntimeError('식약처 조회 실패; 저장 자료를 유지합니다.') from None
            time.sleep(1 + attempt)


def run():
    load_dotenv(ROOT / '.env')
    manifest = json.loads((ROOT / 'data/hospital/hospital_candidates.json').read_text(encoding='utf-8-sig'))
    progress = {'stage': 'matching', 'entries': [], 'errors': [], 'product_count': 0}
    progress_path = ROOT / 'data/hospital/progress.json'
    unique = {}
    for entry in manifest['candidates']:
        term = entry['search_term']
        cache = TARGET / 'data/raw/list' / (term + '.json')
        try:
            data = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else fetch('getDrugPrdtPrmsnInq08', item_name=term)
            save(cache, data)
            rows = data.get('body', {}).get('items', []) or []
            # 중단·취하 품목은 현재 사용 제품 후보에서 제외한다.
            rows = [r for r in rows if str(r.get('CANCEL_NAME', '') or '').strip() in ('', '정상')]
            matches = [{'item_seq': str(r['ITEM_SEQ']), 'product_name': r['ITEM_NAME'],
                        'company_name': r.get('ENTP_NAME', ''),
                        'hospital_adoption': 'unconfirmed'} for r in rows]
            progress['entries'].append({**entry, 'official_candidates': matches,
                'match_status': 'candidates_found' if matches else 'not_found',
                'total_count': data.get('body', {}).get('totalCount'),
                'truncated': int(data.get('body', {}).get('totalCount', 0)) > 100})
            unique.update({str(r['ITEM_SEQ']): r for r in rows})
            print(term, len(matches), flush=True)
        except RuntimeError:
            progress['errors'].append({'term': term, 'stage': 'list'})
        save(progress_path, progress)
    progress['stage'] = 'collecting_details'
    def collect_one(seq):
        path = TARGET / 'data/raw/detail' / (seq + '.json')
        try:
            cached = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            if not cached.get('_hospital_detail_single_row'):
                data = fetch('getDrugPrdtPrmsnDtlInq08', item_seq=seq, numOfRows=1)
                items = data.get('body', {}).get('items', []) or []
                if not any(str(r.get('ITEM_SEQ')) == seq for r in items):
                    raise RuntimeError('품목 ID 불일치')
                data['_hospital_detail_single_row'] = True
                save(path, data)
            return seq, True
        except RuntimeError:
            return seq, False
    log = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(collect_one, seq) for seq in unique]
        for index, future in enumerate(as_completed(jobs), 1):
            seq, ok = future.result()
            if ok:
                progress['product_count'] += 1
                log[seq] = {'collected_at': datetime.fromtimestamp((TARGET / 'data/raw/detail' / (seq + '.json')).stat().st_mtime, tz=timezone.utc).isoformat()}
            else:
                progress['errors'].append({'item_seq': seq, 'stage': 'detail'})
            save(progress_path, progress)
            print('상세', index, '/', len(unique), flush=True)
    save(TARGET / 'data/raw/collection_log.json', log)
    progress['stage'] = 'collected'
    save(progress_path, progress)
    save(ROOT / 'data/hospital/matches.json', progress)


if __name__ == '__main__':
    run()
