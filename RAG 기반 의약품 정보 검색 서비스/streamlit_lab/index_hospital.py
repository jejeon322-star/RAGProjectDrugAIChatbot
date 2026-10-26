"""원래 프로젝트의 로컬 키로 병원 전용 컬렉션을 색인한다."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
os.environ['DRUG_PROJECT_ROOT'] = str(BASE / 'data/hospital/project')

from index_drugs import index

if __name__ == '__main__':
    index()
