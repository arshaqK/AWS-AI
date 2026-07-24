# run_test.py
from pipeline import process_pdf
import json

result = process_pdf("report_LONG.pdf")
print(json.dumps(result, indent=2))