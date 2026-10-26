"""병원 후보 자료 전용 실행 진입점. 기존 전체 약 DB와 분리한다."""
import os
from pathlib import Path
import runpy

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
os.environ['DRUG_PROJECT_ROOT'] = str(BASE / 'data/hospital/project')
runpy.run_path(str(BASE / 'hospital_chat.py'), run_name='__main__')
