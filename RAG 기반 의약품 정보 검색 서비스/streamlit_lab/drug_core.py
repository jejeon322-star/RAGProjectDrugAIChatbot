"""설정, 데이터 및 GMS 연결. 인증정보는 로컬 환경에서만 읽는다."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(os.getenv('DRUG_PROJECT_ROOT', Path(__file__).resolve().parent)).resolve()
load_dotenv(ROOT / '.env')
DEFAULT_BASE = 'https://gms.ssafy.io/gmsapi/api.openai.com/v1/'


class ServiceError(RuntimeError):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def hash_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def settings():
    path = ROOT / 'rag_settings.json'
    if not path.exists():
        raise ServiceError('rag_settings.json이 없습니다. 데이터 준비를 먼저 실행하세요.')
    cfg = read_json(path)
    cfg.setdefault('models', {})
    cfg['models'].setdefault('embedding', 'text-embedding-3-small')
    cfg['models'].setdefault('chat', 'gpt-6-astra')
    batch_size = cfg.get('embedding_batch_size', 16)
    if not isinstance(batch_size, int) or not 1 <= batch_size <= 64:
        raise ServiceError('임베딩 배치 크기는 1~64의 정수로 설정하세요.')
    for key in ('candidate_k', 'external_top_k'):
        value = cfg.get('retrieval', {}).get(key)
        if not isinstance(value, int) or not 1 <= value <= 50:
            raise ServiceError('검색 후보 수와 Top-k는 1~50의 정수로 설정하세요.')
    if cfg['retrieval'].get('method', 'similarity') != 'similarity':
        raise ServiceError('현재 구현은 similarity 검색을 지원합니다. MMR은 심화 구현 대상입니다.')
    return cfg


def client():
    key = os.getenv('GMS_KEY')
    if not key:
        raise ServiceError('GMS_KEY 설정이 필요합니다.')
    base = os.getenv('GMS_BASE_URL', DEFAULT_BASE)
    if base.rstrip('/') == 'https://gms.ssafy.io/gmsapi':
        base = DEFAULT_BASE
    return OpenAI(api_key=key, base_url=base, timeout=75, max_retries=1)


def log_event(kind, data):
    # 원문 질문·키·전체 프롬프트는 저장하지 않는다.
    folder = ROOT / 'storage' / 'logs'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'events.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps({'time': time.time(), 'kind': kind, **data}, ensure_ascii=False) + '\n')


def chat_json(prompt_name, payload):
    # 프롬프트 편집 기준은 프로젝트 루트 prompts/ 한 곳으로 통일한다.
    prompt_dir = Path(os.getenv('DRUG_PROMPT_DIR', Path(__file__).resolve().parent / 'prompts'))
    instructions = (prompt_dir / f'{prompt_name}.txt').read_text(encoding='utf-8-sig')
    started = time.monotonic()
    try:
        result = client().chat.completions.create(
            model=settings()['models']['chat'],
            messages=[{'role': 'system', 'content': instructions},
                      {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
            response_format={'type': 'json_object'},
            max_completion_tokens=3500,
        )
        text = result.choices[0].message.content or ''
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError()
        usage = result.usage.model_dump() if result.usage else {}
        log_event('chat', {'stage': prompt_name, 'duration_seconds': round(time.monotonic()-started, 3), 'prompt_hash': hash_text(instructions), 'usage': usage})
        return value
    except Exception:
        raise ServiceError('AI 응답을 처리하지 못했습니다. 연결 설정을 확인하고 다시 시도하세요.') from None


def embed(texts):
    """동일 모델·주소·텍스트의 임베딩은 SQLite에서 재사용한다."""
    if not texts:
        return []
    model = settings()['models']['embedding']
    base = os.getenv('GMS_BASE_URL', DEFAULT_BASE)
    path = ROOT / 'storage' / 'embedding_cache.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        keys = [hash_text(base + '\n' + model + '\n' + text) for text in texts]
        values = {}
        for key in set(keys):
            row = db.execute('SELECT value FROM vectors WHERE key=?', (key,)).fetchone()
            if row:
                values[key] = json.loads(row[0])
        pending = list(dict.fromkeys(key for key in keys if key not in values))
        mapping = dict(zip(keys, texts))
        batch_size = settings().get('embedding_batch_size', 16)
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start+batch_size]
            try:
                response = client().embeddings.create(model=model, input=[mapping[k] for k in batch])
            except Exception:
                raise ServiceError('임베딩 요청에 실패했습니다. 저장된 캐시는 유지됩니다. 다시 실행할 수 있습니다.') from None
            ordered = sorted(response.data, key=lambda x: x.index)
            if len(ordered) != len(batch):
                raise ServiceError('임베딩 결과 수가 요청과 다릅니다.')
            for key, item in zip(batch, ordered):
                values[key] = item.embedding
                db.execute('INSERT OR REPLACE INTO vectors VALUES (?,?)', (key, json.dumps(item.embedding)))
            db.commit()
            log_event('embedding', {'count': len(batch), 'usage': response.usage.model_dump()})
        return [values[key] for key in keys]


def load_products():
    path = ROOT / 'data' / 'prepared' / 'drugs.json'
    if not path.exists():
        raise ServiceError('데이터가 없습니다. python build_drug_dataset.py를 먼저 실행하세요.')
    return read_json(path)


def vector_client():
    import chromadb
    from chromadb.config import Settings
    return chromadb.PersistentClient(path=str(vector_directory()),
                                     settings=Settings(anonymized_telemetry=False))


def vector_directory():
    # Windows HNSW의 한글 경로 저장 오류를 피하고 모델별 DB를 분리한다.
    override = os.getenv('DRUG_CHROMA_DIR')
    if override:
        return Path(override).resolve()
    local = Path(os.getenv('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    identity = str(ROOT) + '\n' + settings()['models']['embedding']
    return local / 'DrugRag' / hash_text(identity)[:16] / 'chroma'


def collections(require_ready=True):
    manifest = ROOT / 'storage' / 'index_manifest.json'
    if require_ready:
        if not manifest.exists() or not read_json(manifest).get('ready'):
            raise ServiceError('검색 색인이 준비되지 않았습니다. python index_drugs.py를 실행하세요.')
        if read_json(manifest)['embedding_model'] != settings()['models']['embedding']:
            raise ServiceError('임베딩 모델이 변경됐습니다. 다시 색인하세요.')
    db = vector_client()
    return tuple(db.get_or_create_collection(name=settings()['collections'][key], embedding_function=None,
                                            metadata={'hnsw:space': 'cosine'})
                 for key in ('metadata', 'documents'))
