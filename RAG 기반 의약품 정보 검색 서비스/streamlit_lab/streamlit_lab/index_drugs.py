"""두 컬렉션 색인. 중복 없는 upsert, 오래된 ID 정리, 임베딩 캐시."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from drug_core import (ROOT, collections, embed, load_products, read_json,
                       settings, write_json, ServiceError, hash_text, vector_directory)


def index(limit=0):
    products = load_products()
    if limit:
        products = products[:limit]
    allowed = {p['drug_id'] for p in products}
    chunks = read_json(ROOT / 'data' / 'prepared' / 'chunks_preview.json')
    # 표는 병합 셀을 검증하기 전 자동 답변 근거로 쓰지 않는다.
    chunks = [c for c in chunks if c['drug_id'] in allowed and not c.get('contains_table')]
    manifest = {'ready': False, 'embedding_model': settings()['models']['embedding'],
                'products': len(products), 'chunks': len(chunks), 'limited_run': bool(limit),
                'vector_path': str(vector_directory())}
    write_json(ROOT / 'storage' / 'index_manifest.json', manifest)
    meta, docs = collections(require_ready=False)
    entries = [
        (meta, [{'id': 'meta:' + p['drug_id'], 'text': p['search_text'],
                 'metadata': {'drug_id': p['drug_id'], 'product_name': p['product_name'],
                              'source_type': 'metadata', 'hash': hash_text(p['search_text'])}}
                for p in products]),
        (docs, [{'id': c['chunk_id'], 'text': c['text'],
                 'metadata': {'drug_id': c['drug_id'], 'document_id': c['document_id'],
                              'article_index': c['article_index'], 'source_type': c.get('source_type', 'official_label'),
                              'hash': hash_text(c['text'])}}
                for c in chunks]),
    ]
    for collection, records in entries:
        existing = collection.get(include=['metadatas'])
        hashes = {i: m.get('hash') for i, m in zip(existing['ids'], existing['metadatas'])}
        changed = [r for r in records if hashes.get(r['id']) != r['metadata']['hash']]
        print(f'{collection.name}: 대상 {len(records)}, 신규·변경 {len(changed)}', flush=True)
        # 네 요청만 동시에 준비하고 저장은 순서대로 수행한다. 실패해도 캐시와 upsert는 재사용된다.
        batch_size = settings().get('embedding_batch_size', 16)
        group_size = 4 * batch_size
        with ThreadPoolExecutor(max_workers=4) as pool:
            for group_start in range(0, len(changed), group_size):
                batches = [changed[start:start+batch_size] for start in range(group_start, min(group_start+group_size, len(changed)), batch_size)]
                pending = [pool.submit(embed, [r['text'] for r in batch]) for batch in batches]
                for batch, future in zip(batches, pending):
                    try:
                        embeddings = future.result()
                    except ServiceError:
                        # 일시적인 제한·연결 오류는 진행 중인 요청 종료 후 다시 시도한다.
                        for other in pending:
                            try:
                                other.result()
                            except ServiceError:
                                pass
                        embeddings = embed([r['text'] for r in batch])
                    collection.upsert(ids=[r['id'] for r in batch], documents=[r['text'] for r in batch],
                                      embeddings=embeddings, metadatas=[r['metadata'] for r in batch])
                print(f'  {min(group_start+group_size,len(changed))}/{len(changed)}', flush=True)
        stale = set(existing['ids']) - {r['id'] for r in records}
        if stale:
            collection.delete(ids=list(stale))
    manifest.update(ready=True, metadata_count=meta.count(), document_count=docs.count())
    write_json(ROOT / 'storage' / 'index_manifest.json', manifest)
    print('색인 완료:', manifest, flush=True)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('limit은 0 이상이어야 합니다.')
    try:
        index(args.limit)
    except ServiceError as exc:
        print(str(exc))
        raise SystemExit(1)
