"""Multipart file upload saved to disk.

Shows multipart handling in Veloce: a text field and a file declared as
handler parameters with ``Form`` and ``File``/``UploadFile``, the file name
sanitised with ``secure_filename`` before it touches the filesystem, and the
upload streamed to disk with ``UploadFile.save``. A served HTML form lets you
test it from a browser.

Run it::

    python examples/file_upload.py

Then try::

    curl -F "title=Report" -F "file=@README.md" localhost:8000/upload
"""

from __future__ import annotations

from pathlib import Path

from veloce import File, Form, HTMLResponse, UploadFile, Veloce, secure_filename

app = Veloce(title="File Upload")

UPLOAD_DIR = Path("uploaded_files")

_FORM = """\
<!doctype html>
<title>Veloce upload</title>
<h1>Upload a file</h1>
<form action="/upload" method="post" enctype="multipart/form-data">
  <input name="title" placeholder="Title" />
  <input name="file" type="file" />
  <button type="submit">Upload</button>
</form>
"""


@app.get("/")
async def index():
    return HTMLResponse(_FORM)


# A sync `def` handler: Veloce runs it in a thread pool, so the blocking
# filesystem writes below never stall the event loop.
@app.post("/upload")
def upload(title: str = Form(), file: UploadFile = File()):
    UPLOAD_DIR.mkdir(exist_ok=True)
    # Never trust the client-supplied name as a path component.
    safe_name = secure_filename(file.filename or "upload.bin")
    destination = UPLOAD_DIR / safe_name
    file.save(str(destination))
    return {
        "title": title,
        "filename": safe_name,
        "size": file.size,
        "content_type": file.content_type,
    }


if __name__ == "__main__":
    app.run(port=8000)
