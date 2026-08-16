#!/usr/bin/env python3
"""Local browser review tool for manually verifying VLM financial-PDF gold labels."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.vlm.vlm_financial_eval import (
    case_files,
    load_case,
    resolve_pdf_path,
    save_case,
    sha256_file,
    validate_case,
)  # noqa: E402


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Financial PDF review</title>
<style>body{font:14px system-ui;margin:0;display:grid;grid-template-columns:18rem 1fr 28rem;height:100vh}aside,main{padding:12px;overflow:auto;border-right:1px solid #ddd}iframe{width:100%;height:85vh;border:0}button{margin:4px}textarea{width:100%;height:18rem}li{cursor:pointer;margin:.35rem 0}.active{font-weight:bold}</style></head>
<body><aside><h2>Cases</h2><div id="progress"></div><ul id="cases"></ul></aside><main><h2 id="title">Select a case</h2><iframe id="pdf"></iframe></main><aside><h2>Gold label</h2><p>Use null for a verified missing value. Values are pence except employees, which use counts.</p><textarea id="editor"></textarea><div id="errors"></div><button onclick="save()">Save</button><button onclick="verify()">Mark verified</button></aside>
<script>let current=null;const $=id=>document.getElementById(id);async function refresh(){let p=await fetch('/api/progress').then(r=>r.json());$('progress').textContent=`${p.verified}/${p.total} verified`;let cs=await fetch('/api/cases').then(r=>r.json());$('cases').innerHTML=cs.map(c=>`<li class="${c.id===current?'active':''}" onclick="openCase('${c.id}')">${c.id} (${c.status})</li>`).join('')}async function openCase(id){current=id;let c=await fetch('/api/cases/'+encodeURIComponent(id)).then(r=>r.json());$('title').textContent=c.id;$('pdf').src='/pdf/'+encodeURIComponent(id);$('editor').value=JSON.stringify(c,null,2);$('errors').textContent='';refresh()}async function save(){let body;try{body=JSON.parse($('editor').value)}catch(e){$('errors').textContent=e;return}let r=await fetch('/api/cases/'+encodeURIComponent(current),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let out=await r.json();$('errors').textContent=out.errors?.join('\n')||'Saved';if(r.ok)openCase(current)}async function verify(){let c;try{c=JSON.parse($('editor').value)}catch(e){$('errors').textContent=e;return}c.review=c.review||{};c.review.status='verified';c.review.reviewed_at=new Date().toISOString();$('editor').value=JSON.stringify(c,null,2);save()}refresh();</script></body></html>"""


def build_handler(cases_dir: Path) -> type[BaseHTTPRequestHandler]:
    class ReviewHandler(BaseHTTPRequestHandler):
        def send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def lookup(self, identifier: str) -> Path | None:
            for path in case_files(cases_dir):
                if path.stem == identifier:
                    return path
            return None

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                content = PAGE.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            if path == "/api/cases":
                self.send_json([{"id": item.stem, "status": load_case(item).get("review", {}).get("status")} for item in case_files(cases_dir)])
                return
            if path == "/api/progress":
                cases = [load_case(item) for item in case_files(cases_dir)]
                self.send_json({"total": len(cases), "verified": sum(case.get("review", {}).get("status") == "verified" for case in cases)})
                return
            if path.startswith("/api/cases/") or path.startswith("/pdf/"):
                identifier = unquote(path.rsplit("/", 1)[-1])
                source = self.lookup(identifier)
                if source is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                case = load_case(source)
                if path.startswith("/api/"):
                    self.send_json(case)
                    return
                pdf_path = resolve_pdf_path(case)
                if not pdf_path.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND, "PDF no longer exists")
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mimetypes.guess_type(str(pdf_path))[0] or "application/pdf")
                self.send_header("Content-Length", str(pdf_path.stat().st_size))
                self.end_headers()
                with pdf_path.open("rb") as pdf:
                    self.wfile.write(pdf.read())
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if not path.startswith("/api/cases/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            source = self.lookup(unquote(path.rsplit("/", 1)[-1]))
            if source is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                case = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                self.send_json({"errors": ["invalid JSON"]}, HTTPStatus.BAD_REQUEST)
                return
            errors = validate_case(case, require_complete=case.get("review", {}).get("status") == "verified")
            if not errors and case.get("review", {}).get("status") == "verified":
                pdf_path = resolve_pdf_path(case)
                if not pdf_path.is_file() or sha256_file(pdf_path) != case.get("pdf_sha256"):
                    errors.append("PDF is missing or its SHA-256 no longer matches this label")
            if errors:
                self.send_json({"errors": errors}, HTTPStatus.BAD_REQUEST)
                return
            save_case(source, case)
            self.send_json({"saved": True})

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ReviewHandler


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-dir", default="evals/vlm_financials/cases")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), build_handler(Path(args.cases_dir)))
    print(f"Review interface: http://127.0.0.1:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
